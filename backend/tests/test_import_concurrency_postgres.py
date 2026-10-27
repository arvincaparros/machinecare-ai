"""Concurrency tests that need a real PostgreSQL server.

SQLite is deliberately not used here: it serialises writers, so it
cannot show what two overlapping PostgreSQL transactions do, and the
import's last line of defence is a PostgreSQL unique constraint.

These tests run only when ``IMPORT_TEST_POSTGRES_URL`` points at a
dedicated, disposable database, and refuse to run against anything whose
name does not contain ``import_test``. The configured development
database is therefore never touched. The tables this module owns are
dropped and recreated around the run.

Run them with::

    $env:IMPORT_TEST_POSTGRES_URL = (
        "postgresql+psycopg://user:pass@127.0.0.1:5434/"
        "machinecare_import_test"
    )
    python -m pytest tests/test_import_concurrency_postgres.py
"""

import datetime
import os
import threading

import pytest
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.database import Base
from app.models import (
    DowntimeIncident,
    DowntimeResolution,
    Machine,
)
from app.services.incident_import_commit import (
    IncidentImportInsertFailed,
    IncidentImportStaleData,
    commit_incident_import,
)
from app.services.incident_import_service import IncidentImportRow


ENVIRONMENT_VARIABLE = "IMPORT_TEST_POSTGRES_URL"


def _test_database_url():
    """Return the URL of the disposable test database, or skip."""
    value = os.environ.get(ENVIRONMENT_VARIABLE)

    if not value:
        pytest.skip(f"{ENVIRONMENT_VARIABLE} is not set")

    url = make_url(value)

    if "import_test" not in (url.database or "").lower():
        pytest.skip(
            f"{ENVIRONMENT_VARIABLE} must name a dedicated test "
            "database containing 'import_test'"
        )

    return url


def make_row(
    incident_id: str,
    cause: str | None = "Camera calibration failed",
    row_number: int = 2,
) -> IncidentImportRow:
    """Build one parser-approved row."""

    return IncidentImportRow(
        row_number=row_number,
        incident_id=incident_id,
        date=datetime.date(2026, 8, 1),
        machine="AOI-01",
        status="Closed",
        downtime_minutes=25,
        cause=cause,
        root_cause="Camera drift after extended operation",
        action_taken="Recalibrated camera and verified inspection",
        production_line="Line 9",
        product="Module A",
        issue_category="Machine",
        responsible_team="Maintenance",
        preventive_action="Weekly calibration schedule",
    )


@pytest.fixture(scope="module")
def engine():
    """Provide an engine on the disposable PostgreSQL database."""

    engine = create_engine(_test_database_url(), pool_pre_ping=True)

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Machine(
                machine_code="AOI-01",
                name="Automated Optical Inspection",
                production_line="Line 1",
            )
        )
        session.commit()

    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def machine_id(engine):
    """Return the primary key of the machine the tests reference."""

    with Session(engine) as session:
        return session.scalar(
            select(Machine.id).where(Machine.machine_code == "AOI-01")
        )


@pytest.fixture
def empty_incidents(engine):
    """Start every test with no stored incidents."""

    with Session(engine) as session:
        session.execute(delete(DowntimeResolution))
        session.execute(delete(DowntimeIncident))
        session.commit()


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_an_uncommitted_insert_is_invisible_to_a_concurrent_read(
    engine,
    machine_id,
    empty_incidents,
):
    """A preview read is a snapshot, not a reservation."""

    from app.repositories import incident_repository

    with Session(engine) as writer:
        writer.add(
            DowntimeIncident(
                incident_id="DT-RACE-001",
                machine_id=machine_id,
                production_line="Line 9",
                date=datetime.date(2026, 8, 1),
                cause="first",
                downtime_minutes=25,
                status="Closed",
            )
        )
        writer.flush()

        with Session(engine) as reader:
            seen = incident_repository.find_existing_incident_ids(
                reader,
                ["DT-RACE-001"],
            )

        assert seen == set()

        writer.rollback()

    assert count(engine, DowntimeIncident) == 0


def test_a_concurrent_import_rolls_back_and_keeps_the_first_row(
    engine,
    machine_id,
    empty_incidents,
):
    """The unique constraint decides the race; the loser writes nothing.

    A holds an uncommitted row. B reads the IDs (sees nothing), then
    inserts. B blocks on the unique index until A commits, and then its
    whole batch is rolled back.
    """

    writer = Session(engine)
    insert_started = threading.Event()
    outcome: dict[str, object] = {}

    def watch_insert(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("INSERT"):
            insert_started.set()

    event.listen(engine, "before_cursor_execute", watch_insert)

    def run_import():
        with Session(engine) as session:
            try:
                commit_incident_import(
                    session,
                    [
                        make_row("DT-RACE-001", cause="second"),
                        make_row("DT-RACE-002", row_number=3),
                    ],
                    skip_existing=False,
                )
            except BaseException as error:  # reported to the main thread
                outcome["error"] = error
            else:
                outcome["error"] = None

    try:
        writer.add(
            DowntimeIncident(
                incident_id="DT-RACE-001",
                machine_id=machine_id,
                production_line="Line 9",
                date=datetime.date(2026, 8, 1),
                cause="first",
                downtime_minutes=25,
                status="Closed",
            )
        )
        writer.flush()

        thread = threading.Thread(target=run_import, daemon=True)
        thread.start()

        assert insert_started.wait(timeout=60), (
            "the second import never reached its insert"
        )

        writer.commit()
        thread.join(timeout=60)
        assert not thread.is_alive()
    finally:
        event.remove(engine, "before_cursor_execute", watch_insert)
        writer.close()

    assert isinstance(outcome["error"], IncidentImportStaleData)
    assert outcome["error"].incident_ids == ("DT-RACE-001",)

    with Session(engine) as session:
        stored = list(
            session.scalars(
                select(DowntimeIncident).order_by(
                    DowntimeIncident.incident_id
                )
            ).all()
        )

    assert [incident.incident_id for incident in stored] == [
        "DT-RACE-001"
    ]
    assert stored[0].cause == "first"


def test_postgres_rolls_back_the_whole_batch_on_a_refused_insert(
    engine,
    empty_incidents,
):
    """A real PostgreSQL rollback, not a SQLite savepoint."""

    with Session(engine) as session:
        with pytest.raises(IncidentImportInsertFailed):
            commit_incident_import(
                session,
                [
                    make_row("DT-RACE-010", row_number=2),
                    make_row("DT-RACE-011", row_number=3, cause=None),
                ],
                skip_existing=False,
            )

    assert count(engine, DowntimeIncident) == 0
    assert count(engine, DowntimeResolution) == 0
