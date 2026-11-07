"""Guard tests: every protected route refuses unauthenticated callers.

The whole protected surface is enumerated here on purpose: a new route
added without a permission dependency makes this file fail. Bearer
routes answer 401 with ``WWW-Authenticate: Bearer``; the cookie-only
refresh route answers with the same generic session-expired 401 the
client already knows how to react to.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.settings import get_settings
from app.core.security import create_access_token

ME_URL = "/api/v1/auth/me"

IMPORT_FILE = ("incidents.csv", b"header\n", "text/csv")

# (method, url, json body, multipart kind) for every protected route.
# The bodies are minimal but well-formed: the only failure being tested
# here is the missing credential, never request validation.
PROTECTED_ROUTES = [
    ("get", "/api/v1/auth/me", None),
    ("post", "/api/v1/auth/logout", None),
    (
        "post",
        "/api/v1/auth/change-password",
        {"current_password": "Whatever-One-1", "new_password": "Whatever-Two-2"},
    ),
    (
        "post",
        "/api/v1/ask",
        {"question": "Why did the AOI camera drift?"},
    ),
    ("post", "/api/v1/ask-with-files", "question-form"),
    ("get", "/api/v1/incidents/import/template.xlsx", None),
    ("get", "/api/v1/incidents/import/machines", None),
    ("post", "/api/v1/incidents/import/preview", "import-file"),
    ("post", "/api/v1/incidents/import", "import-commit"),
    ("post", "/api/v1/incidents/import/reindex", None),
    ("get", "/api/v1/admin/users", None),
    (
        "post",
        "/api/v1/admin/users",
        {
            "username": "someone",
            "display_name": "Someone",
            "password": "Whatever-One-1",
            "roles": ["viewer"],
        },
    ),
    ("patch", "/api/v1/admin/users/1", {}),
    ("post", "/api/v1/admin/users/1/reset-password", None),
    ("post", "/api/v1/admin/users/1/disable", None),
    ("get", "/api/v1/admin/roles", None),
]

# The refresh route is protected by the cookie, not the bearer header.
COOKIE_ROUTES = [
    ("post", "/api/v1/auth/refresh", None),
]


def _call(api_client, method, url, body):
    if body == "question-form":
        return api_client.post(
            url,
            data={"question": "Why?"},
            files={"file": ("note.txt", b"camera drifted", "text/plain")},
        )

    if body == "import-file":
        return api_client.post(url, files={"file": IMPORT_FILE})

    if body == "import-commit":
        return api_client.post(
            url,
            files={"file": IMPORT_FILE},
            data={"file_sha256": "a" * 64},
        )

    if method == "get":
        return api_client.get(url)

    if method == "patch":
        return api_client.patch(url, json=body or {})

    return api_client.post(url, json=body)


@pytest.mark.parametrize(("method", "url", "body"), PROTECTED_ROUTES)
def test_without_a_token_every_bearer_route_answers_401(
    api_db, api_client, method, url, body
):
    response = _call(api_client, method, url, body)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize(("method", "url", "body"), COOKIE_ROUTES)
def test_without_a_cookie_the_refresh_route_answers_401(
    api_db, api_client, method, url, body
):
    response = _call(api_client, method, url, body)

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired."}
    assert "www-authenticate" not in response.headers


def test_a_garbage_token_is_rejected_like_a_missing_one(api_client):
    for url in (ME_URL, "/api/v1/admin/users"):
        response = api_client.get(
            url, headers={"Authorization": "Bearer not-a-jwt"}
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated."}


def test_an_expired_access_token_is_rejected(api_client, make_user):
    user = make_user(username="vera")

    expired = create_access_token(
        user_id=user.id,
        session_id="00000000-0000-0000-0000-000000000000",
        roles=user.role_names(),
        token_version=user.token_version,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    response = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {expired}"}
    )

    assert response.status_code == 401


def test_a_token_with_a_stale_version_is_rejected(
    api_client, make_user, issue_token, db_session_factory
):
    from app.models import User

    user = make_user(username="vera")
    token = issue_token(user)

    # A password change or disable bumps token_version; the token was
    # issued before that and must stop working immediately.
    with db_session_factory() as db:
        stored = db.get(User, user.id)
        stored.token_version += 1
        db.commit()

    response = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401


def test_a_token_for_a_disabled_account_is_rejected(
    api_client, make_user, issue_token, db_session_factory
):
    from app.models import User

    user = make_user(username="vera")
    token = issue_token(user)

    with db_session_factory() as db:
        stored = db.get(User, user.id)
        stored.is_active = False
        db.commit()

    response = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401


def test_a_token_signed_with_a_foreign_key_is_rejected(api_client, make_user):
    settings = get_settings()
    user = make_user(username="vera")

    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {
            "sub": str(user.id),
            "sid": "00000000-0000-0000-0000-000000000000",
            "roles": ["admin"],
            "ver": user.token_version,
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        "a-completely-different-signing-secret",
        algorithm="HS256",
    )

    response = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {forged}"}
    )

    assert response.status_code == 401


def test_a_token_for_another_audience_is_rejected(api_client, make_user):
    settings = get_settings()
    user = make_user(username="vera")

    now = datetime.now(timezone.utc)
    foreign = jwt.encode(
        {
            "sub": str(user.id),
            "sid": "00000000-0000-0000-0000-000000000000",
            "roles": ["admin"],
            "ver": user.token_version,
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "iss": settings.jwt_issuer,
            "aud": "someone-elses-client",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    response = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {foreign}"}
    )

    assert response.status_code == 401


def test_public_routes_stay_public(api_client):
    health = api_client.get("/health")
    assert health.status_code == 200

    # The reset endpoint is reachable without login; an unknown token
    # is a domain error, not an authentication error.
    reset = api_client.post(
        "/api/v1/auth/reset-password",
        json={"token": "not-a-token", "new_password": "Whatever-One-1"},
    )
    assert reset.status_code == 400
