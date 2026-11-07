
import os
import uuid
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock

# Ensure the production .env database is never used by tests.
os.environ.setdefault("DB_USER", "machinecare_test")
os.environ.setdefault("DB_PASSWORD", "machinecare_test")
os.environ.setdefault("DB_NAME", "machinecare_test")

# Authentication settings for tests. The refresh cookie is insecure
# here because httpx will not send Secure cookies to http://testserver.
os.environ.setdefault("JWT_SECRET", "test-only-secret-never-use-in-production")
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import app
from app.core.permissions import ROLE_ADMIN
from app.core.security import (
    access_token_expiry,
    create_access_token,
    hash_password,
)
from app.db.database import Base
from app.models import (
    DowntimeIncident,
    DowntimeResolution,
    Machine,
    Role,
    User,
)
from app.services import rag_service


@pytest.fixture
def db_session_factory():
    """Provide an isolated in-memory database per test."""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)

    # Mirror the a1 migration's role seed: create_all alone leaves the
    # roles table empty, and the admin endpoints resolve roles from it.
    with engine.begin() as connection:
        connection.execute(
            Role.__table__.insert(),
            [
                {"id": 1, "name": "admin",
                 "description":
                     "Full control: users, imports, reindex, audit."},
                {"id": 2, "name": "maintenance_engineer",
                 "description":
                     "Uses the assistant and runs incident imports."},
                {"id": 3, "name": "viewer",
                 "description":
                     "Uses the assistant and reads incident answers."},
            ],
        )

    connection = engine.connect()
    transaction = connection.begin()

    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield factory
    finally:
        # Discard every test mutation.
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def add_incident(db_session_factory):
    """Insert an isolated incident with an optional resolution."""

    def _add(
        incident_id: str = "DT-TEST-001",
        machine_code: str = "AOI-01",
        status: str = "Closed",
        cause: str = "Camera calibration failed",
        root_cause: str = (
            "Camera drift after extended operation"
        ),
        action_taken: str = (
            "Recalibrated camera and verified inspection"
        ),
        preventive_action: str = (
            "Weekly calibration schedule"
        ),
        with_resolution: bool = True,
    ) -> str:
        with db_session_factory() as db:
            machine = db.scalar(
                select(Machine).where(
                    Machine.machine_code == machine_code
                )
            )

            if machine is None:
                machine = Machine(
                    machine_code=machine_code,
                    name=machine_code,
                    production_line="Line 1",
                )
                db.add(machine)
                db.flush()

            incident = DowntimeIncident(
                incident_id=incident_id,
                machine_id=machine.id,
                production_line="Line 1",
                date=date(2026, 7, 1),
                product="Module A",
                issue_category="Machine",
                cause=cause,
                downtime_minutes=25,
                responsible_team="Maintenance",
                status=status,
            )
            db.add(incident)
            db.flush()

            if with_resolution:
                db.add(
                    DowntimeResolution(
                        incident_id=incident.id,
                        root_cause=root_cause,
                        action_taken=action_taken,
                        preventive_action=preventive_action,
                    )
                )

            db.commit()

        return incident_id

    return _add


@pytest.fixture
def make_match():
    """Build a ChromaDB-like search result."""

    def _make(
        incident_id: str,
        distance: float = 0.20,
        document: str = "stale ChromaDB document",
    ) -> dict:
        return {
            "incident_id": incident_id,
            "document": document,
            "metadata": {
                "incident_id": incident_id,
                "machine": "AOI-01",
                "status": "Closed",
            },
            "distance": distance,
        }

    return _make


@pytest.fixture
def mock_search(monkeypatch):
    """Replace ChromaDB search with controlled matches."""

    mock = MagicMock(return_value=[])
    monkeypatch.setattr(
        rag_service,
        "search_similar_incidents",
        mock,
    )
    return mock


@pytest.fixture
def mock_groq(monkeypatch):
    """Replace the Groq API call with a deterministic mock."""

    mock = MagicMock(return_value="Grounded answer.")
    monkeypatch.setattr(
        rag_service,
        "generate_response",
        mock,
    )
    return mock


# ---------------------------------------------------------------------
# Authentication support (Phase 1)
# ---------------------------------------------------------------------

# Passes the policy: 12+ characters, not a common password, and does
# not contain any test username.
TEST_PASSWORD = "Weatherproof-Cable-47"


@pytest.fixture
def valid_password() -> str:
    """The password shared by tests that log in as a made user."""

    return TEST_PASSWORD

# Role rows seeded by the a1 migration; recreated on demand in tests.
ROLE_IDS = {
    "admin": 1,
    "maintenance_engineer": 2,
    "viewer": 3,
}


@pytest.fixture
def make_user(db_session_factory):
    """Create a user with real role rows; returns the persisted User."""

    def _make(
        username: str = "admin",
        password: str = TEST_PASSWORD,
        roles: tuple[str, ...] = (ROLE_ADMIN,),
        is_active: bool = True,
    ) -> User:
        with db_session_factory() as db:
            role_rows = []

            for name in roles:
                role = db.get(Role, ROLE_IDS[name])

                if role is None:
                    role = Role(id=ROLE_IDS[name], name=name)
                    db.add(role)
                    db.flush()

                role_rows.append(role)

            user = User(
                username=username.strip().lower(),
                display_name=username.strip().title(),
                password_hash=hash_password(password),
                is_active=is_active,
                roles=role_rows,
            )
            db.add(user)
            db.commit()

        return user

    return _make


@pytest.fixture
def issue_token():
    """Sign a real access token for a persisted user row."""

    def _issue(user: User) -> str:
        return create_access_token(
            user_id=user.id,
            session_id=str(uuid.uuid4()),
            roles=user.role_names(),
            token_version=user.token_version,
            expires_at=access_token_expiry(),
        )

    return _issue


@pytest.fixture
def api_db(db_session_factory, monkeypatch):
    """Route the API's databases to the isolated test database.

    Two redirections happen here:

    - ``get_auth_db`` (principal lookups for every protected route)
      yields sessions from the in-memory database.
    - ``SessionLocal`` — used by ``record_event(None, ...)`` for the
      standalone audit writer — is pointed at the same factory, so
      authentication events land in the observable test database
      instead of trying to reach PostgreSQL.
    """

    from app.db import database
    from app.dependencies.auth import get_auth_db

    def _auth_session():
        yield db_session_factory()

    monkeypatch.setattr(database, "SessionLocal", db_session_factory)
    app.dependency_overrides[get_auth_db] = _auth_session

    yield db_session_factory

    app.dependency_overrides.pop(get_auth_db, None)


@pytest.fixture
def api_client(api_db):
    """An unauthenticated TestClient with the auth database wired."""

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(api_db, make_user, issue_token):
    """Open a TestClient authenticated as a user with the given roles.

    The bearer token is signed for real, so every request goes through
    the full ``get_current_user`` chain: signature, active flag,
    ``token_version`` and roles resolved from the database.

    Usage::

        with auth_client() as client:                    # admin
            ...
        with auth_client(roles=(ROLE_VIEWER,)) as client:
            ...
    """

    @contextmanager
    def _auth_client(roles=(ROLE_ADMIN,), username="admin"):
        user = make_user(username=username, roles=roles)

        with TestClient(app) as test_client:
            test_client.headers.update(
                {"Authorization": f"Bearer {issue_token(user)}"}
            )
            yield test_client

    return _auth_client


@pytest.fixture
def admin_token(make_user, issue_token):
    """A valid ``Bearer`` header value for an administrator."""

    user = make_user(username="admin", roles=(ROLE_ADMIN,))

    return f"Bearer {issue_token(user)}"


@pytest.fixture
def audit_events(api_db):
    """Alias of ``api_db``; use it where audit rows are the point."""

    return api_db
