"""Authentication endpoints: login, refresh, logout, me, passwords.

Routes (authentication-and-rbac-plan.md §3.1):

- ``POST /api/v1/auth/login``    public; returns the access token in the
  body and sets the refresh token as an HttpOnly cookie.
- ``POST /api/v1/auth/refresh``  cookie only; rotates the session.
- ``POST /api/v1/auth/logout``   access token; revokes that session.
- ``GET  /api/v1/auth/me``       access token; principal + permissions.
- ``POST /api/v1/auth/change-password``  access token + current password.
- ``POST /api/v1/auth/reset-password``   one-time admin-issued token.

Errors follow plan §5.2: bad credentials and locked accounts are the
same generic 401; a missing/expired/revoked session is a generic 401
``Session expired.``. Services are imported inside the handlers: they
pull in the models, and ``app.db.database`` has import-time side
effects, so module scope of the API layer must stay free of them.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.permissions import permissions_for_roles
from app.core.settings import get_settings
from app.dependencies.auth import Principal, get_auth_db, get_current_user
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    MessageResponse,
    ResetPasswordRequest,
    TokenResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)

INVALID_CREDENTIALS_DETAIL = "Invalid credentials."
SESSION_EXPIRED_DETAIL = "Session expired."
AUTH_UNAVAILABLE_DETAIL = (
    "Authentication is temporarily unavailable. Try again shortly."
)


def _credentials_error() -> HTTPException:
    return HTTPException(
        status_code=401, detail=INVALID_CREDENTIALS_DETAIL
    )


def _session_error() -> HTTPException:
    return HTTPException(status_code=401, detail=SESSION_EXPIRED_DETAIL)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()

    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.refresh_ttl_days * 24 * 60 * 60,
        path="/api/v1/auth",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()

    response.delete_cookie(
        key=settings.cookie_name,
        path="/api/v1/auth",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_auth_db),
) -> TokenResponse:
    """Verify credentials, start a refresh session, issue both tokens."""

    from app.services import audit_service, auth_service

    try:
        outcome = auth_service.login(
            db,
            username=body.username,
            password=body.password,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except auth_service.AccountLocked:
        audit_service.record_event(
            None,
            action=audit_service.ACTION_LOGIN_LOCKED,
            outcome=audit_service.OUTCOME_FAILURE,
            actor_username=body.username.strip().lower(),
            request_id=_request_id(request),
            ip_hash=auth_service.hash_ip(
                request.client.host if request.client else None
            ),
        )
        raise _credentials_error() from None
    except auth_service.InvalidCredentials:
        audit_service.record_event(
            None,
            action=audit_service.ACTION_LOGIN_FAILED,
            outcome=audit_service.OUTCOME_FAILURE,
            actor_username=body.username.strip().lower(),
            request_id=_request_id(request),
            ip_hash=auth_service.hash_ip(
                request.client.host if request.client else None
            ),
        )
        raise _credentials_error() from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Login failed unexpectedly.")
        raise HTTPException(
            status_code=500, detail=AUTH_UNAVAILABLE_DETAIL
        ) from None

    audit_service.record_event(
        None,
        action=audit_service.ACTION_LOGIN_SUCCEEDED,
        outcome=audit_service.OUTCOME_SUCCESS,
        actor_user_id=outcome.user_id,
        actor_username=outcome.username,
        request_id=_request_id(request),
        event_metadata={"session_id": outcome.session_id},
    )

    _set_refresh_cookie(response, outcome.refresh_token)

    return TokenResponse(
        access_token=outcome.access_token,
        expires_in=int(
            (outcome.access_expires_at - _utcnow()).total_seconds()
        ),
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_auth_db),
) -> TokenResponse:
    """Rotate the refresh session named by the cookie."""

    from app.services import audit_service, auth_service

    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)

    if not token:
        raise _session_error()

    try:
        outcome = auth_service.refresh(
            db,
            refresh_token=token,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except auth_service.RefreshReuseDetected:
        audit_service.record_event(
            None,
            action=audit_service.ACTION_REFRESH_REUSE_DETECTED,
            outcome=audit_service.OUTCOME_FAILURE,
            actor_user_id=None,
            request_id=_request_id(request),
            ip_hash=auth_service.hash_ip(
                request.client.host if request.client else None
            ),
        )
        raise _session_error() from None
    except auth_service.InvalidRefreshToken:
        raise _session_error() from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Token refresh failed unexpectedly.")
        raise HTTPException(
            status_code=500, detail=AUTH_UNAVAILABLE_DETAIL
        ) from None

    _set_refresh_cookie(response, outcome.refresh_token)

    return TokenResponse(
        access_token=outcome.access_token,
        expires_in=int(
            (outcome.access_expires_at - _utcnow()).total_seconds()
        ),
    )


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> MessageResponse:
    """Revoke the refresh session named by the cookie, then clear it."""

    from app.services import audit_service, auth_service

    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)

    try:
        auth_service.logout(db, user_id=principal.user_id,
                            refresh_token=token)
    except Exception:
        logger.exception("Logout failed unexpectedly.")
        raise HTTPException(
            status_code=500, detail=AUTH_UNAVAILABLE_DETAIL
        ) from None

    audit_service.record_event(
        None,
        action=audit_service.ACTION_LOGOUT,
        outcome=audit_service.OUTCOME_SUCCESS,
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        request_id=_request_id(request),
    )

    _clear_refresh_cookie(response)

    return MessageResponse(
        status="ok", message="Logged out. The session was revoked."
    )


@router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(get_current_user)) -> MeResponse:
    """Return the current principal with its effective permissions."""

    return MeResponse(
        user_id=principal.user_id,
        username=principal.username,
        display_name=principal.display_name,
        roles=list(principal.roles),
        permissions=sorted(permissions_for_roles(principal.roles)),
    )


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> MessageResponse:
    """Set a new password; kills every token and session of the user."""

    from app.services import audit_service, auth_service

    try:
        auth_service.change_password(
            db,
            user_id=principal.user_id,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except auth_service.InvalidCredentials:
        raise _credentials_error() from None
    except auth_service.WeakPassword as error:
        raise HTTPException(status_code=422, detail=error.message) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Password change failed unexpectedly.")
        raise HTTPException(
            status_code=500, detail=AUTH_UNAVAILABLE_DETAIL
        ) from None

    audit_service.record_event(
        None,
        action=audit_service.ACTION_PASSWORD_CHANGED,
        outcome=audit_service.OUTCOME_SUCCESS,
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        request_id=_request_id(request),
    )

    return MessageResponse(
        status="ok",
        message=(
            "Password changed. All sessions were revoked; log in again."
        ),
    )


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_auth_db),
) -> MessageResponse:
    """Consume a one-time admin-issued token and set a new password."""

    from app.services import audit_service, auth_service

    try:
        username = auth_service.reset_password(
            db, token=body.token, new_password=body.new_password
        )
    except auth_service.InvalidResetToken:
        raise HTTPException(
            status_code=400,
            detail=(
                "This reset token is invalid, already used, or expired. "
                "Ask an administrator for a new one."
            ),
        ) from None
    except auth_service.WeakPassword as error:
        raise HTTPException(status_code=422, detail=error.message) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Password reset failed unexpectedly.")
        raise HTTPException(
            status_code=500, detail=AUTH_UNAVAILABLE_DETAIL
        ) from None

    audit_service.record_event(
        None,
        action=audit_service.ACTION_PASSWORD_RESET,
        outcome=audit_service.OUTCOME_SUCCESS,
        actor_username=username,
        request_id=_request_id(request),
    )

    return MessageResponse(
        status="ok",
        message="Password set. You can now log in with the new password.",
    )
