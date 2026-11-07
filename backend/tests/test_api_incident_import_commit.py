"""API tests for the transactional import and the reindex retry.

The database dependency is overridden with an isolated in-memory SQLite
session and the indexing dependency with a recorder, so no test writes
to the configured PostgreSQL database, runs the seeding or indexing
scripts, or opens the persistent ChromaDB collection.
"""

import csv
import datetime
import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.api.incident_import import (
    get_db_session,
    get_index_service,
)
from app.api.main import app
from app.models import (
    DowntimeIncident,
    DowntimeResolution,
    Machine,
)
from app.repositories import incident_repository
from app.services.incident_import_commit import (
    IncidentImportInsertFailed,
)
from app.services.incident_import_service import (
    IMPORT_COLUMNS,
    MAX_IMPORT_BYTES,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]

IMPORT_URL = "/api/v1/incidents/import"
REINDEX_URL = "/api/v1/incidents/import/reindex"
PREVIEW_URL = "/api/v1/incidents/import/preview"

MACHINES = (
    ("AOI-01", "Automated Optical Inspection", "Line 1"),
    ("AOI-02", "Automated Optical Inspection", "Line 2"),
    ("Pick & Place-03", "Pick and Place", "Line 3"),
)

VALID_ROW = {
    "Incident ID": "DT-2026-101",
    "Date": "2026-08-01",
    "Production Line": "Line 1",
    "Machine": "AOI-01",
    "Product": "Module A",
    "Downtime (min)": 25,
    "Issue Category": "Machine",
    "Cause": "Camera calibration failed",
    "Action Taken": "Recalibrated camera and verified inspection",
    "Root Cause": "Camera drift after extended operation",
    "Responsible Team": "Maintenance",
    "Status": "Closed",
    "Preventive Action": "Weekly calibration schedule",
}

WRITE_PREFIXES = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "CREATE",
    "ALTER",
    "DROP",
    "GRANT",
    "REVOKE",
    "COMMIT",
)


def row_values(**overrides) -> list[object]:
    """Build one row in header order with optional overrides."""

    values = dict(VALID_ROW)
    values.update(overrides)
    return [values[column] for column in IMPORT_COLUMNS]


def csv_bytes(rows: list[list[object]]) -> bytes:
    """Build a CSV upload with the template header."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(IMPORT_COLUMNS))

    for row in rows:
        writer.writerow(row)

    return buffer.getvalue().encode("utf-8")


def digest_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_statements(statements) -> list[str]:
    """Return the recorded statements that modify data or schema."""

    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(WRITE_PREFIXES)
    ]


class IndexRecorder:
    """Stand in for the vector store indexing step."""

    def __init__(self, session_factory, total=7, error=None):
        self.session_factory = session_factory
        self.total = total
        self.error = error
        self.calls = 0
        self.stored_ids: list[str] | None = None

    def __call__(self) -> int:
        self.calls += 1

        with self.session_factory() as peek:
            self.stored_ids = sorted(
                peek.scalars(
                    select(DowntimeIncident.incident_id)
                ).all()
            )

        if self.error is not None:
            raise self.error

        return self.total


class ImportClient:
    """A test client plus the objects a test needs to inspect."""

    def __init__(self, client, session, indexer, statements):
        self.client = client
        self.session = session
        self.indexer = indexer
        self.statements = statements

    def preview(self, content, filename="incidents.csv"):
        return self.client.post(
            PREVIEW_URL,
            files={"file": (filename, content, "text/csv")},
        )

    def post(
        self,
        content: bytes,
        file_sha256: str | None = None,
        skip_existing: bool | None = None,
        filename: str = "incidents.csv",
    ):
        data = {}

        if file_sha256 is not None:
            data["file_sha256"] = file_sha256

        if skip_existing is not None:
            data["skip_existing"] = str(skip_existing).lower()

        return self.client.post(
            IMPORT_URL,
            files={"file": (filename, content, "text/csv")},
            data=data,
        )

    def stored(self) -> list[DowntimeIncident]:
        return list(
            self.session.scalars(
                select(DowntimeIncident).order_by(
                    DowntimeIncident.incident_id
                )
            ).all()
        )


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def session(db_session_factory):
    """Provide an isolated session with three machines."""

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
def sql_statements(session):
    """Record every statement the API sends to the database."""

    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    target = session.get_bind()
    event.listen(target, "before_cursor_execute", record)

    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", record)


@pytest.fixture
def indexer(db_session_factory):
    return IndexRecorder(db_session_factory)


@pytest.fixture
def import_client(
    session, indexer, admin_token, api_db, sql_statements
):
    """Provide a client signed in as an admin, wired to the isolated
    database and the stub. ``api_db`` routes the token lookup to the
    isolated database too. The admin user is created before the
    ``sql_statements`` recorder attaches, so it only captures what the
    request itself sends."""

    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_index_service] = lambda: indexer

    with TestClient(app) as client:
        client.headers.update({"Authorization": admin_token})
        yield ImportClient(client, session, indexer, sql_statements)

    app.dependency_overrides.clear()


@pytest.fixture
def blind_id_lookup(monkeypatch):
    """Make the preview and the pre-insert check miss stored rows.

    That is the race a real import cannot avoid: both reads happened
    before another transaction committed, so only the unique constraint
    can refuse the row. The third lookup, the one that runs after the
    rollback, still sees the truth.
    """

    real = incident_repository.find_existing_incident_ids
    calls = []

    def lookup(db, incident_ids):
        calls.append(list(incident_ids))

        if len(calls) <= 2:
            return set()

        return real(db, incident_ids)

    monkeypatch.setattr(
        incident_repository,
        "find_existing_incident_ids",
        lookup,
    )

    return calls


def add_stored_incident(
    session,
    incident_id: str,
    cause: str = "Existing cause",
    downtime_minutes: int = 10,
) -> None:
    """Insert one committed incident with a resolution."""

    machine = session.scalar(
        select(Machine).where(Machine.machine_code == "AOI-01")
    )
    incident = DowntimeIncident(
        incident_id=incident_id,
        machine_id=machine.id,
        production_line="Line 1",
        date=datetime.date(2026, 7, 1),
        cause=cause,
        downtime_minutes=downtime_minutes,
        status="Closed",
    )
    incident.resolution = DowntimeResolution(
        root_cause="Existing root cause",
        action_taken="Existing action",
    )
    session.add(incident)
    session.commit()


def count_rows(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------------
# A successful import
# ---------------------------------------------------------------------


def test_import_inserts_incidents_and_their_resolutions(import_client):
    content = csv_bytes(
        [
            row_values(),
            row_values(
                **{
                    "Incident ID": "DT-2026-102",
                    "Machine": "AOI-02",
                }
            ),
        ]
    )

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 200
    assert response.json() == {
        "file_name": "incidents.csv",
        "file_sha256": digest_of(content),
        "skip_existing": False,
        "imported_rows": 2,
        "imported_incident_ids": ["DT-2026-101", "DT-2026-102"],
        "skipped_existing_rows": 0,
        "database_committed": True,
        "indexing": {
            "status": "indexed",
            "collection_total": 7,
            "message": (
                "The search index was refreshed from PostgreSQL. "
                "collection_total is the collection's total document "
                "count, not the number of rows this import added."
            ),
        },
    }

    incident = import_client.stored()[0]
    resolution = import_client.session.scalar(
        select(DowntimeResolution).where(
            DowntimeResolution.incident_id == incident.id
        )
    )

    assert incident.status == "Closed"
    assert incident.production_line == "Line 1"
    assert incident.downtime_minutes == 25
    assert incident.responsible_team == "Maintenance"
    assert resolution.root_cause == (
        "Camera drift after extended operation"
    )
    assert resolution.action_taken == (
        "Recalibrated camera and verified inspection"
    )
    assert resolution.preventive_action == (
        "Weekly calibration schedule"
    )
    assert count_rows(import_client.session, DowntimeResolution) == 2


def test_import_keeps_the_production_line_written_in_the_file(
    import_client,
):
    content = csv_bytes([row_values(**{"Production Line": "Line 9"})])

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 200
    assert import_client.stored()[0].production_line == "Line 9"

    machine = import_client.session.scalar(
        select(Machine).where(Machine.machine_code == "AOI-01")
    )
    assert machine.production_line == "Line 1"


def test_import_does_not_create_a_machine(import_client):
    content = csv_bytes(
        [row_values(**{"Machine": "Pick & Place-03"})]
    )

    import_client.post(content, digest_of(content))

    assert count_rows(import_client.session, Machine) == len(MACHINES)


def test_import_accepts_an_uppercase_digest(import_client):
    content = csv_bytes([row_values()])

    response = import_client.post(content, digest_of(content).upper())

    assert response.status_code == 200


def test_the_import_route_is_the_exact_path_without_a_redirect(
    import_client,
):
    content = csv_bytes([row_values()])

    response = import_client.client.post(
        IMPORT_URL,
        files={"file": ("incidents.csv", content, "text/csv")},
        data={"file_sha256": digest_of(content)},
        follow_redirects=False,
    )

    assert response.status_code == 200


# ---------------------------------------------------------------------
# The preview hash
# ---------------------------------------------------------------------


def test_preview_returns_the_digest_the_import_needs(import_client):
    content = csv_bytes([row_values()])

    preview = import_client.preview(content)

    assert preview.status_code == 200
    assert preview.json()["file_sha256"] == digest_of(content)


def test_import_without_a_hash_is_refused(import_client):
    content = csv_bytes([row_values()])

    response = import_client.post(content)

    assert response.status_code == 409
    assert "previewed" in response.json()["detail"]
    assert import_client.stored() == []


def test_import_of_a_changed_file_is_refused(import_client):
    previewed = csv_bytes([row_values()])
    changed = csv_bytes(
        [row_values(**{"Downtime (min)": 240})]
    )

    response = import_client.post(
        changed,
        digest_of(previewed),
    )

    assert response.status_code == 409
    assert "previewed" in response.json()["detail"]
    assert import_client.stored() == []


@pytest.mark.parametrize("value", ["abc", "0" * 64, "z" * 64])
def test_import_with_a_malformed_hash_is_refused(import_client, value):
    content = csv_bytes([row_values()])

    response = import_client.post(content, value)

    assert response.status_code == 409
    assert import_client.stored() == []


# ---------------------------------------------------------------------
# Refused files
# ---------------------------------------------------------------------


def test_existing_ids_are_rejected_by_default(import_client):
    add_stored_incident(import_client.session, "DT-2026-101")
    content = csv_bytes(
        [
            row_values(),
            row_values(**{"Incident ID": "DT-2026-102"}),
        ]
    )

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == (
        "The file has rows that cannot be imported. Nothing was written."
    )
    assert [
        (error["row"], error["field"], error["code"])
        for error in body["errors"]
    ] == [(2, "Incident ID", "existing_incident_id")]
    assert [
        incident.incident_id for incident in import_client.stored()
    ] == ["DT-2026-101"]


def test_existing_ids_are_skipped_when_asked(import_client):
    add_stored_incident(
        import_client.session,
        "DT-2026-101",
        cause="Existing cause",
    )
    content = csv_bytes(
        [
            row_values(**{"Cause": "Replacement cause"}),
            row_values(**{"Incident ID": "DT-2026-102"}),
        ]
    )

    response = import_client.post(
        content,
        digest_of(content),
        skip_existing=True,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["imported_rows"] == 1
    assert body["imported_incident_ids"] == ["DT-2026-102"]
    assert body["skipped_existing_rows"] == 1

    import_client.session.expire_all()
    stored = {
        incident.incident_id: incident
        for incident in import_client.stored()
    }

    assert stored["DT-2026-101"].cause == "Existing cause"
    assert stored["DT-2026-101"].downtime_minutes == 10


@pytest.mark.parametrize("skip_existing", [False, True])
def test_duplicate_ids_in_the_upload_are_always_rejected(
    import_client,
    skip_existing,
):
    content = csv_bytes(
        [
            row_values(),
            row_values(**{"Cause": "Second mention"}),
        ]
    )

    response = import_client.post(
        content,
        digest_of(content),
        skip_existing=skip_existing,
    )

    assert response.status_code == 422
    assert import_client.stored() == []


def test_skipping_does_not_excuse_another_row_error(import_client):
    add_stored_incident(import_client.session, "DT-2026-101")
    content = csv_bytes(
        [
            row_values(),
            row_values(
                **{
                    "Incident ID": "DT-2026-102",
                    "Machine": "PRESS-99",
                }
            ),
        ]
    )

    response = import_client.post(
        content,
        digest_of(content),
        skip_existing=True,
    )

    assert response.status_code == 422
    assert [
        (error["row"], error["code"])
        for error in response.json()["errors"]
    ] == [(3, "unknown_machine")]
    assert len(import_client.stored()) == 1


def test_an_unknown_machine_blocks_the_whole_file(import_client):
    content = csv_bytes(
        [
            row_values(),
            row_values(
                **{
                    "Incident ID": "DT-2026-102",
                    "Machine": "PRESS-99",
                }
            ),
        ]
    )

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 422
    assert [
        (error["row"], error["code"])
        for error in response.json()["errors"]
    ] == [(3, "unknown_machine")]
    assert import_client.stored() == []
    assert count_rows(import_client.session, Machine) == len(MACHINES)


def test_a_parser_error_blocks_every_row(import_client):
    content = csv_bytes(
        [
            row_values(),
            row_values(
                **{
                    "Incident ID": "DT-2026-102",
                    "Date": "31/08/2026",
                }
            ),
        ]
    )

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 422
    assert response.json()["errors"][0]["row"] == 3
    assert import_client.stored() == []


def test_a_rejected_file_never_reaches_the_indexer(import_client):
    content = csv_bytes([row_values(**{"Machine": "PRESS-99"})])

    import_client.post(content, digest_of(content))

    assert import_client.indexer.calls == 0


def test_a_rejected_file_never_writes(import_client):
    content = csv_bytes([row_values(**{"Machine": "PRESS-99"})])

    import_client.post(content, digest_of(content))

    assert write_statements(import_client.statements) == []


def test_an_oversized_upload_is_refused(import_client):
    content = b"x" * (MAX_IMPORT_BYTES + 1)

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 413
    assert import_client.stored() == []


def test_an_unsupported_file_type_is_refused(import_client):
    content = b"not a spreadsheet at all"

    response = import_client.post(
        content,
        digest_of(content),
        filename="incidents.txt",
    )

    assert response.status_code == 415
    assert import_client.stored() == []


def test_an_import_database_failure_is_safe(import_client, monkeypatch):
    def explode(*args, **kwargs):
        raise IncidentImportInsertFailed("INSERT INTO downtime_incidents")

    # The route imports the commit service inside the request, so the
    # service module is what a failure has to be injected into.
    monkeypatch.setattr(
        "app.services.incident_import_commit.commit_incident_import",
        explode,
    )
    content = csv_bytes([row_values()])

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Failed to import the file. No row was written."
    }
    assert "INSERT" not in response.text
    assert "Traceback" not in response.text
    assert import_client.indexer.calls == 0


# ---------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------


def test_a_conflict_rolls_back_the_whole_file(
    import_client,
    blind_id_lookup,
):
    add_stored_incident(import_client.session, "DT-2026-101")
    content = csv_bytes(
        [
            row_values(),
            row_values(**{"Incident ID": "DT-2026-102"}),
        ]
    )

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == (
        "The database changed after the preview. Nothing was written; "
        "preview the file again before importing it."
    )
    assert [
        (error["row"], error["code"]) for error in body["errors"]
    ] == [(2, "existing_incident_id")]

    import_client.session.expire_all()
    assert [
        incident.incident_id for incident in import_client.stored()
    ] == ["DT-2026-101"]


def test_a_conflict_never_overwrites_the_stored_incident(
    import_client,
    blind_id_lookup,
):
    add_stored_incident(
        import_client.session,
        "DT-2026-101",
        cause="Existing cause",
        downtime_minutes=10,
    )
    content = csv_bytes(
        [row_values(**{"Cause": "Replacement cause"})]
    )

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 409

    import_client.session.expire_all()
    incident = import_client.stored()[0]

    assert incident.cause == "Existing cause"
    assert incident.downtime_minutes == 10
    assert len(import_client.stored()) == 1


def test_a_conflict_does_not_reach_the_indexer(
    import_client,
    blind_id_lookup,
):
    add_stored_incident(import_client.session, "DT-2026-101")
    content = csv_bytes([row_values()])

    import_client.post(content, digest_of(content))

    assert import_client.indexer.calls == 0


# ---------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------


def test_indexing_runs_after_the_commit(import_client):
    content = csv_bytes([row_values()])

    import_client.post(content, digest_of(content))

    assert import_client.indexer.calls == 1
    assert import_client.indexer.stored_ids == ["DT-2026-101"]


def test_an_indexing_failure_keeps_the_committed_rows(
    import_client,
    indexer,
):
    indexer.error = RuntimeError("collection is locked: /secret/path")

    content = csv_bytes([row_values()])

    response = import_client.post(content, digest_of(content))

    assert response.status_code == 200
    body = response.json()
    assert body["database_committed"] is True
    assert body["imported_rows"] == 1
    assert body["indexing"]["status"] == "failed"
    assert body["indexing"]["collection_total"] is None
    assert "imported and committed" in body["indexing"]["message"]
    assert "retry" in body["indexing"]["message"]
    assert "locked" not in response.text
    assert "secret" not in response.text

    assert [
        incident.incident_id for incident in import_client.stored()
    ] == ["DT-2026-101"]
    assert count_rows(import_client.session, DowntimeResolution) == 1


def test_a_repeated_import_does_not_overwrite(import_client):
    content = csv_bytes([row_values()])

    first = import_client.post(content, digest_of(content))
    second = import_client.post(content, digest_of(content))

    assert first.status_code == 200
    assert second.status_code == 422
    assert import_client.indexer.calls == 1

    import_client.session.expire_all()
    assert [
        incident.incident_id for incident in import_client.stored()
    ] == ["DT-2026-101"]
    assert count_rows(import_client.session, DowntimeResolution) == 1


def test_a_repeated_import_with_skipping_writes_nothing(import_client):
    content = csv_bytes([row_values()])

    import_client.post(content, digest_of(content))
    response = import_client.post(
        content,
        digest_of(content),
        skip_existing=True,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["imported_rows"] == 0
    assert body["skipped_existing_rows"] == 1
    assert body["indexing"]["status"] == "skipped"
    assert import_client.indexer.calls == 1
    assert len(import_client.stored()) == 1


# ---------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------


def test_reindex_refreshes_the_index_without_touching_the_database(
    import_client,
):
    add_stored_incident(
        import_client.session,
        "DT-2026-101",
        cause="Existing cause",
    )
    import_client.statements.clear()

    response = import_client.client.post(REINDEX_URL)

    assert response.status_code == 200
    assert response.json() == {
        "status": "indexed",
        "collection_total": 7,
        "message": (
            "Every Closed incident was re-indexed from PostgreSQL. "
            "collection_total is the collection's total document count."
        ),
    }
    assert import_client.indexer.calls == 1
    assert write_statements(import_client.statements) == []
    assert count_rows(import_client.session, DowntimeIncident) == 1
    assert count_rows(import_client.session, DowntimeResolution) == 1

    import_client.session.expire_all()
    incident = import_client.stored()[0]
    assert incident.cause == "Existing cause"


def test_a_failed_reindex_reports_503_without_changing_data(
    import_client,
    indexer,
):
    indexer.error = RuntimeError("disk full at /var/lib/chroma")
    add_stored_incident(import_client.session, "DT-2026-101")
    import_client.statements.clear()

    response = import_client.client.post(REINDEX_URL)

    assert response.status_code == 503
    assert response.json() == {
        "status": "failed",
        "collection_total": None,
        "message": (
            "Failed to refresh the search index. No stored incident was "
            "changed; retry once the vector store is available."
        ),
    }
    assert "disk full" not in response.text
    assert "chroma" not in response.text
    assert write_statements(import_client.statements) == []
    assert count_rows(import_client.session, DowntimeIncident) == 1


def test_the_reused_indexing_service_is_upsert_only():
    source = (
        BACKEND_DIR
        / "app"
        / "services"
        / "vector_store_service.py"
    ).read_text(encoding="utf-8")

    assert "collection.upsert(" in source
    assert "return collection.count()" in source
    assert "delete" not in source
    assert "reset" not in source
