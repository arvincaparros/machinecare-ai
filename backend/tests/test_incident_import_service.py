"""Unit tests for the CSV/XLSX incident import parser.

Every file is generated in memory and the parser is a pure function over
bytes, so no test opens PostgreSQL, ChromaDB or the network.
"""

import csv
import datetime
import io
import zipfile

import pytest
from openpyxl import Workbook

from app.services import incident_import_service
from app.services.document_extraction import OLE2_SIGNATURE
from app.services.incident_import_service import (
    IMPORT_COLUMNS,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    IncidentImportError,
    parse_incident_import,
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


def make_row(**overrides) -> list[object]:
    """Build one row in header order with optional overrides."""

    values = dict(VALID_ROW)
    values.update(overrides)
    return [values[column] for column in IMPORT_COLUMNS]


def csv_file(
    rows: list[list[object]],
    header: list[str] | tuple[str, ...] = IMPORT_COLUMNS,
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(header))

    for row in rows:
        writer.writerow(row)

    return buffer.getvalue().encode("utf-8")


def xlsx_file(
    rows: list[list[object]],
    header: list[str] | tuple[str, ...] = IMPORT_COLUMNS,
    sheet_name: str = "Incidents",
    extra_sheets: tuple[tuple[str, list[list[object]], object], ...] = (),
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name

    if header:
        worksheet.append(list(header))

    for row in rows:
        worksheet.append(list(row))

    for name, extra_rows, extra_header in extra_sheets:
        sheet = workbook.create_sheet(name)

        if extra_header:
            sheet.append(list(extra_header))  # type: ignore[arg-type]

        for row in extra_rows:
            sheet.append(list(row))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def csv_result(rows, **kwargs):
    return parse_incident_import("incidents.csv", csv_file(rows, **kwargs))


def xlsx_result(rows, **kwargs):
    return parse_incident_import(
        "incidents.xlsx",
        xlsx_file(rows, **kwargs),
    )


def parse_error(filename: str, content: bytes) -> IncidentImportError:
    with pytest.raises(IncidentImportError) as excinfo:
        parse_incident_import(filename, content)

    return excinfo.value


def error_codes(result) -> list[str]:
    return [error.code for error in result.errors]


def add_zip_entry(content: bytes, name: str, data: bytes) -> bytes:
    buffer = io.BytesIO(content)

    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr(name, data)

    return buffer.getvalue()


# ---------------------------------------------------------------------
# Valid files
# ---------------------------------------------------------------------


def test_csv_returns_validated_rows():
    result = csv_result(
        [
            make_row(),
            make_row(**{"Incident ID": "DT-2026-102"}),
        ]
    )

    assert result.is_valid
    assert result.total_rows == 2
    assert result.valid_row_count == 2

    first = result.valid_rows[0]

    assert first.row_number == 2
    assert first.incident_id == "DT-2026-101"
    assert first.date == datetime.date(2026, 8, 1)
    assert first.machine == "AOI-01"
    assert first.status == "Closed"
    assert first.downtime_minutes == 25
    assert first.production_line == "Line 1"
    assert first.preventive_action == "Weekly calibration schedule"


def test_csv_with_utf8_bom_is_accepted():
    content = b"\xef\xbb\xbf" + csv_file([make_row()])

    result = parse_incident_import("incidents.csv", content)

    assert result.is_valid
    assert result.valid_rows[0].incident_id == "DT-2026-101"


def test_csv_with_invalid_utf8_is_rejected():
    content = b"Incident ID,Date\n\x80,2026-08-01\n"

    error = parse_error("incidents.csv", content)

    assert error.status_code == 422
    assert error.code == "invalid_encoding"
    assert "UTF-8" in error.message


def test_utf16_csv_is_rejected_with_a_targeted_message():
    content = csv_file([make_row()]).decode("utf-8").encode("utf-16")

    error = parse_error("incidents.csv", content)

    assert error.code == "invalid_encoding"
    assert "UTF-16" in error.message


def test_semicolon_delimited_csv_is_rejected():
    header = ";".join(IMPORT_COLUMNS)

    error = parse_error(
        "incidents.csv",
        f"{header}\n".encode(),
    )

    assert error.code == "invalid_headers"
    assert "semicolon" in error.message


def test_xlsx_returns_validated_rows():
    result = xlsx_result([make_row()])

    assert result.is_valid
    assert result.valid_rows[0].date == datetime.date(2026, 8, 1)


def test_single_sheet_with_a_plain_name_is_accepted():
    result = xlsx_result([make_row()], sheet_name="Import")

    assert result.is_valid
    assert result.valid_rows[0].incident_id == "DT-2026-101"


# ---------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------


def test_machine_code_header_alias_is_accepted():
    header = [
        "Machine Code" if column == "Machine" else column
        for column in IMPORT_COLUMNS
    ]

    result = csv_result([make_row()], header=header)

    assert result.is_valid
    assert result.valid_rows[0].machine == "AOI-01"


def test_missing_header_is_rejected():
    header = [
        column
        for column in IMPORT_COLUMNS
        if column != "Cause"
    ]
    row = [
        VALID_ROW[column]
        for column in header
    ]

    error = parse_error(
        "incidents.csv",
        csv_file([row], header=header),
    )

    assert error.status_code == 422
    assert error.code == "invalid_headers"
    assert "Cause" in error.message
    assert "missing" in error.message


def test_unknown_header_is_rejected():
    header = [
        "Couse" if column == "Cause" else column
        for column in IMPORT_COLUMNS
    ]
    values = dict(VALID_ROW)
    values["Couse"] = values["Cause"]
    row = [
        values[column]
        for column in header
    ]

    error = parse_error(
        "incidents.csv",
        csv_file([row], header=header),
    )

    assert error.code == "invalid_headers"
    assert "Couse" in error.message
    assert "unknown" in error.message


def test_duplicate_header_is_rejected():
    header = list(IMPORT_COLUMNS) + ["Machine Code"]
    row = make_row() + ["AOI-01"]

    error = parse_error(
        "incidents.csv",
        csv_file([row], header=header),
    )

    assert error.code == "invalid_headers"
    assert "duplicate" in error.message.lower()


def test_data_without_a_header_column_is_rejected():
    row = make_row() + ["Unexpected value"]

    result = csv_result([row], header=list(IMPORT_COLUMNS))

    assert error_codes(result) == ["unknown_header"]
    assert result.errors[0].field == "column 14"


# ---------------------------------------------------------------------
# Row counting and blank rows
# ---------------------------------------------------------------------


def test_blank_rows_are_ignored_and_rows_keep_excel_numbers():
    blank = [""] * len(IMPORT_COLUMNS)

    result = csv_result([blank, make_row(), blank])

    assert result.total_rows == 1
    assert result.valid_rows[0].row_number == 3


def test_xlsx_blank_rows_are_ignored():
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Incidents"
    worksheet.append([""] * len(IMPORT_COLUMNS))
    worksheet.append(list(IMPORT_COLUMNS))
    worksheet.append([""] * len(IMPORT_COLUMNS))
    worksheet.append(make_row())

    buffer = io.BytesIO()
    workbook.save(buffer)

    result = parse_incident_import("incidents.xlsx", buffer.getvalue())

    assert result.total_rows == 1
    assert result.valid_rows[0].row_number == 4


def test_headers_only_file_is_rejected():
    error = parse_error("incidents.csv", csv_file([]))

    assert error.status_code == 422
    assert error.code == "no_data_rows"


def test_more_than_the_row_limit_is_rejected():
    rows = [
        make_row(**{"Incident ID": f"DT-2026-{index:03d}"})
        for index in range(1, MAX_IMPORT_ROWS + 2)
    ]

    error = parse_error("incidents.csv", csv_file(rows))

    assert error.status_code == 413
    assert error.code == "too_many_rows"
    assert f"{MAX_IMPORT_ROWS:,}" in error.message


def test_workbook_beyond_the_scan_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(
        incident_import_service,
        "MAX_SCANNED_ROWS",
        4,
    )
    rows = [
        make_row(**{"Incident ID": f"DT-2026-{index:03d}"})
        for index in range(101, 105)
    ]

    error = parse_error(
        "incidents.xlsx",
        xlsx_file(rows),
    )

    assert error.status_code == 413
    assert error.code == "too_many_rows"


# ---------------------------------------------------------------------
# File-level rejections
# ---------------------------------------------------------------------


def test_unsupported_extension_is_rejected():
    error = parse_error("incidents.xls", b"anything")

    assert error.status_code == 415
    assert error.code == "unsupported_type"


def test_empty_file_is_rejected():
    error = parse_error("incidents.csv", b"")

    assert error.status_code == 422
    assert error.code == "empty_file"


def test_file_larger_than_five_megabytes_is_rejected():
    content = b"a" * (MAX_IMPORT_BYTES + 1)

    error = parse_error("incidents.csv", content)

    assert error.status_code == 413
    assert error.code == "file_too_large"
    assert "5 MB" in error.message


def test_non_zip_xlsx_is_rejected():
    error = parse_error("incidents.xlsx", b"not a workbook")

    assert error.status_code == 415
    assert error.code == "invalid_workbook"


def test_ole2_container_is_rejected():
    error = parse_error(
        "incidents.xlsx",
        OLE2_SIGNATURE + b"legacy or encrypted",
    )

    assert error.status_code == 415
    assert error.code == "unsupported_type"


def test_zip_without_a_workbook_is_rejected():
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", b"hello")

    error = parse_error("incidents.xlsx", buffer.getvalue())

    assert error.code == "invalid_workbook"


def test_macro_enabled_workbook_is_rejected():
    content = add_zip_entry(
        xlsx_file([make_row()]),
        "xl/vbaProject.bin",
        b"\x00" * 8,
    )

    error = parse_error("incidents.xlsx", content)

    assert error.status_code == 415
    assert error.code == "macro_enabled"


# ---------------------------------------------------------------------
# Worksheet selection
# ---------------------------------------------------------------------


def test_only_the_incidents_sheet_is_read():
    content = xlsx_file(
        [make_row()],
        extra_sheets=(
            (
                "Example",
                [make_row(**{"Incident ID": "DT-0000-001"})],
                IMPORT_COLUMNS,
            ),
        ),
    )

    result = parse_incident_import("incidents.xlsx", content)

    assert result.total_rows == 1
    assert result.valid_rows[0].incident_id == "DT-2026-101"


def test_example_only_workbook_is_rejected():
    content = xlsx_file(
        [make_row(**{"Incident ID": "DT-0000-001"})],
        sheet_name="Example",
    )

    error = parse_error("incidents.xlsx", content)

    assert error.code == "missing_data_sheet"
    assert "never imported" in error.message


def test_workbook_without_an_incidents_sheet_is_rejected():
    content = xlsx_file(
        [make_row()],
        sheet_name="Data",
        extra_sheets=(("Notes", [], None),),
    )

    error = parse_error("incidents.xlsx", content)

    assert error.code == "missing_data_sheet"
    assert "Incidents" in error.message


# ---------------------------------------------------------------------
# Incident ID
# ---------------------------------------------------------------------


def test_duplicate_incident_ids_are_rejected():
    result = csv_result([make_row(), make_row()])

    assert not result.is_valid
    assert result.total_rows == 2
    assert result.valid_row_count == 1

    error = result.errors[0]

    assert error.row == 3
    assert error.field == "Incident ID"
    assert error.code == "duplicate_incident_id"
    assert "row 2" in error.message


@pytest.mark.parametrize(
    "incident_id",
    [
        "DT-2026-01",
        "DT-2026-0001",
        "dt-2026-101",
        "DT-26-101",
        "ABC-2026-101",
        "DT-2026-ABC",
    ],
)
def test_invalid_incident_id_formats_are_rejected(incident_id):
    result = csv_result([make_row(**{"Incident ID": incident_id})])

    assert error_codes(result) == ["invalid_format"]
    assert "DT-YYYY-NNN" in result.errors[0].message


def test_reserved_example_id_is_rejected():
    result = csv_result([make_row(**{"Incident ID": "DT-0000-001"})])

    assert error_codes(result) == ["reserved_example_id"]
    assert "reserved" in result.errors[0].message


def test_incident_id_length_limit_is_enforced():
    long_id = "DT-2026-" + "1" * 43

    result = csv_result([make_row(**{"Incident ID": long_id})])

    assert error_codes(result) == ["too_long"]


# ---------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------


def test_xlsx_date_cell_is_accepted():
    result = xlsx_result(
        [make_row(**{"Date": datetime.datetime(2026, 8, 1)})]
    )

    assert result.is_valid
    assert result.valid_rows[0].date == datetime.date(2026, 8, 1)


def test_xlsx_date_cell_with_a_time_is_rejected():
    result = xlsx_result(
        [make_row(**{"Date": datetime.datetime(2026, 8, 1, 10, 30)})]
    )

    assert error_codes(result) == ["date_has_time"]
    assert "10:30" in result.errors[0].message


@pytest.mark.parametrize(
    "value",
    [
        "08/01/2026",
        "2026-8-1",
        "2026-08-01T00:00:00",
        "1 August 2026",
    ],
)
def test_non_iso_text_dates_are_rejected(value):
    result = xlsx_result([make_row(**{"Date": value})])

    assert error_codes(result) == ["invalid_date"]


def test_impossible_calendar_date_is_rejected():
    result = csv_result([make_row(**{"Date": "2026-02-30"})])

    assert error_codes(result) == ["invalid_date"]
    assert "real calendar date" in result.errors[0].message


def test_excel_serial_number_date_is_rejected():
    result = xlsx_result([make_row(**{"Date": 46235})])

    assert error_codes(result) == ["invalid_date"]


# ---------------------------------------------------------------------
# Machine, status and downtime
# ---------------------------------------------------------------------


def test_numeric_machine_code_is_rejected():
    result = csv_result([make_row(**{"Machine": "12"})])

    assert error_codes(result) == ["invalid_machine_reference"]


def test_numeric_cell_for_machine_is_rejected():
    result = xlsx_result([make_row(**{"Machine": 12})])

    assert error_codes(result) == ["invalid_type"]


@pytest.mark.parametrize("status", ["Open", "In Progress", "closed", "CLOSED"])
def test_status_other_than_closed_is_rejected(status):
    result = csv_result([make_row(**{"Status": status})])

    assert error_codes(result) == ["invalid_status"]
    assert "Closed" in result.errors[0].message


def test_downtime_accepts_zero_and_the_upper_bound():
    result = csv_result(
        [
            make_row(**{"Downtime (min)": 0}),
            make_row(
                **{
                    "Incident ID": "DT-2026-102",
                    "Downtime (min)": 1440,
                }
            ),
        ]
    )

    assert result.is_valid
    assert [row.downtime_minutes for row in result.valid_rows] == [
        0,
        1440,
    ]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (-5, "out_of_range"),
        (1441, "out_of_range"),
        (1.5, "invalid_integer"),
        ("25.0", "invalid_integer"),
        ("ten", "invalid_integer"),
        ("", "required"),
    ],
)
def test_invalid_downtime_values_are_rejected(value, code):
    result = csv_result([make_row(**{"Downtime (min)": value})])

    assert error_codes(result) == [code]


def test_boolean_downtime_cell_is_rejected():
    result = xlsx_result([make_row(**{"Downtime (min)": True})])

    assert error_codes(result) == ["invalid_integer"]


def test_downtime_range_message_names_the_bounds():
    result = csv_result([make_row(**{"Downtime (min)": 1441})])

    assert "0 and 1440" in result.errors[0].message


# ---------------------------------------------------------------------
# Required fields and string limits
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "Incident ID",
        "Date",
        "Machine",
        "Downtime (min)",
        "Cause",
        "Action Taken",
        "Root Cause",
        "Status",
    ],
)
def test_required_fields_are_enforced(field):
    result = csv_result([make_row(**{field: "   "})])

    assert error_codes(result) == ["required"]
    assert result.errors[0].field == field


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("Production Line", 100),
        ("Machine", 100),
        ("Product", 150),
        ("Issue Category", 100),
        ("Cause", 4000),
        ("Action Taken", 4000),
        ("Root Cause", 4000),
        ("Responsible Team", 100),
        ("Preventive Action", 4000),
    ],
)
def test_string_limits_are_enforced(field, limit):
    result = csv_result([make_row(**{field: "x" * (limit + 1)})])

    assert error_codes(result) == ["too_long"]
    assert f"{limit:,}" in result.errors[0].message


def test_optional_fields_accept_blank_values():
    result = csv_result(
        [
            make_row(
                **{
                    "Production Line": "",
                    "Product": " ",
                    "Issue Category": "",
                    "Responsible Team": "",
                    "Preventive Action": "",
                }
            )
        ]
    )

    assert result.is_valid

    row = result.valid_rows[0]

    assert row.production_line is None
    assert row.product is None
    assert row.issue_category is None
    assert row.responsible_team is None
    assert row.preventive_action is None


def test_one_invalid_row_does_not_hide_the_valid_rows():
    result = csv_result(
        [
            make_row(),
            make_row(
                **{
                    "Incident ID": "DT-2026-102",
                    "Status": "Open",
                }
            ),
        ]
    )

    assert not result.is_valid
    assert result.total_rows == 2
    assert result.valid_row_count == 1
    assert result.errors[0].row == 3


# ---------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------


def test_formula_without_a_cached_value_is_rejected():
    result = xlsx_result([make_row(**{"Cause": "=1+1"})])

    assert error_codes(result) == ["formula_without_value"]
    assert result.errors[0].field == "Cause"
    assert "Paste the value" in result.errors[0].message


def test_formula_in_an_optional_field_is_rejected():
    result = xlsx_result([make_row(**{"Preventive Action": "=1+1"})])

    assert error_codes(result) == ["formula_without_value"]
    assert result.errors[0].field == "Preventive Action"
