"""Unit tests for the downloadable incident import templates.

The builders return bytes, so nothing touches the filesystem, the
database or ChromaDB.
"""

import csv
import io

import pytest
from openpyxl import load_workbook

from app.services.incident_import_service import (
    IMPORT_COLUMNS,
    IncidentImportError,
    parse_incident_import,
)
from app.services.incident_import_template import (
    EXAMPLE_ROWS,
    build_csv_template,
    build_xlsx_template,
)


def load_template():
    return load_workbook(io.BytesIO(build_xlsx_template()))


def test_xlsx_template_has_three_sheets():
    assert load_template().sheetnames == [
        "Instructions",
        "Incidents",
        "Example",
    ]


def test_incidents_sheet_has_the_headers_and_no_data_rows():
    sheet = load_template()["Incidents"]

    headers = [cell.value for cell in sheet[1]]

    assert headers == list(IMPORT_COLUMNS)
    assert sheet.max_row == 1


def test_example_sheet_has_three_complete_rows():
    sheet = load_template()["Example"]

    rows = list(sheet.iter_rows(min_row=2, values_only=True))

    assert len(rows) == 3
    assert all(len(row) == len(IMPORT_COLUMNS) for row in rows)
    assert [row[0] for row in rows] == [
        record["Incident ID"]
        for record in EXAMPLE_ROWS
    ]
    assert all(
        str(row[0]).startswith("DT-0000-")
        for row in rows
    )


def test_instructions_sheet_documents_the_rules():
    sheet = load_template()["Instructions"]

    text = " ".join(
        str(cell.value)
        for row in sheet.iter_rows()
        for cell in row
        if cell.value
    )

    for fragment in (
        "Closed",
        "DT-YYYY-NNN",
        "5,000",
        "5 MB",
        "UTF-8",
        "Incidents",
        "DT-0000-###",
    ):
        assert fragment in text


def test_example_ids_are_rejected_when_copied_into_incident_data():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(IMPORT_COLUMNS))

    for record in EXAMPLE_ROWS:
        writer.writerow(
            [record[column] for column in IMPORT_COLUMNS]
        )

    result = parse_incident_import(
        "incidents.csv",
        buffer.getvalue().encode("utf-8"),
    )

    assert not result.is_valid
    assert result.valid_row_count == 0
    assert len(result.errors) == len(EXAMPLE_ROWS)
    assert {
        error.code
        for error in result.errors
    } == {"reserved_example_id"}


def test_template_as_shipped_contains_no_incident_rows():
    with pytest.raises(IncidentImportError) as excinfo:
        parse_incident_import("template.xlsx", build_xlsx_template())

    assert excinfo.value.code == "no_data_rows"


def test_csv_template_contains_only_the_header_row():
    content = build_csv_template()

    assert content.startswith(b"\xef\xbb\xbf")

    rows = list(
        csv.reader(io.StringIO(content.decode("utf-8-sig")))
    )

    assert rows == [list(IMPORT_COLUMNS)]
