"""Admin user management: create, update, disable, list.

Guard rails (recorded decisions on the plan's ``[OPEN]`` items):

- An admin can never disable or demote their **own** account.
- The change that would leave zero active administrators is refused.
- Role changes bump ``token_version`` so demotions take effect at once;
  disabling additionally revokes every refresh session.

Every mutation writes its audit rows on the caller's session, so they
commit or roll back together with the change (fail closed).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.password_policy import describe_violations, validate_password
from app.core.permissions import ROLE_ADMIN
from app.core.security import hash_password
from app.core.settings import get_settings
from app.models import Role, User
from app.services import audit_service
from app.services.audit_service import (
    ACTION_USER_CREATED,
    ACTION_USER_DISABLED,
    ACTION_USER_ROLE_CHANGED,
    ACTION_USER_UPDATED,
    OUTCOME_SUCCESS,
)


class UsernameTaken(Exception):
    """The username (case-insensitive) already exists."""


class UserNotFound(LookupError):
    """No user with that id."""


class InvalidRoleName(Exception):
    """A role name outside the fixed set was requested."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names
        self.message = f"Unknown role: {', '.join(names)}."


class SelfManagementError(Exception):
    """An admin tried to disable or demote their own account."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LastAdminError(Exception):
    """The change would leave zero active administrators."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def list_users(db: Session) -> list[User]:
    """Every managed user, newest first."""

    return list(
        db.scalars(
            select(User).order_by(User.created_at.desc(), User.id.desc())
        )
    )


def list_roles(db: Session) -> list[Role]:
    """The fixed role set, by id."""

    return list(db.scalars(select(Role).order_by(Role.id)))


def create_user(
    db: Session,
    *,
    username: str,
    display_name: str,
    password: str,
    role_names: list[str],
    acting_admin: User,
    request_id: str | None = None,
) -> User:
    """Provision an account; admin-provisioned only, no self-signup."""

    settings = get_settings()
    normalized = _validate_username(username)

    if db.scalar(select(User).where(User.username == normalized)):
        raise UsernameTaken

    roles = _resolve_roles(db, role_names)

    violations = validate_password(
        password,
        username=normalized,
        min_length=settings.password_min_length,
    )

    if violations:
        from app.services.auth_service import WeakPassword

        raise WeakPassword(
            describe_violations(
                violations, min_length=settings.password_min_length
            )
        )

    user = User(
        username=normalized,
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        is_active=True,
        roles=roles,
        created_by=acting_admin.id,
    )

    db.add(user)
    db.flush()

    audit_service.record_event(
        db,
        action=ACTION_USER_CREATED,
        outcome=OUTCOME_SUCCESS,
        actor_user_id=acting_admin.id,
        actor_username=acting_admin.username,
        resource_type="user",
        resource_id=user.id,
        request_id=request_id,
        event_metadata={
            "username": user.username,
            "roles": sorted(role.name for role in roles),
        },
    )

    db.commit()

    return user


def update_user(
    db: Session,
    *,
    user_id: int,
    acting_admin: User,
    display_name: str | None = None,
    role_names: list[str] | None = None,
    is_active: bool | None = None,
    request_id: str | None = None,
) -> User:
    """PATCH semantics: only the provided fields change."""

    user = db.get(User, user_id)

    if user is None:
        raise UserNotFound

    changed_roles = False

    if role_names is not None:
        if user.id == acting_admin.id:
            raise SelfManagementError(
                "You cannot change the roles of your own account."
            )

        roles = _resolve_roles(db, role_names)
        _guard_last_admin(
            db,
            target=user,
            admin_stays=ROLE_ADMIN in [role.name for role in roles],
        )

        previous = user.role_names()
        user.roles = roles
        changed_roles = True

        audit_service.record_event(
            db,
            action=ACTION_USER_ROLE_CHANGED,
            outcome=OUTCOME_SUCCESS,
            actor_user_id=acting_admin.id,
            actor_username=acting_admin.username,
            resource_type="user",
            resource_id=user.id,
            request_id=request_id,
            event_metadata={
                "username": user.username,
                "from": previous,
                "to": user.role_names(),
            },
        )

    if display_name is not None:
        user.display_name = display_name.strip()

    disabling = is_active is False and user.is_active

    if is_active is not None and is_active != user.is_active:
        if user.id == acting_admin.id:
            raise SelfManagementError(
                "You cannot disable your own account."
            )

        if disabling:
            _guard_last_admin(db, target=user, admin_stays=False)

        user.is_active = is_active

    if changed_roles or disabling:
        # Demotions and disables must kill outstanding access tokens.
        user.token_version += 1

    if disabling:
        audit_service.record_event(
            db,
            action=ACTION_USER_DISABLED,
            outcome=OUTCOME_SUCCESS,
            actor_user_id=acting_admin.id,
            actor_username=acting_admin.username,
            resource_type="user",
            resource_id=user.id,
            request_id=request_id,
            event_metadata={"username": user.username},
        )

    audit_service.record_event(
        db,
        action=ACTION_USER_UPDATED,
        outcome=OUTCOME_SUCCESS,
        actor_user_id=acting_admin.id,
        actor_username=acting_admin.username,
        resource_type="user",
        resource_id=user.id,
        request_id=request_id,
        event_metadata={
            "username": user.username,
            "fields": [
                field
                for field, provided in (
                    ("display_name", display_name is not None),
                    ("roles", role_names is not None),
                    ("is_active", is_active is not None),
                )
                if provided
            ],
        },
    )

    db.commit()

    return user


def disable_user(
    db: Session,
    *,
    user_id: int,
    acting_admin: User,
    request_id: str | None = None,
) -> User:
    """Dedicated disable endpoint: inactive + version bump."""

    user = db.get(User, user_id)

    if user is None:
        raise UserNotFound

    if user.id == acting_admin.id:
        raise SelfManagementError("You cannot disable your own account.")

    if not user.is_active:
        return user

    _guard_last_admin(db, target=user, admin_stays=False)

    user.is_active = False
    user.token_version += 1

    audit_service.record_event(
        db,
        action=ACTION_USER_DISABLED,
        outcome=OUTCOME_SUCCESS,
        actor_user_id=acting_admin.id,
        actor_username=acting_admin.username,
        resource_type="user",
        resource_id=user.id,
        request_id=request_id,
        event_metadata={"username": user.username},
    )

    db.commit()

    return user


def _validate_username(username: str) -> str:
    """Usernames are stored lowercase; uniqueness is case-insensitive."""

    normalized = username.strip().lower()

    if not normalized:
        from app.services.auth_service import WeakPassword

        raise WeakPassword("Username must not be empty.")

    return normalized


def _resolve_roles(db: Session, role_names: list[str]) -> list[Role]:
    """Map requested role names onto seeded rows, rejecting unknowns."""

    requested = {name.strip().lower() for name in role_names if name.strip()}

    roles = list(
        db.scalars(select(Role).where(Role.name.in_(requested)))
    )

    found = {role.name for role in roles}

    if found != requested:
        raise InvalidRoleName(sorted(requested - found))

    return roles


def _guard_last_admin(
    db: Session,
    *,
    target: User,
    admin_stays: bool,
) -> None:
    """Refuse changes that would remove the last active admin."""

    if admin_stays:
        return

    had_admin = ROLE_ADMIN in target.role_names()

    if not had_admin or not target.is_active:
        return

    other_admins = db.scalar(
        select(User.id).where(
            User.id != target.id,
            User.is_active.is_(True),
            User.roles.any(Role.name == ROLE_ADMIN),
        )
    )

    if other_admins is None:
        raise LastAdminError(
            "This change would leave no active administrator."
        )


__all__ = [
    "InvalidRoleName",
    "LastAdminError",
    "SelfManagementError",
    "UsernameTaken",
    "UserNotFound",
    "create_user",
    "disable_user",
    "list_roles",
    "list_users",
    "update_user",
]
