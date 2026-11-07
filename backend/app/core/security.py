"""Password hashing and token primitives for authentication.

Passwords are hashed with Argon2id (the pwdlib ``recommended`` hasher,
which selects it because the ``argon2`` extra is installed). Access
tokens are short HS256 JWTs; refresh and reset credentials are opaque
random strings that are stored only as SHA-256 digests.

Nothing in this module reads the database or opens a network
connection, so it is safe to import from the API layer's module scope.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.core.settings import get_settings

_password_hasher = PasswordHash.recommended()

# Computed once at import: verifying a login attempt for a user that does
# not exist against this hash makes both failure paths spend the same
# Argon2 work, so response timing cannot reveal which part failed.
_DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(24))


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""

    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored Argon2id hash."""

    return _password_hasher.verify(password, password_hash)


def verify_and_update_password(
    password: str,
    password_hash: str,
) -> tuple[bool, str | None]:
    """Verify a password and return an upgraded hash when warranted.

    Returns ``(valid, new_hash)``. ``new_hash`` is not ``None`` only
    when the password verified *and* the stored hash was produced with
    parameters below the library's current defaults, so the caller can
    persist the stronger hash transparently at login time.
    """

    return _password_hasher.verify_and_update(password, password_hash)


def verify_dummy_password(password: str) -> bool:
    """Verify against the dummy hash to equalise unknown-user timing."""

    return _password_hasher.verify(password, _DUMMY_HASH)


def password_needs_rehash(password_hash: str) -> bool:
    """Whether the stored hash was made with weaker current parameters."""

    return _password_hasher.current_hasher.check_needs_rehash(
        password_hash
    )


def new_opaque_token() -> str:
    """Generate a 384-bit opaque token for refresh or reset use."""

    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest stored in place of an opaque token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def access_token_expiry(now: datetime | None = None) -> datetime:
    """Expiry instant for a fresh access token."""

    settings = get_settings()
    return (now or datetime.now(timezone.utc)) + timedelta(
        minutes=settings.access_ttl_minutes
    )


def refresh_session_expiry(now: datetime | None = None) -> datetime:
    """Expiry instant for a fresh refresh session."""

    settings = get_settings()
    return (now or datetime.now(timezone.utc)) + timedelta(
        days=settings.refresh_ttl_days
    )


def create_access_token(
    *,
    user_id: int,
    session_id: str,
    roles: list[str],
    token_version: int,
    expires_at: datetime,
) -> str:
    """Issue a signed HS256 access token for one refresh session.

    ``ver`` carries the user's ``token_version`` so a password change,
    role change or disable can invalidate every outstanding token at
    once; ``sid`` ties the token to its refresh session for logout.
    """

    settings = get_settings()
    now = datetime.now(timezone.utc)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": session_id,
        "roles": roles,
        "ver": token_version,
        "iat": now,
        "exp": expires_at,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": uuid.uuid4().hex,
    }

    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify an access token, or raise InvalidTokenError."""

    settings = get_settings()

    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )


__all__ = [
    "InvalidTokenError",
    "access_token_expiry",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "hash_token",
    "new_opaque_token",
    "password_needs_rehash",
    "refresh_session_expiry",
    "verify_dummy_password",
    "verify_password",
]
