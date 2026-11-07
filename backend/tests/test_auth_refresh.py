"""Refresh-token rotation, expiry and reuse-detection tests.

The refresh token travels only in the HttpOnly cookie, so each request
passes it explicitly (the client cookie jar is emptied after login to
keep every request's credentials unambiguous).
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.security import refresh_session_expiry
from app.core.settings import get_settings
from app.models import RefreshSession, User

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
ME_URL = "/api/v1/auth/me"


def _refresh_cookie(response) -> str:
    """The raw refresh token from a login/refresh Set-Cookie header."""

    settings = get_settings()

    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{settings.cookie_name}="):
            return header.split(";", 1)[0].split("=", 1)[1]

    raise AssertionError("no refresh cookie was set")


@pytest.fixture
def logged_in(api_client, make_user, valid_password):
    """Create a viewer, log in, and hand back both credentials."""

    make_user(username="vera", password=valid_password, roles=("viewer",))

    response = api_client.post(
        LOGIN_URL,
        json={"username": "vera", "password": valid_password},
    )
    assert response.status_code == 200

    refresh_token = _refresh_cookie(response)
    api_client.cookies.delete(get_settings().cookie_name)

    return {
        "access": response.json()["access_token"],
        "refresh": refresh_token,
    }


def test_refresh_rotates_the_session_and_the_new_token_works(
    api_client, db_session_factory, logged_in
):
    response = api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: logged_in["refresh"]}
    )

    assert response.status_code == 200
    new_access = response.json()["access_token"]

    me = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {new_access}"}
    )
    assert me.status_code == 200

    # The rotation chain is stored: the old session is revoked and the
    # new one points at it.
    with db_session_factory() as db:
        sessions = db.scalars(
            select(RefreshSession).order_by(RefreshSession.issued_at)
        ).all()

        assert len(sessions) == 2
        assert sessions[0].revoked_at is not None
        assert sessions[1].revoked_at is None
        assert sessions[1].rotated_from == sessions[0].id


def test_refresh_without_a_cookie_is_a_generic_401(api_client):
    response = api_client.post(REFRESH_URL)

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired."}


def test_an_unrecognised_cookie_is_a_generic_401(api_client):
    response = api_client.post(
        REFRESH_URL,
        cookies={get_settings().cookie_name: "not-a-real-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired."}


def test_an_expired_refresh_session_is_rejected(
    api_client, db_session_factory, logged_in
):
    with db_session_factory() as db:
        session = db.scalar(select(RefreshSession))
        session.expires_at = refresh_session_expiry() - timedelta(days=20)
        db.commit()

    response = api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: logged_in["refresh"]}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired."}


def test_a_disabled_user_cannot_refresh(
    api_client, db_session_factory, logged_in
):
    with db_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "vera"))
        user.is_active = False
        db.commit()

    response = api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: logged_in["refresh"]}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired."}


def test_reusing_a_rotated_token_revokes_the_whole_family(
    api_client, db_session_factory, logged_in
):
    first = api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: logged_in["refresh"]}
    )
    assert first.status_code == 200

    current_refresh = _refresh_cookie(first)
    api_client.cookies.delete(get_settings().cookie_name)

    # Presenting the already-rotated token again is treated as theft:
    # 401 for the attacker, and every session of the user is revoked.
    replay = api_client.post(
        REFRESH_URL,
        cookies={get_settings().cookie_name: logged_in["refresh"]},
    )
    assert replay.status_code == 401
    assert replay.json() == {"detail": "Session expired."}

    # The legitimate client's newest session is dead too.
    victim = api_client.post(
        REFRESH_URL, cookies={get_settings().cookie_name: current_refresh}
    )
    assert victim.status_code == 401

    with db_session_factory() as db:
        active = db.scalars(
            select(RefreshSession).where(RefreshSession.revoked_at.is_(None))
        ).all()
        assert active == []
