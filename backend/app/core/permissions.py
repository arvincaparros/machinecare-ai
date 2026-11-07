"""Centralized permission definitions and the role-to-permission matrix.

Permissions are code constants, not database rows (the Phase 1 plan
considers a permission table overkill for three fixed roles). Every
authorization check resolves through :func:`permissions_for_roles`, so
changing the matrix here changes it everywhere at once.
"""

from __future__ import annotations

from collections.abc import Iterable

# Permission codes (authentication-and-rbac-plan.md §4.2).
ASSISTANT_USE = "assistant:use"
INCIDENTS_READ = "incidents:read"
INCIDENTS_IMPORT = "incidents:import"
INCIDENTS_WRITE = "incidents:write"
INCIDENTS_DELETE = "incidents:delete"
INDEX_RUN = "index:run"
USERS_MANAGE = "users:manage"
AUDIT_READ = "audit:read"

# Roles seeded by the a1 migration.
ROLE_ADMIN = "admin"
ROLE_MAINTENANCE_ENGINEER = "maintenance_engineer"
ROLE_VIEWER = "viewer"

ALL_ROLES: tuple[str, ...] = (
    ROLE_ADMIN,
    ROLE_MAINTENANCE_ENGINEER,
    ROLE_VIEWER,
)

ALL_PERMISSIONS: frozenset[str] = frozenset(
    {
        ASSISTANT_USE,
        INCIDENTS_READ,
        INCIDENTS_IMPORT,
        INCIDENTS_WRITE,
        INCIDENTS_DELETE,
        INDEX_RUN,
        USERS_MANAGE,
        AUDIT_READ,
    }
)

# The permission matrix (plan §4.3). Decision: the Maintenance Engineer
# keeps incidents:import — import is an append-only bulk capture workflow
# that never updates or deletes.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: ALL_PERMISSIONS,
    ROLE_MAINTENANCE_ENGINEER: frozenset(
        {
            ASSISTANT_USE,
            INCIDENTS_READ,
            INCIDENTS_IMPORT,
        }
    ),
    ROLE_VIEWER: frozenset(
        {
            ASSISTANT_USE,
            INCIDENTS_READ,
        }
    ),
}


def permissions_for_roles(roles: Iterable[str]) -> frozenset[str]:
    """Union of the permissions held by the given role names.

    Unknown role names contribute nothing, so a role removed from this
    module cannot silently retain its old powers.
    """

    permissions: set[str] = set()

    for role in roles:
        permissions |= ROLE_PERMISSIONS.get(role, frozenset())

    return frozenset(permissions)
