"""Downloadable CSV and XLSX templates for the incident import.

The template is generated from the parser's own constants, so a header,
limit or reserved example ID can never disagree with the validation
rules. The workbook has three sheets and only ``Incidents`` is read as
data:

- ``Instructions`` documents the format, the limits and every column.
- ``Incidents`` holds the header row and nothing else.
- ``Example`` shows three filled rows with IDs from the reserved
  ``DT-0000-###`` block, which the parser rejects if they are copied
  into real data.

The builders return bytes; writing a file or serving a download is the
caller's job.
"""

from __future__ import annotations

import csv
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from app.services.incident_import_service import (
    DATA_SHEET_NAME,
    EXAMPLE_INCIDENT_IDS,
    IMPORT_COLUMNS,
    MAX_DOWNTIME_MINUTES,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    MAX_LENGTHS,
    REQUIRED_COLUMNS,
)


COLUMN_RULES = {
    "Incident ID": (
        "Format DT-YYYY-NNN, for example DT-2026-101. "
        "DT-0000-### is reserved for the example rows."
    ),
    "Date": (
        "YYYY-MM-DD text or an Excel date cell. "
        "A time part is rejected."
    ),
    "Production Line": (
        f"Up to {MAX_LENGTHS['Production Line']} characters. "
        "Use the line the incident happened on."
    ),
    "Machine": (
        "Existing machine code, for example AOI-01. "
        "Not a machine name or a numeric ID."
    ),
    "Product": f"Up to {MAX_LENGTHS['Product']} characters.",
    "Downtime (min)": (
        f"Whole number from 0 to {MAX_DOWNTIME_MINUTES}."
    ),
    "Issue Category": (
        f"Up to {MAX_LENGTHS['Issue Category']} characters."
    ),
    "Cause": f"Up to {MAX_LENGTHS['Cause']:,} characters.",
    "Action Taken": (
        f"Up to {MAX_LENGTHS['Action Taken']:,} characters."
    ),
    "Root Cause": f"Up to {MAX_LENGTHS['Root Cause']:,} characters.",
    "Responsible Team": (
        f"Up to {MAX_LENGTHS['Responsible Team']} characters."
    ),
    "Status": "Must be Closed in this version.",
    "Preventive Action": (
        f"Up to {MAX_LENGTHS['Preventive Action']:,} characters."
    ),
}

COLUMN_WIDTHS = {
    "Incident ID": 14,
    "Date": 13,
    "Production Line": 17,
    "Machine": 22,
    "Product": 15,
    "Downtime (min)": 16,
    "Issue Category": 16,
    "Cause": 34,
    "Action Taken": 34,
    "Root Cause": 32,
    "Responsible Team": 19,
    "Status": 11,
    "Preventive Action": 32,
}

INSTRUCTION_LINES = (
    ("MachineCare AI — incident import template", True),
    ("", False),
    ("How to use", True),
    (
        "1. Enter one incident per row on the Incidents sheet, "
        "starting on row 2.",
        False,
    ),
    (
        "2. Keep the header row unchanged: do not rename, reorder, "
        "add or delete columns.",
        False,
    ),
    (
        "3. The Example sheet shows filled rows. Copy the values you "
        "need, then replace the reserved DT-0000-### IDs.",
        False,
    ),
    (
        "4. Save the file as .xlsx and upload it from the Import "
        "incidents dialog. A CSV edition also works: save it as UTF-8 "
        "with comma separators.",
        False,
    ),
    (
        f"5. Limits: {MAX_IMPORT_BYTES // (1024 * 1024)} MB per file "
        f"and {MAX_IMPORT_ROWS:,} data rows. Blank rows are ignored.",
        False,
    ),
    (
        "6. Only Closed incidents can be imported in this version.",
        False,
    ),
    ("", False),
    ("Column rules", True),
)

# Three rows copied from the project's own seed vocabulary. Their IDs use
# the reserved year so a copied row is rejected instead of imported.
EXAMPLE_ROWS = (
    {
        "Incident ID": EXAMPLE_INCIDENT_IDS[0],
        "Date": "2026-07-01",
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
    },
    {
        "Incident ID": EXAMPLE_INCIDENT_IDS[1],
        "Date": "2026-07-01",
        "Production Line": "Line 2",
        "Machine": "Pick & Place-03",
        "Product": "PCB X",
        "Downtime (min)": 40,
        "Issue Category": "Material",
        "Cause": "Feeder empty",
        "Action Taken": "Reloaded feeder and resumed production",
        "Root Cause": "Incorrect replenishment timing",
        "Responsible Team": "Production",
        "Status": "Closed",
        "Preventive Action": "Low-stock alert",
    },
    {
        "Incident ID": EXAMPLE_INCIDENT_IDS[2],
        "Date": "2026-07-02",
        "Production Line": "Line 3",
        "Machine": "Reflow Oven",
        "Product": "PCB X",
        "Downtime (min)": 55,
        "Issue Category": "Equipment",
        "Cause": "Temperature exceeded limit",
        "Action Taken": "Stopped oven and adjusted profile",
        "Root Cause": "Faulty temperature sensor",
        "Responsible Team": "Maintenance",
        "Status": "Closed",
        "Preventive Action": "Monthly sensor validation",
    },
)


def build_xlsx_template() -> bytes:
    """Build the three-sheet XLSX import template."""

    workbook = Workbook()

    instructions = workbook.active
    instructions.title = "Instructions"
    _write_instructions(instructions)

    incidents = workbook.create_sheet(DATA_SHEET_NAME)
    _write_header(incidents)

    example = workbook.create_sheet("Example")
    _write_header(example)

    for record in EXAMPLE_ROWS:
        example.append(
            [record[column] for column in IMPORT_COLUMNS]
        )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_csv_template() -> bytes:
    """Build the header-only CSV template, encoded for Excel."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(IMPORT_COLUMNS)
    return buffer.getvalue().encode("utf-8-sig")


def _write_header(worksheet) -> None:
    """Write the import headers with readable widths."""

    worksheet.append(list(IMPORT_COLUMNS))

    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")

    worksheet.freeze_panes = "A2"

    for index, column in enumerate(IMPORT_COLUMNS, start=1):
        worksheet.column_dimensions[
            get_column_letter(index)
        ].width = COLUMN_WIDTHS[column]


def _write_instructions(worksheet) -> None:
    """Write the usage steps and one line per column."""

    for text, bold in INSTRUCTION_LINES:
        worksheet.append([text or None])

        if bold and text:
            worksheet.cell(
                row=worksheet.max_row,
                column=1,
            ).font = Font(bold=True)

    for column in IMPORT_COLUMNS:
        requirement = (
            "required"
            if column in REQUIRED_COLUMNS
            else "optional"
        )

        worksheet.append(
            [
                f"{column} ({requirement}) — "
                f"{COLUMN_RULES[column]}"
            ]
        )

    worksheet.column_dimensions["A"].width = 118
