"""Authentication service: login, refresh rotation, logout, passwords.

Token model (authentication-and-rbac-plan.md §3.2):

- The access token is a 15-minute HS256 JWT naming its refresh session
  (``sid``) and the user's ``token_version`` (``ver``).
- The refresh token is an opaque 384-bit string in an HttpOnly cookie;
  only its SHA-256 digest is stored, in ``refresh_sessions``.
- Every refresh **rotates**: the used session is revoked and a new one
  is issued, linked through ``rotated_from``. Presenting a revoked or
  rotated token again is treated as theft: every session of that user
  is revoked and ``auth.refresh.reuse_detected`` is audited.

This module is the only place that mutates users, sessions and reset
tokens; the API layer maps its exceptions onto HTTP responses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.password_policy import describe_violations, validate_password
from app.core.security import (
    access_token_expiry,
    create_access_token,
    hash_password,
    hash_token,
    new_opaque_token,
    refresh_session_expiry,
    verify_and_update_password,
    verify_dummy_password,
)
from app.core.settings import get_settings
from app.models import PasswordResetToken, RefreshSession, User


class InvalidCredentials(Exception):
    """Unknown user or wrong password (identical to the outside)."""


class AccountLocked(InvalidCredentials):
    """Too many failures; rejected until ``locked_until`` passes."""


class InvalidRefreshToken(Exception):
    """Unknown, expired or revoked refresh token."""


class RefreshReuseDetected(InvalidRefreshToken):
    """A rotated/revoked token was presented again; family revoked."""


class InvalidResetToken(Exception):
    """Unknown, expired, used or non-redeemable reset token."""


class WeakPassword(Exception):
    """The candidate password violates the policy."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class LoginOutcome:
    """What a successful login or refresh returns to the API layer."""

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    session_id: str
    roles: list[str]
    # For audit attribution only; never returned to the client body.
    user_id: int
    username: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Treat naive datetimes (SQLite) as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value


def hash_ip(ip: str | None) -> str | None:
    """Salted digest of a client IP; raw addresses are never stored."""

    if not ip:
        return None

    settings = get_settings()
    return hash_token(f"{settings.jwt_secret}:{ip}")


def login(
    db: Session,
    *,
    username: str,
    password: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> LoginOutcome:
    """Verify credentials and start a refresh session.

    Unknown users and wrong passwords raise the same ``InvalidCredentials``
    and take the same Argon2 work. The ``failed_login_count`` /
    ``locked_until`` pair implements the exponential lockout.
    """

    settings = get_settings()
    now = _utcnow()
    normalized = username.strip().lower()

    user = db.scalar(select(User).where(User.username == normalized))

    if user is None:
        verify_dummy_password(password)
        raise InvalidCredentials

    if not user.is_active:
        # Disabled accounts are indistinguishable from unknown ones:
        # same response, same Argon2 work.
        verify_dummy_password(password)
        raise InvalidCredentials

    if (
        user.locked_until is not None
        and _as_utc(user.locked_until) > now
    ):
        raise AccountLocked

    valid, upgraded = verify_and_update_password(
        password, user.password_hash
    )

    if not valid:
        user.failed_login_count += 1

        if user.failed_login_count >= settings.lockout_threshold:
            over = user.failed_login_count - settings.lockout_threshold
            delay = min(
                settings.lockout_base_seconds * (2**over),
                settings.lockout_max_seconds,
            )
            user.locked_until = now + timedelta(seconds=delay)

        db.commit()
        raise InvalidCredentials

    if upgraded:
        # Transparently upgrade hashes made with older parameters.
        user.password_hash = upgraded

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    session, refresh_token = _start_refresh_session(
        db,
        user,
        user_agent=user_agent,
        ip=ip,
        now=now,
    )

    access_token, expires_at = _issue_access_token(user, session.id, now)
    roles = user.role_names()

    db.commit()

    return LoginOutcome(
        access_token=access_token,
        access_expires_at=expires_at,
        refresh_token=refresh_token,
        session_id=str(session.id),
        roles=roles,
        user_id=user.id,
        username=user.username,
    )


def refresh(
    db: Session,
    *,
    refresh_token: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> LoginOutcome:
    """Rotate a refresh session and issue a fresh access token."""

    now = _utcnow()
    token_hash = hash_token(refresh_token)

    session = db.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == token_hash
        )
    )

    if session is None:
        raise InvalidRefreshToken

    if session.revoked_at is not None:
        # Reuse of a rotated or revoked token: assume theft and revoke
        # the whole family (every session of this user).
        _revoke_all_user_sessions(db, session.user_id, now=now)
        db.commit()
        raise RefreshReuseDetected

    if _as_utc(session.expires_at) <= now:
        raise InvalidRefreshToken

    user = db.get(User, session.user_id)

    if user is None or not user.is_active:
        raise InvalidRefreshToken

    session.revoked_at = now

    new_session, new_refresh_token = _start_refresh_session(
        db,
        user,
        user_agent=user_agent,
        ip=ip,
        now=now,
        rotated_from=session.id,
    )

    access_token, expires_at = _issue_access_token(
        user, new_session.id, now
    )
    roles = user.role_names()

    db.commit()

    return LoginOutcome(
        access_token=access_token,
        access_expires_at=expires_at,
        refresh_token=new_refresh_token,
        session_id=str(new_session.id),
        roles=roles,
        user_id=user.id,
        username=user.username,
    )


def logout(
    db: Session,
    *,
    user_id: int,
    refresh_token: str | None,
) -> None:
    """Revoke the refresh session named by the cookie, if any."""

    if not refresh_token:
        return

    now = _utcnow()
    token_hash = hash_token(refresh_token)

    session = db.scalar(
        select(RefreshSession).where(
            RefreshSession.token_hash == token_hash,
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
        )
    )

    if session is not None:
        session.revoked_at = now
        db.commit()


def change_password(
    db: Session,
    *,
    user_id: int,
    current_password: str,
    new_password: str,
) -> None:
    """Set a new password after verifying the current one.

    Bumps ``token_version`` and revokes every refresh session, so all
    outstanding access tokens and other sessions stop working.
    """

    settings = get_settings()
    user = db.get(User, user_id)

    if user is None or not user.is_active:
        raise InvalidCredentials

    valid, _ = verify_and_update_password(
        current_password, user.password_hash
    )

    if not valid:
        raise InvalidCredentials

    _set_password(db, user, new_password, settings.password_min_length)


def reset_password(
    db: Session,
    *,
    token: str,
    new_password: str,
) -> str:
    """Consume a one-time admin-issued token and set a new password.

    Returns the username whose password was set.
    """

    settings = get_settings()
    now = _utcnow()

    reset = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_token(token)
        )
    )

    if reset is None or not reset.is_redeemable(now):
        raise InvalidResetToken

    user = db.get(User, reset.user_id)

    if user is None or not user.is_active:
        raise InvalidResetToken

    _set_password(db, user, new_password, settings.password_min_length)

    reset.used_at = now
    db.commit()

    return user.username


def issue_reset_token(
    db: Session,
    *,
    user_id: int,
) -> tuple[str, datetime]:
    """Create a one-time reset token; returns the token and its expiry.

    The caller returns the raw token in the admin API response once;
    only the digest is persisted.
    """

    settings = get_settings()
    now = _utcnow()

    user = db.get(User, user_id)

    if user is None:
        raise LookupError("User not found.")

    if not user.is_active:
        raise ValueError("Cannot issue a reset token for a disabled user.")

    token = new_opaque_token()

    db.add(
        PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=now
            + timedelta(minutes=settings.reset_token_ttl_minutes),
        )
    )
    db.commit()

    return token, now + timedelta(
        minutes=settings.reset_token_ttl_minutes
    )


def _set_password(
    db: Session,
    user: User,
    new_password: str,
    min_length: int,
) -> None:
    """Shared tail of change/reset: policy, hash, version bump, revoke."""

    violations = validate_password(
        new_password,
        username=user.username,
        min_length=min_length,
    )

    if violations:
        raise WeakPassword(
            describe_violations(violations, min_length=min_length)
        )

    user.password_hash = hash_password(new_password)
    user.token_version += 1

    now = _utcnow()
    _revoke_all_user_sessions(db, user.id, now=now)

    db.commit()


def _start_refresh_session(
    db: Session,
    user: User,
    *,
    user_agent: str | None,
    ip: str | None,
    now: datetime,
    rotated_from: uuid.UUID | None = None,
) -> tuple[RefreshSession, str]:
    """Create one refresh session; returns the session and raw token."""

    token = new_opaque_token()

    session = RefreshSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token(token),
        issued_at=now,
        expires_at=refresh_session_expiry(now),
        rotated_from=rotated_from,
        user_agent=(user_agent or "")[:255] or None,
        ip_hash=hash_ip(ip),
    )

    db.add(session)

    return session, token


def _issue_access_token(
    user: User,
    session_id: uuid.UUID,
    now: datetime,
) -> tuple[str, datetime]:
    """Sign the short-lived access token for one refresh session."""

    expires_at = access_token_expiry(now)

    token = create_access_token(
        user_id=user.id,
        session_id=str(session_id),
        roles=user.role_names(),
        token_version=user.token_version,
        expires_at=expires_at,
    )

    return token, expires_at


def _revoke_all_user_sessions(
    db: Session,
    user_id: int,
    *,
    now: datetime,
) -> int:
    """Revoke every active refresh session of one user."""

    sessions = db.scalars(
        select(RefreshSession).where(
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
        )
    ).all()

    for session in sessions:
        session.revoked_at = now

    return len(sessions)


__all__ = [
    "AccountLocked",
    "InvalidCredentials",
    "InvalidRefreshToken",
    "InvalidResetToken",
    "LoginOutcome",
    "RefreshReuseDetected",
    "WeakPassword",
    "change_password",
    "hash_ip",
    "issue_reset_token",
    "login",
    "logout",
    "refresh",
    "reset_password",
]
