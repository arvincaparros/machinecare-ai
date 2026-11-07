"""Request and response schemas for admin user management."""

from datetime import datetime

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """Body of POST /api/v1/admin/users."""

    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=150)
    # Raw password; validated by the policy in app.core.password_policy.
    password: str = Field(min_length=1, max_length=256)
    roles: list[str] = Field(min_length=1)


class UserUpdate(BaseModel):
    """Body of PATCH /api/v1/admin/users/{id}; fields are optional."""

    display_name: str | None = Field(default=None, min_length=1, max_length=150)
    roles: list[str] | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class UserOut(BaseModel):
    """A managed user. Never contains the password hash."""

    id: int
    username: str
    display_name: str
    is_active: bool
    roles: list[str]
    created_at: datetime
    last_login_at: datetime | None
    locked_until: datetime | None


class UserListResponse(BaseModel):
    users: list[UserOut]


class ResetTokenResponse(BaseModel):
    """The one-time reset token, shown exactly once in this response."""

    reset_token: str
    expires_in: int


class RoleOut(BaseModel):
    id: int
    name: str
    description: str | None


class RoleListResponse(BaseModel):
    roles: list[RoleOut]
