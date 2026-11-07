"""Append-only audit log for authentication and administration events.

Phase 1 writes: login success/failure/lockout, logout, refresh reuse
detection, password change/reset and every user administration change.
The log is only ever appended to; nothing in this phase reads it back
(the Phase 6 audit API will).

Rows for authentication events are written in their own transaction so
a failed audit write can never block a legitimate login; rows for
administrative mutations share the mutation's transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.database import Base

# JSONB on PostgreSQL (indexable, per the plan); plain JSON on the
# in-memory SQLite the test suite runs on.
AuditMetadata = JSON().with_variant(JSONB(), "postgresql")

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"


class AuditLog(Base):
    __tablename__ = "audit_log"

    # Integer on SQLite so inserts auto-generate ids there too; the
    # PostgreSQL type stays BIGINT (see app/models/user.py).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # NULL for, e.g., a failed login of an unknown user.
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )

    # Captured at attempt time, so failed attempts are attributable.
    actor_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # Dotted action name, e.g. auth.login.failed, user.role.changed.
    action: Mapped[str] = mapped_column(String(64), nullable=False)

    resource_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    resource_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    # Correlation id from the request-id middleware.
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Attribute is ``event_metadata`` because ``metadata`` is reserved
    # by SQLAlchemy; the column name is ``metadata`` as designed.
    # Never contains secrets: no passwords, hashes or tokens.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", AuditMetadata, nullable=False
    )

    # success | failure
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
