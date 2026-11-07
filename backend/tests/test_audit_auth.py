"""Audit-log tests for the authentication flows.

Auth events are written in their own transaction through
``SessionLocal``; the ``audit_events`` fixture points that name at the
isolated database so the rows are observable. The assertions double as
a secrets sweep: no password, hash or token may reach any column.
"""

import json

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.services.audit_service import (
    ACTION_LOGIN_FAILED,
    ACTION_LOGIN_LOCKED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_LOGOUT,
    ACTION_PASSWORD_CHANGED,
    ACTION_REFRESH_REUSE_DETECTED,
)

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"
CHANGE_URL = "/api/v1/auth/change-password"


def _rows(db, action):
    return list(
        db.scalars(select(AuditLog).where(AuditLog.action == action)).all()
    )


def _refresh_cookie(response) -> str:
    from app.core.settings import get_settings

    settings = get_settings()

    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{settings.cookie_name}="):
            return header.split(";", 1)[0].split("=", 1)[1]

    raise AssertionError("no refresh cookie was set")


def test_a_failed_login_is_audited_with_the_normalised_username(
    api_client, audit_events, db_session_factory, make_user, valid_password
):
    marker = "Marker-Secret-99"
    make_user(username="vera", password=valid_password)

    api_client.post(LOGIN_URL, json={"username": "  VERA ", "password": marker})

    with db_session_factory() as db:
        rows = _rows(db, ACTION_LOGIN_FAILED)

    assert len(rows) == 1
    assert rows[0].outcome == "failure"
    assert rows[0].actor_username == "vera"
    assert rows[0].actor_user_id is None  # never resolved on failure


def test_a_successful_login_is_audited_with_the_actor_and_session(
    api_client, audit_events, db_session_factory, make_user, valid_password
):
    make_user(username="vera", password=valid_password)

    response = api_client.post(
        LOGIN_URL, json={"username": "vera", "password": valid_password}
    )
    assert response.status_code == 200

    with db_session_factory() as db:
        user_id = db.scalars(
            select(AuditLog.actor_user_id).where(
                AuditLog.action == ACTION_LOGIN_SUCCEEDED
            )
        ).first()
        metadata = db.scalars(
            select(AuditLog.event_metadata).where(
                AuditLog.action == ACTION_LOGIN_SUCCEEDED
            )
        ).first()

    assert isinstance(user_id, int)
    assert "session_id" in (metadata or {})


def test_lockout_reaches_the_audit_log(
    api_client, audit_events, db_session_factory, make_user, valid_password
):
    make_user(username="vera", password=valid_password)

    for _ in range(6):
        api_client.post(
            LOGIN_URL, json={"username": "vera", "password": "Nope-Nope-1"}
        )

    with db_session_factory() as db:
        locked = _rows(db, ACTION_LOGIN_LOCKED)

    assert len(locked) == 1  # only the attempt made while locked


def test_logout_and_password_change_are_audited(
    api_client,
    audit_events,
    db_session_factory,
    make_user,
    issue_token,
    valid_password,
):
    user = make_user(username="vera", password=valid_password)
    headers = {"Authorization": f"Bearer {issue_token(user)}"}

    api_client.post(LOGOUT_URL, headers=headers)
    api_client.post(
        CHANGE_URL,
        headers=headers,
        json={
            "current_password": valid_password,
            "new_password": "Brand-New-Pass-1",
        },
    )

    with db_session_factory() as db:
        assert len(_rows(db, ACTION_LOGOUT)) == 1
        assert len(_rows(db, ACTION_PASSWORD_CHANGED)) == 1


def test_refresh_reuse_detection_is_audited(
    api_client,
    audit_events,
    db_session_factory,
    make_user,
    valid_password,
):
    from app.core.settings import get_settings

    make_user(username="vera", password=valid_password)

    login = api_client.post(
        LOGIN_URL, json={"username": "vera", "password": valid_password}
    )
    original = _refresh_cookie(login)
    api_client.cookies.delete(get_settings().cookie_name)

    first = api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: original}
    )
    assert first.status_code == 200
    api_client.cookies.delete(get_settings().cookie_name)

    # Replay the rotated token: theft is assumed and audited.
    api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: original}
    )

    with db_session_factory() as db:
        rows = _rows(db, ACTION_REFRESH_REUSE_DETECTED)

    assert len(rows) == 1
    assert rows[0].outcome == "failure"


def test_no_secret_material_reaches_any_audit_column(
    api_client,
    audit_events,
    db_session_factory,
    make_user,
    valid_password,
):
    marker = "Marker-Secret-99"
    make_user(username="vera", password=valid_password)

    api_client.post(LOGIN_URL, json={"username": "vera", "password": marker})
    api_client.post(
        LOGIN_URL, json={"username": "vera", "password": valid_password}
    )

    with db_session_factory() as db:
        rows = list(db.scalars(select(AuditLog)).all())

    assert rows, "expected audit rows from the two logins"

    for row in rows:
        blob = " ".join(
            [
                str(row.actor_username or ""),
                str(row.action or ""),
                str(row.resource_type or ""),
                str(row.resource_id or ""),
                str(row.request_id or ""),
                str(row.ip_hash or ""),
                json.dumps(row.event_metadata, default=str),
                str(row.outcome or ""),
            ]
        )

        assert marker not in blob
        assert valid_password not in blob
        assert "$argon2" not in blob


def test_ip_addresses_are_stored_only_as_digests(
    api_client, audit_events, db_session_factory, make_user, valid_password
):
    make_user(username="vera", password=valid_password)

    api_client.post(
        LOGIN_URL, json={"username": "vera", "password": "Nope-Nope-1"}
    )

    with db_session_factory() as db:
        rows = list(db.scalars(select(AuditLog)).all())

    assert rows

    for row in rows:
        if row.ip_hash is not None:
            assert len(row.ip_hash) == 64
            assert "testclient" not in row.ip_hash
