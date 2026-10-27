"""Tests for the transactional side of the incident import.

Every test runs against an isolated in-memory database and inserts
synthetic rows. The configured PostgreSQL database and the persistent
ChromaDB collection are never opened.
"""

import datetime

import pytest
from sqlalchemy import select

from app.models import (
    DowntimeIncident,
    DowntimeResolution,
    Machine,
)
from app.repositories import incident_repository
from app.services.incident_import_commit import (
    IncidentImportInsertFailed,
    IncidentImportStaleData,
    commit_incident_import,
    stale_data_errors,
)
from app.services.incident_import_preview import (
    EXISTING_INCIDENT_ID,
    UNKNOWN_MACHINE,
)
from app.services.incident_import_service import IncidentImportRow


MACHINES = (
    ("AOI-01", "Automated Optical Inspection", "Line 1"),
    ("AOI-02", "Automated Optical Inspection", "Line 2"),
)


def make_row(
    incident_id: str = "DT-IMPORT-001",
    machine: str = "AOI-01",
    row_number: int = 2,
    production_line: str = "Line 9",
    cause: str = "Camera calibration failed",
) -> IncidentImportRow:
    """Build one parser-approved row."""

    return IncidentImportRow(
        row_number=row_number,
        incident_id=incident_id,
        date=datetime.date(2026, 8, 1),
        machine=machine,
        status="Closed",
        downtime_minutes=25,
        cause=cause,
        root_cause="Camera drift after extended operation",
        action_taken="Recalibrated camera and verified inspection",
        production_line=production_line,
        product="Module A",
        issue_category="Machine",
        responsible_team="Maintenance",
        preventive_action="Weekly calibration schedule",
    )


@pytest.fixture
def session(db_session_factory):
    """Provide an isolated session with two machines."""

    session = db_session_factory()

    for code, name, production_line in MACHINES:
        session.add(
            Machine(
                machine_code=code,
                name=name,
                production_line=production_line,
            )
        )

    session.commit()

    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def add_stored_incident(session):
    """Insert one incident that an import has to leave alone."""

    def _add(
        incident_id: str,
        machine_code: str = "AOI-01",
        cause: str = "Existing cause",
        downtime_minutes: int = 10,
    ) -> str:
        machine = session.scalar(
            select(Machine).where(
                Machine.machine_code == machine_code
            )
        )
        session.add(
            DowntimeIncident(
                incident_id=incident_id,
                machine_id=machine.id,
                production_line="Line 1",
                date=datetime.date(2026, 7, 1),
                cause=cause,
                downtime_minutes=downtime_minutes,
                status="Closed",
            )
        )
        session.commit()

        return incident_id

    return _add


@pytest.fixture
def blind_first_lookup(monkeypatch):
    """Make the pre-insert ID check miss a row that exists.

    That is what a simultaneous import looks like: this transaction read
    the IDs before the other one committed, so the unique constraint,
    not the check, is what refuses the row.
    """

    real = incident_repository.find_existing_incident_ids
    calls = []

    def lookup(db, incident_ids):
        calls.append(list(incident_ids))

        if len(calls) == 1:
            return set()

        return real(db, incident_ids)

    monkeypatch.setattr(
        incident_repository,
        "find_existing_incident_ids",
        lookup,
    )

    return calls


def stored_incidents(session) -> list[DowntimeIncident]:
    """Return every stored incident, ordered by business ID."""

    return list(
        session.scalars(
            select(DowntimeIncident).order_by(
                DowntimeIncident.incident_id
            )
        ).all()
    )


# ---------------------------------------------------------------------
# Inserts
# ---------------------------------------------------------------------


def test_commit_inserts_the_incident_and_its_resolution(session):
    outcome = commit_incident_import(
        session,
        [make_row()],
        skip_existing=False,
    )

    assert outcome.imported_row_count == 1
    assert outcome.imported_incident_ids == ("DT-IMPORT-001",)
    assert outcome.skipped_existing_rows == 0

    stored = stored_incidents(session)
    assert len(stored) == 1

    incident = stored[0]
    machine = session.scalar(
        select(Machine).where(Machine.machine_code == "AOI-01")
    )

    assert incident.machine_id == machine.id
    assert incident.status == "Closed"
    assert incident.date == datetime.date(2026, 8, 1)
    assert incident.downtime_minutes == 25
    assert incident.cause == "Camera calibration failed"
    assert incident.product == "Module A"
    assert incident.issue_category == "Machine"
    assert incident.responsible_team == "Maintenance"

    resolution = session.scalar(select(DowntimeResolution))
    assert resolution.incident_id == incident.id
    assert resolution.root_cause == (
        "Camera drift after extended operation"
    )
    assert resolution.action_taken == (
        "Recalibrated camera and verified inspection"
    )
    assert resolution.preventive_action == (
        "Weekly calibration schedule"
    )


def test_commit_keeps_the_incident_own_production_line(session):
    commit_incident_import(
        session,
        [make_row(production_line="Line 9")],
        skip_existing=False,
    )

    incident = stored_incidents(session)[0]
    machine = session.scalar(
        select(Machine).where(Machine.machine_code == "AOI-01")
    )

    assert incident.production_line == "Line 9"
    assert machine.production_line == "Line 1"


def test_commit_inserts_every_row_in_file_order(session):
    outcome = commit_incident_import(
        session,
        [
            make_row("DT-IMPORT-002", row_number=2),
            make_row("DT-IMPORT-001", machine="AOI-02", row_number=3),
        ],
        skip_existing=False,
    )

    assert outcome.imported_incident_ids == (
        "DT-IMPORT-002",
        "DT-IMPORT-001",
    )
    assert len(stored_incidents(session)) == 2
    assert session.scalar(
        select(DowntimeResolution).where(
            DowntimeResolution.id.is_not(None)
        )
    ) is not None


def test_commit_with_no_rows_writes_nothing(session):
    outcome = commit_incident_import(session, [], skip_existing=False)

    assert outcome.imported_row_count == 0
    assert outcome.skipped_existing_rows == 0
    assert stored_incidents(session) == []


# ---------------------------------------------------------------------
# Existing records
# ---------------------------------------------------------------------


def test_existing_ids_are_rejected_by_default(session, add_stored_incident):
    add_stored_incident("DT-IMPORT-001")

    with pytest.raises(IncidentImportStaleData) as raised:
        commit_incident_import(
            session,
            [
                make_row("DT-IMPORT-001", row_number=2),
                make_row("DT-IMPORT-002", row_number=3),
            ],
            skip_existing=False,
        )

    assert raised.value.incident_ids == ("DT-IMPORT-001",)
    assert raised.value.machine_codes == ()
    assert [
        incident.incident_id for incident in stored_incidents(session)
    ] == ["DT-IMPORT-001"]


def test_existing_ids_are_skipped_when_asked(session, add_stored_incident):
    add_stored_incident("DT-IMPORT-001")

    outcome = commit_incident_import(
        session,
        [
            make_row("DT-IMPORT-001", row_number=2),
            make_row("DT-IMPORT-002", row_number=3),
        ],
        skip_existing=True,
    )

    assert outcome.imported_incident_ids == ("DT-IMPORT-002",)
    assert outcome.skipped_existing_rows == 1
    assert [
        incident.incident_id for incident in stored_incidents(session)
    ] == ["DT-IMPORT-001", "DT-IMPORT-002"]


def test_skipping_leaves_the_stored_incident_untouched(
    session,
    add_stored_incident,
):
    add_stored_incident(
        "DT-IMPORT-001",
        cause="Existing cause",
        downtime_minutes=10,
    )

    commit_incident_import(
        session,
        [make_row("DT-IMPORT-001", cause="Replacement cause")],
        skip_existing=True,
    )

    session.expire_all()
    incident = stored_incidents(session)[0]

    assert incident.cause == "Existing cause"
    assert incident.downtime_minutes == 10


def test_a_machine_that_disappeared_is_stale_data(session):
    with pytest.raises(IncidentImportStaleData) as raised:
        commit_incident_import(
            session,
            [make_row(machine="PRESS-99")],
            skip_existing=False,
        )

    assert raised.value.machine_codes == ("PRESS-99",)
    assert raised.value.incident_ids == ()
    assert stored_incidents(session) == []


# ---------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------


def test_an_insert_failure_rolls_back_every_row(session):
    # The second row carries no cause, which breaks the NOT NULL column
    # after the first row has already been added to the same transaction.
    rows = [
        make_row("DT-IMPORT-001", row_number=2),
        make_row("DT-IMPORT-002", row_number=3, cause=None),
    ]

    with pytest.raises(IncidentImportInsertFailed):
        commit_incident_import(session, rows, skip_existing=False)

    assert stored_incidents(session) == []


def test_a_unique_conflict_rolls_back_every_row(
    session,
    add_stored_incident,
    blind_first_lookup,
):
    add_stored_incident("DT-IMPORT-001")
    blind_first_lookup.clear()

    with pytest.raises(IncidentImportStaleData) as raised:
        commit_incident_import(
            session,
            [
                make_row("DT-IMPORT-001", row_number=2),
                make_row("DT-IMPORT-002", row_number=3),
            ],
            skip_existing=False,
        )

    assert raised.value.incident_ids == ("DT-IMPORT-001",)
    assert [
        incident.incident_id for incident in stored_incidents(session)
    ] == ["DT-IMPORT-001"]


def test_a_unique_conflict_never_overwrites_the_stored_row(
    session,
    add_stored_incident,
    blind_first_lookup,
):
    add_stored_incident(
        "DT-IMPORT-001",
        cause="Existing cause",
        downtime_minutes=10,
    )

    with pytest.raises(IncidentImportStaleData):
        commit_incident_import(
            session,
            [make_row("DT-IMPORT-001", cause="Replacement cause")],
            skip_existing=False,
        )

    session.expire_all()
    incident = stored_incidents(session)[0]

    assert incident.cause == "Existing cause"
    assert incident.downtime_minutes == 10


def test_skipping_does_not_rescue_a_unique_conflict(
    session,
    add_stored_incident,
    blind_first_lookup,
):
    add_stored_incident("DT-IMPORT-001")

    with pytest.raises(IncidentImportStaleData):
        commit_incident_import(
            session,
            [make_row("DT-IMPORT-001")],
            skip_existing=True,
        )

    assert len(stored_incidents(session)) == 1


# ---------------------------------------------------------------------
# Row-level errors and digests
# ---------------------------------------------------------------------


def test_stale_data_errors_name_the_affected_rows():
    rows = [
        make_row("DT-IMPORT-001", row_number=2),
        make_row("DT-IMPORT-002", machine="AOI-02", row_number=3),
    ]
    stale = IncidentImportStaleData(
        incident_ids=("DT-IMPORT-001",),
        machine_codes=("AOI-02",),
    )

    errors = stale_data_errors(stale, rows)

    assert [
        (error.row, error.field, error.code) for error in errors
    ] == [
        (2, "Incident ID", EXISTING_INCIDENT_ID),
        (3, "Machine", UNKNOWN_MACHINE),
    ]
    assert "DT-IMPORT-001" in errors[0].message
    assert "AOI-02" in errors[1].message
