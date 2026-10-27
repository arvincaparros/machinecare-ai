
import os
from datetime import date
from unittest.mock import MagicMock

# Ensure the production .env database is never used by tests.
os.environ.setdefault("DB_USER", "machinecare_test")
os.environ.setdefault("DB_PASSWORD", "machinecare_test")
os.environ.setdefault("DB_NAME", "machinecare_test")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models import (
    DowntimeIncident,
    DowntimeResolution,
    Machine,
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
