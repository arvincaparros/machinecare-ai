"""Audit event writer for authentication and administration.

Two write modes, matching the plan's durability rule:

- Authentication events (login failures, logout, reuse detection) are
  recorded with their **own** transaction: a failing audit write is
  logged and swallowed, because it must never block or fail the
  request it observed.
- Administrative mutations (user created/updated/disabled, role
  changed, password reset issued) pass the mutation's session: the row
  commits or rolls back **with** the mutation, failing closed.

Secrets never go into ``event_metadata``: no passwords, no hashes, no
tokens. Callers pass already-safe dictionaries.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import OUTCOME_FAILURE, OUTCOME_SUCCESS, AuditLog

logger = logging.getLogger(__name__)

# Action names (authentication-and-rbac-plan.md §7).
ACTION_LOGIN_SUCCEEDED = "auth.login.succeeded"
ACTION_LOGIN_FAILED = "auth.login.failed"
ACTION_LOGIN_LOCKED = "auth.login.locked"
ACTION_LOGOUT = "auth.logout"
ACTION_REFRESH_REUSE_DETECTED = "auth.refresh.reuse_detected"
ACTION_PASSWORD_CHANGED = "auth.password.changed"
ACTION_PASSWORD_RESET = "auth.password.reset"
ACTION_RESET_TOKEN_ISSUED = "auth.password.reset_issued"
ACTION_USER_CREATED = "user.created"
ACTION_USER_UPDATED = "user.updated"
ACTION_USER_DISABLED = "user.disabled"
ACTION_USER_ROLE_CHANGED = "user.role.changed"


def record_event(
    db: Session | None,
    *,
    action: str,
    outcome: str,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    ip_hash: str | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> None:
    """Append one audit row.

    With ``db=None`` the row is written in its own transaction and any
    failure is logged, never raised. With a session the row joins the
    caller's transaction: an audit failure then fails the mutation too,
    which is the intended fail-closed behaviour for admin changes.
    """

    row = AuditLog(
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        action=action,
        outcome=(outcome or OUTCOME_FAILURE),
        resource_type=resource_type,
        resource_id=(
            str(resource_id) if resource_id is not None else None
        ),
        request_id=request_id,
        ip_hash=ip_hash,
        event_metadata=event_metadata or {},
    )

    if db is None:
        _record_in_own_transaction(row)
        return

    db.add(row)


def _record_in_own_transaction(row: AuditLog) -> None:
    """Commit the row standalone, logging instead of raising."""

    from app.db.database import SessionLocal

    try:
        with SessionLocal() as session:
            session.add(row)
            session.commit()
    except Exception:
        # Authentication events must not fail the request that
        # produced them; the row is lost, the log line is the record.
        logger.exception(
            "Failed to write the audit event %s.", row.action
        )


__all__ = [
    "ACTION_LOGIN_FAILED",
    "ACTION_LOGIN_LOCKED",
    "ACTION_LOGIN_SUCCEEDED",
    "ACTION_LOGOUT",
    "ACTION_PASSWORD_CHANGED",
    "ACTION_PASSWORD_RESET",
    "ACTION_REFRESH_REUSE_DETECTED",
    "ACTION_RESET_TOKEN_ISSUED",
    "ACTION_USER_CREATED",
    "ACTION_USER_DISABLED",
    "ACTION_USER_ROLE_CHANGED",
    "ACTION_USER_UPDATED",
    "OUTCOME_FAILURE",
    "OUTCOME_SUCCESS",
    "record_event",
]
