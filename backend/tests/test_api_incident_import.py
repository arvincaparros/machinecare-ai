"""API tests for the read-only incident import endpoints.

The database dependency is overridden with an isolated in-memory SQLite
session, and every statement the API executes is recorded, so the tests
prove the preview only reads. Nothing here opens the configured
PostgreSQL database, writes rows, or queries the persistent ChromaDB
collection.
"""

import csv
import datetime
import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import event, func, select

from app.api.incident_import import get_db_session
from app.api.main import app
from app.models import DowntimeIncident, Machine
from app.services.incident_import_service import (
    IMPORT_COLUMNS,
    MAX_IMPORT_BYTES,
)
from app.services.incident_import_template import (
    build_csv_template,
    build_xlsx_template,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]

PREVIEW_URL = "/api/v1/incidents/import/preview"
MACHINES_URL = "/api/v1/incidents/import/machines"
TEMPLATE_URL = "/api/v1/incidents/import/template.xlsx"

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

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


def row_values(**overrides) -> list[object]:
    """Build one row in header order with optional overrides."""

    values = dict(VALID_ROW)
    values.update(overrides)
    return [values[column] for column in IMPORT_COLUMNS]


def csv_bytes(
    rows: list[list[object]],
    header: list[str] | tuple[str, ...] = IMPORT_COLUMNS,
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(header))

    for row in rows:
        writer.writerow(row)

    return buffer.getvalue().encode("utf-8")


def xlsx_bytes(
    rows: list[list[object]],
    header: list[str] | tuple[str, ...] = IMPORT_COLUMNS,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Incidents"
    sheet.append(list(header))

    for row in rows:
        sheet.append(list(row))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def workbook_members(content: bytes) -> dict[str, bytes]:
    """Return a workbook's parts, ignoring openpyxl's timestamps."""

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "docProps/core.xml"
        }


def post_preview(
    client: TestClient,
    content: bytes,
    filename: str = "incidents.csv",
    skip_existing: str | None = None,
):
    data = {}

    if skip_existing is not None:
        data["skip_existing"] = skip_existing

    return client.post(
        PREVIEW_URL,
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
    )


# Statement prefixes that would mean the preview wrote something. The
# isolated session runs in savepoint mode, so it may also emit the
# ``SAVEPOINT`` transaction-control statements that SQLAlchemy uses for
# isolation; those are not writes and are ignored here.
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


def write_statements(statements) -> list[str]:
    """Return the recorded statements that modify data or schema."""

    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(WRITE_PREFIXES)
    ]


def select_statements(statements) -> list[str]:
    """Return the recorded statements that read data."""

    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def import_session(db_session_factory):
    """Provide a session with three machines and no incidents."""

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
def sql_statements(import_session):
    """Record every statement the API sends to the database."""

    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    target = import_session.get_bind()
    event.listen(target, "before_cursor_execute", record)

    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", record)


@pytest.fixture
def client(import_session):
    """Provide a test client bound to the isolated database."""

    app.dependency_overrides[get_db_session] = lambda: import_session

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def add_existing_incident(
    session,
    incident_id: str,
    machine_code: str = "AOI-01",
    cause: str = "Existing cause",
) -> str:
    """Insert one incident that a later preview must recognise."""

    machine = session.scalar(
        select(Machine).where(Machine.machine_code == machine_code)
    )

    session.add(
        DowntimeIncident(
            incident_id=incident_id,
            machine_id=machine.id,
            production_line="Line 1",
            date=datetime.date(2026, 7, 1),
            cause=cause,
            downtime_minutes=10,
            status="Closed",
        )
    )
    session.commit()

    return incident_id


# ---------------------------------------------------------------------
# Template download
# ---------------------------------------------------------------------


def test_xlsx_template_downloads_the_step_one_workbook(client):
    response = client.get(TEMPLATE_URL)

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_CONTENT_TYPE
    assert response.headers["content-disposition"] == (
        'attachment; filename="machinecare-incident-import-template.xlsx"'
    )
    # openpyxl stamps dcterms:created in docProps/core.xml, so two builds
    # of the same template differ in that member only.
    assert workbook_members(response.content) == workbook_members(
        build_xlsx_template()
    )

    workbook = load_workbook(io.BytesIO(response.content))

    assert workbook.sheetnames == ["Instructions", "Incidents", "Example"]
    assert [cell.value for cell in workbook["Incidents"][1]] == list(
        IMPORT_COLUMNS
    )
    assert workbook["Incidents"].max_row == 1


def test_csv_template_downloads_a_bom_prefixed_header(client):
    response = client.get(TEMPLATE_URL, params={"format": "csv"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="machinecare-incident-import-template.csv"'
    )
    assert response.content == build_csv_template()
    assert response.content.startswith(b"\xef\xbb\xbf")

    rows = list(
        csv.reader(io.StringIO(response.content.decode("utf-8-sig")))
    )

    assert rows == [list(IMPORT_COLUMNS)]


def test_template_format_is_case_insensitive(client):
    response = client.get(TEMPLATE_URL, params={"format": "CSV"})

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_unknown_template_format_is_rejected(client):
    response = client.get(TEMPLATE_URL, params={"format": "pdf"})

    assert response.status_code == 422
    assert response.json() == {"detail": "format must be xlsx or csv."}


def test_template_download_does_not_query_the_database(
    client,
    sql_statements,
):
    client.get(TEMPLATE_URL)
    client.get(TEMPLATE_URL, params={"format": "csv"})

    assert sql_statements == []


# ---------------------------------------------------------------------
# Machine list
# ---------------------------------------------------------------------


def test_machine_list_returns_the_codes_for_the_picker(client):
    response = client.get(MACHINES_URL)

    assert response.status_code == 200
    assert response.json() == {
        "machines": [
            {
                "code": "AOI-01",
                "name": "Automated Optical Inspection",
                "production_line": "Line 1",
            },
            {
                "code": "AOI-02",
                "name": "Automated Optical Inspection",
                "production_line": "Line 2",
            },
            {
                "code": "Pick & Place-03",
                "name": "Pick and Place",
                "production_line": "Line 3",
            },
        ]
    }


def test_machine_list_hides_database_ids(client):
    body = client.get(MACHINES_URL).json()

    assert all("id" not in machine for machine in body["machines"])


def test_machine_list_failure_returns_a_safe_error(client, monkeypatch):
    from app.repositories import incident_repository

    def boom(db):
        raise RuntimeError(
            "could not connect: postgresql://user:hunter2@db:5432/prod"
        )

    monkeypatch.setattr(incident_repository, "get_machines", boom)

    response = client.get(MACHINES_URL)

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to load the machine codes."}
    assert "hunter2" not in response.text
    assert "postgresql" not in response.text
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------
# Successful previews
# ---------------------------------------------------------------------


def test_valid_preview_returns_new_rows(client):
    content = csv_bytes([row_values()])
    response = post_preview(client, content)

    assert response.status_code == 200
    assert response.json() == {
        "file_name": "incidents.csv",
        "file_sha256": hashlib.sha256(content).hexdigest(),
        "skip_existing": False,
        "total_rows": 1,
        "valid_rows": 1,
        "invalid_rows": 0,
        "skipped_existing_rows": 0,
        "eligible_rows": 1,
        "rows": [
            {
                "row": 2,
                "incident_id": "DT-2026-101",
                "date": "2026-08-01",
                "machine": "AOI-01",
                "production_line": "Line 1",
                "downtime_minutes": 25,
                "state": "new",
            }
        ],
        "errors": [],
    }


def test_preview_keeps_the_incident_production_line(client):
    response = post_preview(
        client,
        csv_bytes([row_values(**{"Production Line": "Line 9"})]),
    )

    row = response.json()["rows"][0]

    assert row["production_line"] == "Line 9"
    assert row["machine"] == "AOI-01"


def test_xlsx_upload_is_previewed(client):
    response = post_preview(
        client,
        xlsx_bytes([row_values()]),
        filename="incidents.xlsx",
    )

    assert response.status_code == 200
    assert response.json()["eligible_rows"] == 1


def test_excel_date_cell_is_accepted(client):
    response = post_preview(
        client,
        xlsx_bytes([row_values(**{"Date": datetime.date(2026, 8, 1)})]),
        filename="incidents.xlsx",
    )

    body = response.json()

    assert response.status_code == 200
    assert body["rows"][0]["date"] == "2026-08-01"


def test_blank_rows_are_ignored(client):
    blank = [""] * len(IMPORT_COLUMNS)

    body = post_preview(
        client,
        csv_bytes([blank, row_values(), blank]),
    ).json()

    assert body["total_rows"] == 1
    assert body["rows"][0]["row"] == 3


# ---------------------------------------------------------------------
# Unknown machines
# ---------------------------------------------------------------------


def test_unknown_machine_is_rejected(client):
    response = post_preview(
        client,
        csv_bytes([row_values(**{"Machine": "AOI-99"})]),
    )

    assert response.status_code == 200

    body = response.json()

    assert body["total_rows"] == 1
    assert body["valid_rows"] == 0
    assert body["invalid_rows"] == 1
    assert body["eligible_rows"] == 0
    assert body["rows"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["row"] == 2
    assert body["errors"][0]["field"] == "Machine"
    assert body["errors"][0]["code"] == "unknown_machine"
    assert "AOI-99" in body["errors"][0]["message"]


def test_machine_codes_must_match_exactly(client):
    body = post_preview(
        client,
        csv_bytes([row_values(**{"Machine": "aoi-01"})]),
    ).json()

    assert body["errors"][0]["code"] == "unknown_machine"
    assert body["eligible_rows"] == 0


def test_unknown_machine_never_creates_a_machine(
    client,
    import_session,
    sql_statements,
):
    post_preview(
        client,
        csv_bytes([row_values(**{"Machine": "AOI-99"})]),
    )

    assert import_session.scalar(
        select(func.count()).select_from(Machine)
    ) == len(MACHINES)
    assert write_statements(sql_statements) == []
    assert select_statements(sql_statements)


# ---------------------------------------------------------------------
# Existing incident IDs
# ---------------------------------------------------------------------


@pytest.mark.parametrize("skip_existing", [None, "false"])
def test_existing_incident_id_is_rejected_by_default(
    client,
    import_session,
    skip_existing,
):
    add_existing_incident(import_session, "DT-2026-101")

    response = post_preview(
        client,
        csv_bytes([row_values()]),
        skip_existing=skip_existing,
    )

    assert response.status_code == 200

    body = response.json()

    assert body["skip_existing"] is False
    assert body["total_rows"] == 1
    assert body["valid_rows"] == 0
    assert body["invalid_rows"] == 1
    assert body["skipped_existing_rows"] == 0
    assert body["eligible_rows"] == 0
    assert body["rows"] == []
    assert body["errors"][0]["row"] == 2
    assert body["errors"][0]["field"] == "Incident ID"
    assert body["errors"][0]["code"] == "existing_incident_id"
    assert "already exists" in body["errors"][0]["message"]


def test_existing_incident_is_skipped_when_requested(
    client,
    import_session,
):
    add_existing_incident(import_session, "DT-2026-101")

    response = post_preview(
        client,
        csv_bytes([row_values()]),
        skip_existing="true",
    )

    assert response.status_code == 200

    body = response.json()

    assert body["skip_existing"] is True
    assert body["total_rows"] == 1
    assert body["valid_rows"] == 1
    assert body["invalid_rows"] == 0
    assert body["skipped_existing_rows"] == 1
    assert body["eligible_rows"] == 0
    assert body["errors"] == []
    assert body["rows"][0]["state"] == "skipped_existing"


def test_skipping_never_updates_the_existing_incident(
    client,
    import_session,
):
    add_existing_incident(import_session, "DT-2026-101", cause="Original")

    post_preview(
        client,
        csv_bytes([
            row_values(
                **{
                    "Cause": "Replacement cause",
                    "Downtime (min)": 480,
                }
            )
        ]),
        skip_existing="true",
    )

    import_session.expire_all()

    incident = import_session.scalar(
        select(DowntimeIncident).where(
            DowntimeIncident.incident_id == "DT-2026-101"
        )
    )

    assert incident.cause == "Original"
    assert incident.downtime_minutes == 10


# ---------------------------------------------------------------------
# Duplicate IDs inside the file
# ---------------------------------------------------------------------


@pytest.mark.parametrize("skip_existing", [None, "true"])
def test_duplicate_ids_are_rejected_whatever_skip_existing_says(
    client,
    skip_existing,
):
    body = post_preview(
        client,
        csv_bytes([row_values(), row_values()]),
        skip_existing=skip_existing,
    ).json()

    codes = [error["code"] for error in body["errors"]]

    assert "duplicate_incident_id" in codes
    assert body["total_rows"] == 2
    assert body["valid_rows"] == 1
    assert body["invalid_rows"] == 1
    assert body["skipped_existing_rows"] == 0
    assert body["eligible_rows"] == 1


def test_a_duplicate_of_an_existing_id_is_still_a_duplicate(
    client,
    import_session,
):
    add_existing_incident(import_session, "DT-2026-101")

    body = post_preview(
        client,
        csv_bytes([row_values(), row_values()]),
        skip_existing="true",
    ).json()

    assert body["total_rows"] == 2
    assert body["skipped_existing_rows"] == 1
    assert body["invalid_rows"] == 1
    assert body["eligible_rows"] == 0
    assert [error["code"] for error in body["errors"]] == [
        "duplicate_incident_id"
    ]
    assert body["errors"][0]["row"] == 3


# ---------------------------------------------------------------------
# Mixed files and counts
# ---------------------------------------------------------------------


def test_mixed_rows_report_counts_that_sum_to_the_total(
    client,
    import_session,
):
    add_existing_incident(import_session, "DT-2026-102", machine_code="AOI-02")

    rows = [
        row_values(),
        row_values(
            **{"Incident ID": "DT-2026-102", "Machine": "AOI-02"}
        ),
        row_values(
            **{"Incident ID": "DT-2026-103", "Machine": "AOI-99"}
        ),
        row_values(**{"Incident ID": "DT-2026-104", "Status": "Open"}),
    ]

    body = post_preview(
        client,
        csv_bytes(rows),
        skip_existing="true",
    ).json()

    assert body["total_rows"] == 4
    assert body["valid_rows"] == 2
    assert body["skipped_existing_rows"] == 1
    assert body["invalid_rows"] == 2
    assert body["eligible_rows"] == 1

    assert body["total_rows"] == (
        body["invalid_rows"]
        + body["skipped_existing_rows"]
        + body["eligible_rows"]
    )
    assert body["valid_rows"] == (
        body["skipped_existing_rows"] + body["eligible_rows"]
    )

    assert [(error["row"], error["code"]) for error in body["errors"]] == [
        (4, "unknown_machine"),
        (5, "invalid_status"),
    ]
    assert [
        (row["row"], row["state"]) for row in body["rows"]
    ] == [(2, "new"), (3, "skipped_existing")]


def test_field_errors_keep_their_row_and_code(client):
    body = post_preview(
        client,
        csv_bytes([
            row_values(**{"Downtime (min)": -5}),
            row_values(**{"Incident ID": "DT-26-101"}),
        ]),
    ).json()

    assert [
        (error["row"], error["field"], error["code"])
        for error in body["errors"]
    ] == [
        (2, "Downtime (min)", "out_of_range"),
        (3, "Incident ID", "invalid_format"),
    ]
    assert body["eligible_rows"] == 0


def test_a_file_with_only_invalid_rows_does_not_query_the_database(
    client,
    sql_statements,
):
    body = post_preview(
        client,
        csv_bytes([row_values(**{"Status": "Open"})]),
    ).json()

    assert body["invalid_rows"] == 1
    assert body["errors"][0]["code"] == "invalid_status"
    assert sql_statements == []


# ---------------------------------------------------------------------
# Rejected uploads
# ---------------------------------------------------------------------


def test_unsupported_extension_returns_415(client):
    response = post_preview(client, b"anything", filename="incidents.xls")

    assert response.status_code == 415
    assert ".csv or .xlsx" in response.json()["detail"]


def test_malformed_workbook_returns_415(client):
    response = post_preview(
        client,
        b"not a zip archive",
        filename="incidents.xlsx",
    )

    assert response.status_code == 415
    assert "not a valid .xlsx" in response.json()["detail"]


def test_zip_without_a_workbook_returns_415(client):
    import zipfile

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hello")

    response = post_preview(
        client,
        buffer.getvalue(),
        filename="incidents.xlsx",
    )

    assert response.status_code == 415
    assert "ZIP archive" in response.json()["detail"]


def test_oversized_upload_returns_413(client):
    response = post_preview(
        client,
        b"a" * (MAX_IMPORT_BYTES + 1),
        filename="incidents.csv",
    )

    assert response.status_code == 413
    assert "5 MB" in response.json()["detail"]


def test_empty_upload_returns_422(client):
    response = post_preview(client, b"")

    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


def test_missing_file_returns_422(client):
    response = client.post(PREVIEW_URL)

    assert response.status_code == 422


def test_missing_header_returns_422(client):
    header = [column for column in IMPORT_COLUMNS if column != "Cause"]
    row = [VALID_ROW[column] for column in header]

    response = post_preview(client, csv_bytes([row], header=header))

    assert response.status_code == 422
    assert "Cause" in response.json()["detail"]
    assert "missing" in response.json()["detail"]


def test_headers_only_file_returns_422(client):
    response = post_preview(client, csv_bytes([]))

    assert response.status_code == 422
    assert "no incident rows" in response.json()["detail"]


# ---------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------


def test_database_failure_returns_a_safe_error(client, monkeypatch):
    from app.repositories import incident_repository

    def boom(db, machine_codes):
        raise RuntimeError(
            "connection failed: postgresql://user:hunter2@db:5432/prod"
        )

    monkeypatch.setattr(
        incident_repository,
        "find_known_machine_codes",
        boom,
    )

    response = post_preview(client, csv_bytes([row_values()]))

    assert response.status_code == 500
    assert response.json() == {
        "detail": (
            "Failed to check the import file against the incident database."
        )
    }

    for secret in ("hunter2", "postgresql", "RuntimeError", "Traceback"):
        assert secret not in response.text


# ---------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------


def test_preview_executes_only_select_statements(
    client,
    import_session,
    sql_statements,
):
    add_existing_incident(import_session, "DT-2026-102", machine_code="AOI-02")
    sql_statements.clear()

    response = post_preview(
        client,
        csv_bytes([
            row_values(),
            row_values(
                **{"Incident ID": "DT-2026-102", "Machine": "AOI-02"}
            ),
        ]),
        skip_existing="true",
    )

    assert response.status_code == 200
    assert sql_statements, "the preview must check the database"
    assert write_statements(sql_statements) == []
    assert select_statements(sql_statements)

    # No pending ORM changes: nothing was added, modified or removed.
    assert list(import_session.new) == []
    assert list(import_session.dirty) == []
    assert list(import_session.deleted) == []


def test_preview_leaves_the_stored_rows_unchanged(
    client,
    import_session,
):
    add_existing_incident(import_session, "DT-2026-102", machine_code="AOI-02")

    before = (
        import_session.scalar(
            select(func.count()).select_from(DowntimeIncident)
        ),
        import_session.scalar(select(func.count()).select_from(Machine)),
    )

    post_preview(
        client,
        csv_bytes([
            row_values(),
            row_values(**{"Incident ID": "DT-2026-103"}),
        ]),
    )

    import_session.expire_all()

    after = (
        import_session.scalar(
            select(func.count()).select_from(DowntimeIncident)
        ),
        import_session.scalar(select(func.count()).select_from(Machine)),
    )

    assert after == before
    assert after == (1, len(MACHINES))


def test_preview_does_not_touch_the_vector_store(client, monkeypatch):
    from app.services import vector_store_service

    collection = MagicMock()
    monkeypatch.setattr(vector_store_service, "collection", collection)

    response = post_preview(
        client,
        csv_bytes([row_values()]),
        skip_existing="true",
    )

    assert response.status_code == 200
    assert collection.mock_calls == []


def test_api_startup_imports_neither_the_database_nor_the_vector_store():
    """Importing the API must not open PostgreSQL, ChromaDB or models."""

    script = (
        "import sys\n"
        "import app.api.main\n"
        "blocked = sorted(\n"
        "    name for name in (\n"
        "        'chromadb',\n"
        "        'groq',\n"
        "        'app.db.database',\n"
        "        'app.models',\n"
        "        'app.repositories.incident_repository',\n"
        "        'app.services.incident_import_commit',\n"
        "        'app.services.rag_service',\n"
        "        'app.services.vector_store_service',\n"
        "    )\n"
        "    if name in sys.modules\n"
        ")\n"
        "print(','.join(blocked))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
