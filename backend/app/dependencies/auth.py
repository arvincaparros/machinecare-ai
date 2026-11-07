"""FastAPI dependencies for authentication and authorization.

The chain is:

    bearer_scheme -> get_current_user -> require_permission(code)

``get_current_user`` decodes the access token, loads the user and
checks ``is_active`` and ``token_version`` on **every request**, so a
password change, role change or disable invalidates outstanding tokens
immediately. Authorization is always computed from the roles stored in
the database, never from the roles claim inside the token.

``HTTPBearer(auto_error=False)`` is used deliberately: FastAPI's
default error path answers 403 for a missing credential, while the
plan requires 401 with ``WWW-Authenticate: Bearer``.

Module-scope imports stay clear of ``app.db.database`` and
``app.models`` (both have import-time side effects); the handler
imports them lazily, like every other route in the API layer.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from sqlalchemy.orm import Session

from app.core.permissions import permissions_for_roles
from app.core.security import InvalidTokenError, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)

NOT_AUTHENTICATED_DETAIL = "Not authenticated."
FORBIDDEN_DETAIL = "You do not have permission to perform this action."


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, resolved from the database."""

    user_id: int
    session_id: str
    username: str
    display_name: str
    roles: tuple[str, ...]
    permissions: frozenset[str]

    def has_permission(self, code: str) -> bool:
        return code in self.permissions


def _not_authenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=NOT_AUTHENTICATED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_auth_db() -> Iterator[Session]:
    """Yield a session lazily, so importing this module stays side-effect
    free (``app.db.database`` reads its settings at import time)."""

    from app.db.database import get_db

    yield from get_db()


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[Session, Depends(get_auth_db)],
) -> Principal:
    """Resolve the caller from the bearer token, or raise 401."""

    from app.models import User

    if credentials is None:
        raise _not_authenticated()

    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _not_authenticated() from None

    user_id, session_id, token_version = _validate_claims(claims)

    user = db.get(User, user_id)

    if (
        user is None
        or not user.is_active
        or user.token_version != token_version
    ):
        # A disabled user or a stale token_version is treated exactly
        # like a missing credential: 401, prompting a refresh attempt.
        raise _not_authenticated()

    roles = tuple(user.role_names())

    return Principal(
        user_id=user.id,
        session_id=session_id,
        username=user.username,
        display_name=user.display_name,
        roles=roles,
        permissions=permissions_for_roles(roles),
    )


def _validate_claims(claims: dict) -> tuple[int, str, int]:
    """Extract and type-check the claims the dependency relies on."""

    subject = claims.get("sub")
    session_id = claims.get("sid")
    token_version = claims.get("ver")

    if not isinstance(subject, str) or not isinstance(session_id, str):
        raise _not_authenticated()

    if not isinstance(token_version, int):
        raise _not_authenticated()

    try:
        user_id = int(subject)
    except ValueError:
        raise _not_authenticated() from None

    return user_id, session_id, token_version


def require_permission(permission: str):
    """Dependency factory: valid login **and** the given permission."""

    def dependency(
        principal: Annotated[Principal, Depends(get_current_user)],
    ) -> Principal:
        if not principal.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )

        return principal

    return dependency


def require_any_permission(*permissions: str):
    """Dependency factory: passes with any one of the permissions."""

    def dependency(
        principal: Annotated[Principal, Depends(get_current_user)],
    ) -> Principal:
        if not any(
            principal.has_permission(code) for code in permissions
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )

        return principal

    return dependency


__all__ = [
    "NOT_AUTHENTICATED_DETAIL",
    "Principal",
    "bearer_scheme",
    "get_auth_db",
    "get_current_user",
    "require_any_permission",
    "require_permission",
]
