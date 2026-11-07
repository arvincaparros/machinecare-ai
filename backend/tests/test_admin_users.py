"""Admin user-management endpoint tests.

Only callers holding ``users:manage`` reach these routes (enforced by
the RBAC suite); here an administrator exercises the account lifecycle:
create, list, update, disable and the one-time reset-token flow.
"""

import pytest

from app.core.settings import get_settings

USERS_URL = "/api/v1/admin/users"
ROLES_URL = "/api/v1/admin/roles"
RESET_URL = "/api/v1/auth/reset-password"
LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"

NEW_PASSWORD = "Brand-New-Pass-1"


def _create(client, username="nova", roles=("viewer",), password=None):
    from tests.conftest import TEST_PASSWORD

    return client.post(
        USERS_URL,
        json={
            "username": username,
            "display_name": username.title(),
            "password": password or TEST_PASSWORD,
            "roles": list(roles),
        },
    )


def test_an_admin_creates_an_account_and_the_hash_stays_hidden(
    auth_client, valid_password
):
    with auth_client() as admin:
        response = _create(admin, username="nova", password=valid_password)

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "nova"
    assert body["display_name"] == "Nova"
    assert body["is_active"] is True
    assert body["roles"] == ["viewer"]
    assert "password" not in body
    assert valid_password not in response.text
    assert "argon" not in response.text.lower()


def test_created_usernames_are_normalised_and_unique(
    auth_client, valid_password
):
    with auth_client() as admin:
        first = _create(admin, username="Nova")
        duplicate = _create(admin, username="NOVA")

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert "taken" in duplicate.json()["detail"].lower()


def test_a_weak_password_is_refused(auth_client):
    with auth_client() as admin:
        weak = _create(admin, username="nova", password="short")

    assert weak.status_code == 422


def test_an_unknown_role_is_refused(auth_client):
    with auth_client() as admin:
        response = _create(admin, username="nova", roles=("wizard",))

    assert response.status_code == 422
    assert "wizard" in response.json()["detail"]


def test_listing_users_never_exposes_hashes(auth_client, valid_password):
    with auth_client() as admin:
        _create(admin, username="nova")
        response = admin.get(USERS_URL)

    assert response.status_code == 200
    usernames = [user["username"] for user in response.json()["users"]]
    assert "admin" in usernames
    assert "nova" in usernames
    assert "argon" not in response.text.lower()


def test_updating_a_role_bumps_the_token_version(
    auth_client, api_client, make_user, issue_token, valid_password
):
    user = make_user(username="vera", password=valid_password)
    old_headers = {"Authorization": f"Bearer {issue_token(user)}"}

    before = api_client.get(ME_URL, headers=old_headers)
    assert before.status_code == 200

    with auth_client() as admin:
        response = admin.patch(
            f"{USERS_URL}/{user.id}", json={"roles": ["viewer"]}
        )

    assert response.status_code == 200
    assert response.json()["roles"] == ["viewer"]

    # The demoted account's outstanding token stops working at once;
    # authorization is recomputed from the database on every request.
    after = api_client.get(ME_URL, headers=old_headers)
    assert after.status_code == 401


def test_disabling_an_account_kills_its_tokens_and_logins(
    auth_client, api_client, make_user, issue_token, valid_password
):
    user = make_user(
        username="vera", password=valid_password, roles=("viewer",)
    )
    headers = {"Authorization": f"Bearer {issue_token(user)}"}

    before = api_client.get(ME_URL, headers=headers)
    assert before.status_code == 200

    with auth_client() as admin:
        response = admin.post(f"{USERS_URL}/{user.id}/disable")

    assert response.status_code == 200
    assert response.json()["is_active"] is False

    # Outstanding access tokens die with the account...
    after = api_client.get(ME_URL, headers=headers)
    assert after.status_code == 401

    # ...and so does the password.
    login = api_client.post(
        LOGIN_URL, json={"username": "vera", "password": valid_password}
    )
    assert login.status_code == 401


def test_an_admin_cannot_disable_their_own_account(auth_client):
    with auth_client() as admin:
        me = admin.get(ME_URL)
        own_id = me.json()["user_id"]

        response = admin.post(f"{USERS_URL}/{own_id}/disable")

    assert response.status_code == 409
    assert "own account" in response.json()["detail"]


def test_an_admin_cannot_change_their_own_roles(auth_client):
    with auth_client() as admin:
        me = admin.get(ME_URL)
        own_id = me.json()["user_id"]

        response = admin.patch(
            f"{USERS_URL}/{own_id}", json={"roles": ["viewer"]}
        )

    assert response.status_code == 409
    assert "own account" in response.json()["detail"]


def test_the_role_list_is_the_fixed_set(auth_client):
    with auth_client() as admin:
        response = admin.get(ROLES_URL)

    assert response.status_code == 200
    names = {role["name"] for role in response.json()["roles"]}
    assert names == {"admin", "maintenance_engineer", "viewer"}


def test_unknown_user_ids_answer_404(auth_client):
    with auth_client() as admin:
        patch = admin.patch(f"{USERS_URL}/9999", json={"is_active": True})
        disable = admin.post(f"{USERS_URL}/9999/disable")
        token = admin.post(f"{USERS_URL}/9999/reset-password")

    assert patch.status_code == 404
    assert disable.status_code == 404
    assert token.status_code == 404


def test_the_reset_token_flow_works_exactly_once(
    auth_client, api_client, make_user, valid_password
):
    user = make_user(username="vera", password=valid_password)

    with auth_client() as admin:
        issued = admin.post(f"{USERS_URL}/{user.id}/reset-password")

    assert issued.status_code == 200
    body = issued.json()
    assert 0 < body["expires_in"] <= get_settings().reset_token_ttl_minutes * 60
    assert valid_password not in issued.text

    reset = api_client.post(
        RESET_URL,
        json={"token": body["reset_token"], "new_password": NEW_PASSWORD},
    )
    assert reset.status_code == 200

    # The new password works; the old one is dead.
    old_login = api_client.post(
        LOGIN_URL, json={"username": "vera", "password": valid_password}
    )
    new_login = api_client.post(
        LOGIN_URL, json={"username": "vera", "password": NEW_PASSWORD}
    )
    assert old_login.status_code == 401
    assert new_login.status_code == 200

    # A reset token is single-use.
    reuse = api_client.post(
        RESET_URL,
        json={"token": body["reset_token"], "new_password": "Another-Pass-1"},
    )
    assert reuse.status_code == 400


def test_a_weak_reset_password_is_refused(
    auth_client, api_client, make_user, valid_password
):
    user = make_user(username="vera", password=valid_password)

    with auth_client() as admin:
        issued = admin.post(f"{USERS_URL}/{user.id}/reset-password")

    reset = api_client.post(
        RESET_URL,
        json={"token": issued.json()["reset_token"], "new_password": "short"},
    )

    assert reset.status_code == 422
