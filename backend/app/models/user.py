"""User account model for authentication.

Usernames are stored lowercased: the service layer normalizes on write,
which makes the plain unique constraint case-insensitive on both
PostgreSQL and the in-memory SQLite used by the test suite.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class User(Base):
    __tablename__ = "users"

    # BigInteger on PostgreSQL; Integer on SQLite, where only an exact
    # ``INTEGER PRIMARY KEY`` aliases the rowid and auto-generates ids.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )

    display_name: Mapped[str] = mapped_column(String(150), nullable=False)

    # Argon2id hash; never logged, never returned by any endpoint.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # Bumped on password change, role change and disable; every access
    # token carries the value it was issued with and is rejected the
    # moment it no longer matches.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )

    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles",
        back_populates="users",
        lazy="selectin",
    )

    refresh_sessions: Mapped[list["RefreshSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def role_names(self) -> list[str]:
        """The names of this user's roles, sorted for stable output."""

        return sorted(role.name for role in self.roles)
