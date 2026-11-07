"""Request and response schemas for the authentication endpoints."""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Body of POST /api/v1/auth/login."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """The access token issued by login and refresh.

    The refresh token is never in this body: it travels only in the
    HttpOnly cookie.
    """

    access_token: str
    token_type: str = "bearer"
    # Seconds until the access token expires, so the client can refresh
    # proactively instead of learning from 401 responses.
    expires_in: int


class MeResponse(BaseModel):
    """GET /api/v1/auth/me — the current principal and permissions."""

    user_id: int
    username: str
    display_name: str
    roles: list[str]
    permissions: list[str]


class ChangePasswordRequest(BaseModel):
    """Body of POST /api/v1/auth/change-password (self-service)."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ResetPasswordRequest(BaseModel):
    """Body of POST /api/v1/auth/reset-password (with admin token)."""

    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class MessageResponse(BaseModel):
    """Small acknowledgement body for logout and password endpoints."""

    status: str
    message: str
