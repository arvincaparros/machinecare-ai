"""Refresh session model: the revocable unit behind the refresh cookie.

Only the SHA-256 digest of the opaque refresh token is stored, so a
database leak does not yield usable cookies. ``rotated_from`` links a
session to the session it replaced during rotation; a refresh attempt
with an already-rotated or revoked token is treated as theft and
revokes every session of that user.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # SHA-256 hex of the opaque refresh token; unique for lookup.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Set when this session was replaced by rotation or revoked by
    # logout, a disable or reuse detection.
    rotated_from: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user_agent: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Never store the raw IP; a salted digest only allows correlating
    # sessions for abuse detection.
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship(back_populates="refresh_sessions")

    def is_active(self, now: datetime) -> bool:
        """True while the session is neither revoked nor expired."""

        return self.revoked_at is None and self.expires_at > now
