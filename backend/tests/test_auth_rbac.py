"""RBAC tests: the permission matrix and its enforcement per route.

The unit tests pin the matrix itself; the endpoint tests drive the
``get_current_user`` -> ``require_permission`` chain with real tokens
and database roles, so a frontend-only restriction could never pass
here.
"""

import pytest

from app.api.incident_import import get_db_session, get_index_service
from app.api.main import app, get_ask_service
from app.core.permissions import (
    ALL_PERMISSIONS,
    ALL_ROLES,
    ASSISTANT_USE,
    INCIDENTS_IMPORT,
    INCIDENTS_READ,
    ROLE_ADMIN,
    ROLE_MAINTENANCE_ENGINEER,
    ROLE_VIEWER,
    ROLE_PERMISSIONS,
    permissions_for_roles,
)

from tests.test_api_incident_import import csv_bytes, row_values

ASK_URL = "/api/v1/ask"
TEMPLATE_URL = "/api/v1/incidents/import/template.xlsx"
MACHINES_URL = "/api/v1/incidents/import/machines"
PREVIEW_URL = "/api/v1/incidents/import/preview"
COMMIT_URL = "/api/v1/incidents/import"
REINDEX_URL = "/api/v1/incidents/import/reindex"
USERS_URL = "/api/v1/admin/users"

FORBIDDEN_DETAIL = "You do not have permission to perform this action."

ANSWER = {
    "answer": "Grounded answer.",
    "sources": [],
    "cited_incident_ids": [],
    "unsupported_citations": [],
}

ROLES_BY_PRIVILEGE = (
    (ROLE_ADMIN,),
    (ROLE_MAINTENANCE_ENGINEER,),
    (ROLE_VIEWER,),
)


def _assert_forbidden(response):
    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    # A 403 says "authenticated, but not enough": it must never ask the
    # client to authenticate again.
    assert "www-authenticate" not in response.headers


@pytest.fixture
def import_db(db_session_factory):
    """Override the incident database with one AOI-01 machine row.

    The import routes resolve their session through ``get_db_session``;
    without this override they would reach for PostgreSQL.
    """

    from app.models import Machine

    session = db_session_factory()
    session.add(
        Machine(
            machine_code="AOI-01",
            name="Automated Optical Inspection",
            production_line="Line 1",
        )
    )
    session.commit()

    app.dependency_overrides[get_db_session] = lambda: session

    yield session

    app.dependency_overrides.pop(get_db_session, None)
    session.close()


# ---------------------------------------------------------------------
# The matrix itself
# ---------------------------------------------------------------------


def test_the_admin_role_holds_every_permission():
    assert permissions_for_roles([ROLE_ADMIN]) == ALL_PERMISSIONS


def test_the_engineer_role_can_import_but_not_administer():
    assert permissions_for_roles(
        [ROLE_MAINTENANCE_ENGINEER]
    ) == frozenset({ASSISTANT_USE, INCIDENTS_READ, INCIDENTS_IMPORT})


def test_the_viewer_role_is_read_only():
    assert permissions_for_roles([ROLE_VIEWER]) == frozenset(
        {ASSISTANT_USE, INCIDENTS_READ}
    )


def test_unknown_roles_contribute_no_permissions():
    assert permissions_for_roles(["ghost"]) == frozenset()


def test_every_declared_role_has_a_matrix_entry():
    assert set(ROLE_PERMISSIONS) == set(ALL_ROLES)

    for permissions in ROLE_PERMISSIONS.values():
        assert permissions <= ALL_PERMISSIONS
        assert permissions  # no role may be entirely powerless


# ---------------------------------------------------------------------
# Enforcement per route
# ---------------------------------------------------------------------


def test_ask_is_open_to_every_role(auth_client):
    app.dependency_overrides[get_ask_service] = lambda: (
        lambda **kwargs: ANSWER
    )

    try:
        for roles in ROLES_BY_PRIVILEGE:
            with auth_client(roles=roles, username=roles[0]) as client:
                response = client.post(
                    ASK_URL, json={"question": "Why did the AOI drift?"}
                )
                assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_ask_service, None)


def test_the_machine_list_is_open_to_every_role(auth_client, import_db):
    for roles in ROLES_BY_PRIVILEGE:
        with auth_client(roles=roles, username=roles[0]) as client:
            response = client.get(MACHINES_URL)
            assert response.status_code == 200


def test_the_import_template_needs_the_import_permission(auth_client):
    for roles, expected in (
        ((ROLE_ADMIN,), 200),
        ((ROLE_MAINTENANCE_ENGINEER,), 200),
        ((ROLE_VIEWER,), 403),
    ):
        with auth_client(roles=roles, username=roles[0]) as client:
            response = client.get(TEMPLATE_URL)

            if expected == 200:
                assert response.status_code == 200
            else:
                _assert_forbidden(response)


def test_preview_needs_the_import_permission(auth_client, import_db):
    content = csv_bytes([row_values()])

    for roles, expected in (
        ((ROLE_ADMIN,), 200),
        ((ROLE_MAINTENANCE_ENGINEER,), 200),
        ((ROLE_VIEWER,), 403),
    ):
        with auth_client(roles=roles, username=roles[0]) as client:
            response = client.post(
                PREVIEW_URL,
                files={"file": ("incidents.csv", content, "text/csv")},
            )

            if expected == 200:
                assert response.status_code == 200
            else:
                _assert_forbidden(response)


def test_commit_needs_the_import_permission(auth_client, import_db):
    content = csv_bytes([row_values()])

    for roles in ROLES_BY_PRIVILEGE:
        with auth_client(roles=roles, username=roles[0]) as client:
            response = client.post(
                COMMIT_URL,
                files={"file": ("incidents.csv", content, "text/csv")},
            )

            if roles == (ROLE_VIEWER,):
                _assert_forbidden(response)
            else:
                # Without a prior preview the handler refuses with 409;
                # the point here is that the permission check passed.
                assert response.status_code == 409


def test_reindex_needs_the_index_permission(auth_client):
    app.dependency_overrides[get_index_service] = lambda: (lambda: 0)

    try:
        for roles, expected in (
            ((ROLE_ADMIN,), 200),
            ((ROLE_MAINTENANCE_ENGINEER,), 403),
            ((ROLE_VIEWER,), 403),
        ):
            with auth_client(roles=roles, username=roles[0]) as client:
                response = client.post(REINDEX_URL)

                if expected == 200:
                    assert response.status_code == 200
                else:
                    _assert_forbidden(response)
    finally:
        app.dependency_overrides.pop(get_index_service, None)


def test_user_management_needs_the_manage_permission(auth_client):
    for roles in ROLES_BY_PRIVILEGE:
        with auth_client(roles=roles, username=roles[0]) as client:
            response = client.get(USERS_URL)

            if roles == (ROLE_ADMIN,):
                assert response.status_code == 200
            else:
                _assert_forbidden(response)


def test_me_reports_the_effective_roles_and_permissions(auth_client):
    for roles, expected_permissions in (
        (
            (ROLE_ADMIN,),
            sorted(ALL_PERMISSIONS),
        ),
        (
            (ROLE_MAINTENANCE_ENGINEER,),
            ["assistant:use", "incidents:import", "incidents:read"],
        ),
        (
            (ROLE_VIEWER,),
            ["assistant:use", "incidents:read"],
        ),
    ):
        with auth_client(roles=roles, username=roles[0]) as client:
            response = client.get("/api/v1/auth/me")

            assert response.status_code == 200
            body = response.json()
            assert body["roles"] == sorted(roles)
            assert body["permissions"] == expected_permissions
