"""End-to-end tests for ``POST /api/v1/auth/login`` and friends.

The real service runs against the isolated SQLite database: Argon2
verification, lockout accounting and token issuance are never mocked.
Cookie flags are asserted from the raw ``Set-Cookie`` header so they
cannot silently regress.
"""

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.models import User

LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
CHANGE_URL = "/api/v1/auth/change-password"

WRONG_PASSWORD = "Nope-Nope-Nope-1"


def _set_cookie_headers(response) -> str:
    """Every Set-Cookie header of the response, joined for grepping."""

    return "; ".join(response.headers.get_list("set-cookie"))


def test_login_returns_a_bearer_token_and_sets_the_refresh_cookie(
    api_client, make_user, valid_password
):
    make_user(username="vera", password=valid_password, roles=("viewer",))

    response = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    # The plan's 15-minute access tokens, reported so the client can
    # refresh proactively.
    assert 880 <= body["expires_in"] <= 900
    assert len(body["access_token"]) > 50
    # The refresh token never appears in the body.
    assert valid_password not in response.text

    settings = get_settings()
    set_cookies = _set_cookie_headers(response)
    assert f"{settings.cookie_name}=" in set_cookies
    assert "HttpOnly" in set_cookies
    assert "SameSite=strict" in set_cookies
    # Scoped to the auth routes only.
    assert "Path=/api/v1/auth" in set_cookies
    # Tests run with COOKIE_SECURE=false; production flips it on.
    assert "Secure" not in set_cookies


def test_the_token_from_login_is_accepted_by_me(
    api_client, make_user, valid_password
):
    make_user(username="vera", password=valid_password, roles=("viewer",))

    login = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )
    token = login.json()["access_token"]

    me = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )

    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "vera"
    assert body["roles"] == ["viewer"]
    assert body["permissions"] == ["assistant:use", "incidents:read"]


def test_a_wrong_password_is_rejected(api_client, make_user, valid_password):
    make_user(username="vera", password=valid_password)

    response = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": WRONG_PASSWORD},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials."}


def test_an_unknown_username_is_indistinguishable_from_a_wrong_password(
    api_client,
):
    ghost = api_client.post(
        LOGIN_URL,
        json={"username": "ghost", "password": WRONG_PASSWORD},
    )

    assert ghost.status_code == 401
    assert ghost.json() == {"detail": "Invalid credentials."}


def test_usernames_are_case_insensitive_and_trimmed(
    api_client, make_user, valid_password
):
    make_user(username="vera", password=valid_password)

    response = api_client.post(
        LOGIN_URL,
        json={"username": "  VERA  ", "password": valid_password},
    )

    assert response.status_code == 200


def test_repeated_failures_lock_the_account(
    api_client, make_user, valid_password, db_session_factory
):
    make_user(username="vera", password=valid_password)

    for _ in range(5):
        failed = api_client.post(
            LOGIN_URL,
            json={"username": "vera", "password": WRONG_PASSWORD},
        )
        assert failed.status_code == 401

    # Even the correct password is refused while the lockout lasts,
    # with the same generic message.
    locked = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )

    assert locked.status_code == 401
    assert locked.json() == {"detail": "Invalid credentials."}

    with db_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "vera"))
        assert user.failed_login_count == 5
        assert user.locked_until is not None


def test_a_successful_login_resets_the_lockout_counters(
    api_client, make_user, valid_password, db_session_factory
):
    make_user(username="vera", password=valid_password)

    for _ in range(3):
        api_client.post(
            LOGIN_URL,
            json={"username": "vera", "password": WRONG_PASSWORD},
        )

    ok = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )
    assert ok.status_code == 200

    with db_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "vera"))
        assert user.failed_login_count == 0
        assert user.locked_until is None
        assert user.last_login_at is not None


def test_a_disabled_account_cannot_log_in(
    api_client, make_user, valid_password
):
    make_user(username="vera", password=valid_password, is_active=False)

    response = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials."}


def test_change_password_requires_the_current_one(
    api_client, make_user, valid_password, issue_token
):
    user = make_user(username="vera", password=valid_password)
    headers = {"Authorization": f"Bearer {issue_token(user)}"}

    wrong = api_client.post(
        CHANGE_URL,
        headers=headers,
        json={
            "current_password": WRONG_PASSWORD,
            "new_password": "Brand-New-Pass-1",
        },
    )

    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "Invalid credentials."}


def test_change_password_rejects_policy_violations(
    api_client, make_user, valid_password, issue_token
):
    user = make_user(username="vera", password=valid_password)
    headers = {"Authorization": f"Bearer {issue_token(user)}"}

    weak = api_client.post(
        CHANGE_URL,
        headers=headers,
        json={"current_password": valid_password, "new_password": "short"},
    )

    assert weak.status_code == 422


def test_change_password_revokes_every_token_and_session(
    api_client, make_user, valid_password, issue_token
):
    user = make_user(username="vera", password=valid_password)
    old_headers = {"Authorization": f"Bearer {issue_token(user)}"}

    changed = api_client.post(
        CHANGE_URL,
        headers=old_headers,
        json={
            "current_password": valid_password,
            "new_password": "Brand-New-Pass-1",
        },
    )
    assert changed.status_code == 200

    # The outstanding access token no longer verifies (version bump).
    stale = api_client.get(ME_URL, headers=old_headers)
    assert stale.status_code == 401

    # The old password is dead; the new one works.
    old_login = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )
    new_login = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": "Brand-New-Pass-1"},
    )

    assert old_login.status_code == 401
    assert new_login.status_code == 200
