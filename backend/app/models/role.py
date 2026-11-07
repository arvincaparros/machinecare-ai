"""Role model and the user_roles association.

Roles are database rows seeded by the a1 migration so the admin user
management endpoints can list them; the permissions each role carries
live in ``app.core.permissions`` and are code constants.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    SmallInteger,
    String,
    Table,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

# Many-to-many even though v1 assigns a single role per user, so a user
# can hold several roles later without another migration.
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        SmallInteger,
        ForeignKey("roles.id"),
        primary_key=True,
    ),
)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    users: Mapped[list["User"]] = relationship(
        secondary=user_roles,
        back_populates="roles",
        lazy="selectin",
    )
