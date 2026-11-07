"""Admin endpoints for account and role management.

Every route requires the ``users:manage`` permission, checked by the
FastAPI dependency — the frontend never decides authorization. Audit
rows ride on the mutation's session so they cannot succeed while the
change fails, and vice versa.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.permissions import USERS_MANAGE
from app.core.settings import get_settings
from app.dependencies.auth import Principal, get_auth_db
from app.dependencies.auth import require_permission
from app.schemas.users import (
    ResetTokenResponse,
    RoleListResponse,
    RoleOut,
    UserCreate,
    UserListResponse,
    UserOut,
    UserUpdate,
)

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin-users"],
    dependencies=[Depends(require_permission(USERS_MANAGE))],
)

ADMIN_UNAVAILABLE_DETAIL = (
    "User management is temporarily unavailable. Try again shortly."
)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _acting_admin(db: Session, principal: Principal) -> "User":
    """The caller's persisted row; the services audit real users."""

    from app.models import User

    acting_admin = db.get(User, principal.user_id)

    if acting_admin is None:  # pragma: no cover - auth guarantees it
        raise HTTPException(status_code=401, detail="Not authenticated.")

    return acting_admin


def _to_user_out(user) -> UserOut:  # noqa: ANN001 (ORM instance)
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_active=user.is_active,
        roles=user.role_names(),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        locked_until=user.locked_until,
    )


@router.get("/users", response_model=UserListResponse)
def list_users(
    principal: Principal = Depends(require_permission(USERS_MANAGE)),
    db: Session = Depends(get_auth_db),
) -> UserListResponse:
    """List every managed account."""

    from app.services import user_admin_service

    try:
        users = user_admin_service.list_users(db)
    except Exception:
        logger.exception("Listing users failed.")
        raise HTTPException(
            status_code=500, detail=ADMIN_UNAVAILABLE_DETAIL
        ) from None

    return UserListResponse(
        users=[_to_user_out(user) for user in users]
    )


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    responses={
        409: {"description": "Username already taken."},
        422: {"description": "Invalid roles or weak password."},
    },
)
def create_user(
    body: UserCreate,
    request: Request,
    principal: Principal = Depends(require_permission(USERS_MANAGE)),
    db: Session = Depends(get_auth_db),
) -> UserOut:
    """Provision an account. Passwords never appear in any response."""

    from app.services import auth_service, user_admin_service

    try:
        user = user_admin_service.create_user(
            db,
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            role_names=body.roles,
            acting_admin=_acting_admin(db, principal),
            request_id=_request_id(request),
        )
    except user_admin_service.UsernameTaken:
        raise HTTPException(
            status_code=409,
            detail="That username is already taken.",
        ) from None
    except user_admin_service.InvalidRoleName as error:
        raise HTTPException(
            status_code=422, detail=error.message
        ) from None
    except auth_service.WeakPassword as error:
        raise HTTPException(
            status_code=422, detail=error.message
        ) from None
    except Exception:
        logger.exception("Creating the user failed.")
        raise HTTPException(
            status_code=500, detail=ADMIN_UNAVAILABLE_DETAIL
        ) from None

    return _to_user_out(user)


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    responses={
        404: {"description": "User not found."},
        409: {"description": "Self-management or last-admin guard."},
        422: {"description": "Invalid role name."},
    },
)
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    principal: Principal = Depends(require_permission(USERS_MANAGE)),
    db: Session = Depends(get_auth_db),
) -> UserOut:
    """Change display name, roles or activation of one account."""

    from app.services import user_admin_service

    try:
        user = user_admin_service.update_user(
            db,
            user_id=user_id,
            acting_admin=_acting_admin(db, principal),
            display_name=body.display_name,
            role_names=body.roles,
            is_active=body.is_active,
            request_id=_request_id(request),
        )
    except user_admin_service.UserNotFound:
        raise HTTPException(
            status_code=404, detail="User not found."
        ) from None
    except user_admin_service.SelfManagementError as error:
        raise HTTPException(
            status_code=409, detail=error.message
        ) from None
    except user_admin_service.LastAdminError as error:
        raise HTTPException(
            status_code=409, detail=error.message
        ) from None
    except user_admin_service.InvalidRoleName as error:
        raise HTTPException(
            status_code=422, detail=error.message
        ) from None
    except Exception:
        logger.exception("Updating the user failed.")
        raise HTTPException(
            status_code=500, detail=ADMIN_UNAVAILABLE_DETAIL
        ) from None

    return _to_user_out(user)


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetTokenResponse,
    responses={
        404: {"description": "User not found."},
        409: {"description": "User is disabled."},
    },
)
def issue_reset_token(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_permission(USERS_MANAGE)),
    db: Session = Depends(get_auth_db),
) -> ResetTokenResponse:
    """Issue a one-time reset token, shown once, handed over out of band.

    No email delivery in Phase 1 (recorded decision); the token is the
    response body and is stored only as a digest.
    """

    from app.services import audit_service, auth_service

    try:
        token, expires_at = auth_service.issue_reset_token(
            db, user_id=user_id
        )
    except LookupError:
        raise HTTPException(
            status_code=404, detail="User not found."
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail=(
                "This account is disabled. Enable it before issuing "
                "a reset token."
            ),
        ) from None
    except Exception:
        logger.exception("Issuing the reset token failed.")
        raise HTTPException(
            status_code=500, detail=ADMIN_UNAVAILABLE_DETAIL
        ) from None

    audit_service.record_event(
        None,
        action=audit_service.ACTION_RESET_TOKEN_ISSUED,
        outcome=audit_service.OUTCOME_SUCCESS,
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        resource_type="user",
        resource_id=user_id,
        request_id=_request_id(request),
    )

    settings = get_settings()

    return ResetTokenResponse(
        reset_token=token,
        expires_in=int(
            max(
                0,
                (
                    expires_at - auth_service._utcnow()
                ).total_seconds(),
            )
        ),
    )


@router.post(
    "/users/{user_id}/disable",
    response_model=UserOut,
    responses={
        404: {"description": "User not found."},
        409: {"description": "Self-disable or last-admin guard."},
    },
)
def disable_user(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_permission(USERS_MANAGE)),
    db: Session = Depends(get_auth_db),
) -> UserOut:
    """Deactivate an account and invalidate all of its tokens."""

    from app.services import user_admin_service

    try:
        user = user_admin_service.disable_user(
            db,
            user_id=user_id,
            acting_admin=_acting_admin(db, principal),
            request_id=_request_id(request),
        )
    except user_admin_service.UserNotFound:
        raise HTTPException(
            status_code=404, detail="User not found."
        ) from None
    except user_admin_service.SelfManagementError as error:
        raise HTTPException(
            status_code=409, detail=error.message
        ) from None
    except user_admin_service.LastAdminError as error:
        raise HTTPException(
            status_code=409, detail=error.message
        ) from None
    except Exception:
        logger.exception("Disabling the user failed.")
        raise HTTPException(
            status_code=500, detail=ADMIN_UNAVAILABLE_DETAIL
        ) from None

    return _to_user_out(user)


@router.get("/roles", response_model=RoleListResponse)
def list_roles(
    principal: Principal = Depends(require_permission(USERS_MANAGE)),
    db: Session = Depends(get_auth_db),
) -> RoleListResponse:
    """The fixed role set available for assignment."""

    from app.services import user_admin_service

    try:
        roles = user_admin_service.list_roles(db)
    except Exception:
        logger.exception("Listing roles failed.")
        raise HTTPException(
            status_code=500, detail=ADMIN_UNAVAILABLE_DETAIL
        ) from None

    return RoleListResponse(
        roles=[
            RoleOut(
                id=role.id, name=role.name, description=role.description
            )
            for role in roles
        ]
    )
