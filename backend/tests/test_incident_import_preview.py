"""Unit tests for the database-aware import preview.

The preview is a pure function of a parser result and two sets of
strings, so these tests build both directly and never open PostgreSQL or
ChromaDB.
"""

import csv
import datetime
import io

import pytest

from app.services.incident_import_preview import (
    EXISTING_INCIDENT_ID,
    STATE_NEW,
    STATE_SKIPPED,
    UNKNOWN_MACHINE,
    build_import_preview,
    collect_lookup_values,
)
from app.services.incident_import_service import (
    IMPORT_COLUMNS,
    ImportRowError,
    IncidentImportResult,
    IncidentImportRow,
    parse_incident_import,
)


KNOWN_MACHINES = {"AOI-01", "AOI-02"}

VALID_ROW = {
    "Incident ID": "DT-2026-101",
    "Date": "2026-08-01",
    "Production Line": "Line 1",
    "Machine": "AOI-01",
    "Product": "Module A",
    "Downtime (min)": 25,
    "Issue Category": "Machine",
    "Cause": "Camera calibration failed",
    "Action Taken": "Recalibrated camera",
    "Root Cause": "Camera drift",
    "Responsible Team": "Maintenance",
    "Status": "Closed",
    "Preventive Action": "Weekly calibration schedule",
}


def make_row(
    row_number: int = 2,
    incident_id: str = "DT-2026-101",
    machine: str = "AOI-01",
) -> IncidentImportRow:
    return IncidentImportRow(
        row_number=row_number,
        incident_id=incident_id,
        date=datetime.date(2026, 8, 1),
        machine=machine,
        status="Closed",
        downtime_minutes=25,
        cause="Camera calibration failed",
        root_cause="Camera drift",
        action_taken="Recalibrated camera",
        production_line="Line 1",
    )


def row_values(**overrides) -> list[object]:
    values = dict(VALID_ROW)
    values.update(overrides)
    return [values[column] for column in IMPORT_COLUMNS]


def csv_content(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(IMPORT_COLUMNS))

    for row in rows:
        writer.writerow(row)

    return buffer.getvalue().encode("utf-8")


def parsed(
    rows=(),
    errors=(),
    total_rows: int | None = None,
) -> IncidentImportResult:
    return IncidentImportResult(
        file_name="incidents.csv",
        total_rows=len(rows) if total_rows is None else total_rows,
        valid_rows=tuple(rows),
        errors=tuple(errors),
    )


def error_codes(preview) -> list[str]:
    return [error.code for error in preview.errors]


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------


def test_new_rows_are_eligible():
    preview = build_import_preview(
        parsed([make_row(), make_row(3, "DT-2026-102")]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
    )

    assert preview.is_valid
    assert preview.total_rows == 2
    assert preview.valid_row_count == 2
    assert preview.invalid_row_count == 0
    assert preview.skipped_row_count == 0
    assert preview.eligible_row_count == 2
    assert {item.state for item in preview.rows} == {STATE_NEW}


def test_preview_carries_the_parser_row_unchanged():
    row = make_row()

    preview = build_import_preview(
        parsed([row]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
    )

    assert preview.rows[0].row is row
    assert preview.rows[0].row.production_line == "Line 1"


def test_unknown_machine_is_a_row_error():
    preview = build_import_preview(
        parsed([make_row(machine="AOI-99")]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
    )

    assert not preview.is_valid
    assert preview.valid_row_count == 0
    assert preview.eligible_row_count == 0
    assert preview.invalid_row_count == 1
    assert error_codes(preview) == [UNKNOWN_MACHINE]

    error = preview.errors[0]

    assert error.row == 2
    assert error.field == "Machine"
    assert "AOI-99" in error.message
    assert "never creates machines" in error.message


def test_machine_matching_is_exact():
    preview = build_import_preview(
        parsed([make_row(machine="aoi-01")]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
    )

    assert error_codes(preview) == [UNKNOWN_MACHINE]


def test_existing_id_is_rejected_by_default():
    preview = build_import_preview(
        parsed([make_row()]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-101"},
    )

    assert not preview.is_valid
    assert preview.valid_row_count == 0
    assert preview.invalid_row_count == 1
    assert preview.skipped_row_count == 0
    assert preview.eligible_row_count == 0
    assert error_codes(preview) == [EXISTING_INCIDENT_ID]
    assert preview.errors[0].field == "Incident ID"


def test_existing_id_is_skipped_when_requested():
    preview = build_import_preview(
        parsed([make_row()]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-101"},
        skip_existing=True,
    )

    assert preview.is_valid
    assert preview.skip_existing is True
    assert preview.valid_row_count == 1
    assert preview.skipped_row_count == 1
    assert preview.eligible_row_count == 0
    assert preview.invalid_row_count == 0
    assert preview.rows[0].state == STATE_SKIPPED
    assert preview.skipped_rows == preview.rows
    assert preview.eligible_rows == ()


def test_skip_existing_keeps_new_rows_eligible():
    preview = build_import_preview(
        parsed([make_row(), make_row(3, "DT-2026-102")]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-102"},
        skip_existing=True,
    )

    assert preview.valid_row_count == 2
    assert preview.skipped_row_count == 1
    assert preview.eligible_row_count == 1
    assert preview.skipped_rows[0].row.incident_id == "DT-2026-102"
    assert preview.eligible_rows[0].row.incident_id == "DT-2026-101"


def test_unknown_machine_wins_over_an_existing_id():
    preview = build_import_preview(
        parsed([make_row(machine="AOI-99")]),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-101"},
        skip_existing=True,
    )

    assert preview.valid_row_count == 0
    assert preview.skipped_row_count == 0
    assert error_codes(preview) == [UNKNOWN_MACHINE]


# ---------------------------------------------------------------------
# Counts and errors
# ---------------------------------------------------------------------


def test_counts_satisfy_the_documented_invariant():
    parser_error = ImportRowError(
        4,
        "Status",
        "invalid_status",
        "Status must be Closed.",
    )
    preview = build_import_preview(
        parsed(
            [make_row(), make_row(3, "DT-2026-102")],
            errors=[parser_error],
            total_rows=3,
        ),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-102"},
    )

    assert preview.total_rows == 3
    assert preview.eligible_row_count == 1
    assert preview.skipped_row_count == 0
    assert preview.invalid_row_count == 2
    assert preview.valid_row_count == 1

    assert preview.total_rows == (
        preview.invalid_row_count
        + preview.skipped_row_count
        + preview.eligible_row_count
    )


def test_invalid_rows_count_rows_not_errors():
    errors = [
        ImportRowError(2, "Cause", "required", "Cause is required."),
        ImportRowError(2, "Root Cause", "required", "Root Cause is required."),
    ]
    preview = build_import_preview(
        parsed(errors=errors, total_rows=1),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
    )

    assert len(preview.errors) == 2
    assert preview.invalid_row_count == 1


def test_parser_errors_are_preserved_and_ordered_by_row():
    parser_errors = [
        ImportRowError(5, "Date", "invalid_date", "Bad date."),
        ImportRowError(2, "Machine", "required", "Machine is required."),
    ]
    preview = build_import_preview(
        parsed([make_row(3, "DT-2026-103")], parser_errors, total_rows=3),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
    )

    assert [(error.row, error.code) for error in preview.errors] == [
        (2, "required"),
        (5, "invalid_date"),
    ]


def test_rows_absent_from_the_parser_result_are_never_classified():
    """A row the parser rejected is not checked against the database."""

    parser_error = ImportRowError(2, "Cause", "required", "Cause is required.")

    preview = build_import_preview(
        parsed(errors=[parser_error], total_rows=1),
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-101"},
        skip_existing=True,
    )

    assert preview.rows == ()
    assert error_codes(preview) == ["required"]


# ---------------------------------------------------------------------
# Lookup values
# ---------------------------------------------------------------------


def test_lookup_values_are_unique_and_sorted():
    machine_codes, incident_ids = collect_lookup_values(
        parsed([
            make_row(2, "DT-2026-103", "AOI-02"),
            make_row(3, "DT-2026-101", "AOI-01"),
            make_row(4, "DT-2026-102", "AOI-01"),
        ])
    )

    assert machine_codes == ("AOI-01", "AOI-02")
    assert incident_ids == ("DT-2026-101", "DT-2026-102", "DT-2026-103")


def test_lookup_values_of_a_file_without_valid_rows_are_empty():
    assert collect_lookup_values(parsed()) == ((), ())


# ---------------------------------------------------------------------
# Duplicate IDs are rejected by the parser, whatever skip_existing says
# ---------------------------------------------------------------------


@pytest.mark.parametrize("skip_existing", [False, True])
def test_duplicate_ids_in_one_file_are_always_rejected(skip_existing):
    parser_result = parse_incident_import(
        "incidents.csv",
        csv_content([row_values(), row_values()]),
    )

    preview = build_import_preview(
        parser_result,
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids=(),
        skip_existing=skip_existing,
    )

    assert error_codes(preview) == ["duplicate_incident_id"]
    assert preview.errors[0].row == 3
    assert preview.valid_row_count == 1
    assert preview.eligible_row_count == 1
    assert preview.total_rows == 2


def test_a_duplicate_of_an_existing_id_is_still_a_duplicate():
    parser_result = parse_incident_import(
        "incidents.csv",
        csv_content([row_values(), row_values()]),
    )

    preview = build_import_preview(
        parser_result,
        known_machine_codes=KNOWN_MACHINES,
        existing_incident_ids={"DT-2026-101"},
        skip_existing=True,
    )

    assert error_codes(preview) == ["duplicate_incident_id"]
    assert preview.skipped_row_count == 1
    assert preview.eligible_row_count == 0
    assert preview.total_rows == 2
