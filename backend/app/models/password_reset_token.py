"""One-time password reset tokens issued by administrators.

The token itself is shown once in the admin API response and handed to
the user out of band; only its SHA-256 digest is stored. A token is
single use: consuming it sets ``used_at``, and any later attempt with
the same token is rejected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def is_redeemable(self, now: datetime) -> bool:
        """True while the token is unused and unexpired.

        Naive datetimes (how SQLite stores timestamps) are read as UTC,
        matching the service layer's convention.
        """

        expires_at = self.expires_at

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        return self.used_at is None and expires_at > now
