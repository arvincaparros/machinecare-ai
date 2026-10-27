"""Parsing and field validation for incident import files.

The parser is a pure function over bytes: it reads one CSV or XLSX
upload, validates every field against the incident schema limits and
returns structured rows plus row-level errors. It never opens
PostgreSQL, ChromaDB or the network, so the preview step can reuse it
unchanged and the commit step can trust its output.

A row is returned only when every field is valid. Errors carry the
spreadsheet row number and the header name, so the maintenance engineer
can fix the file without reading a traceback. File-level problems
(extension, encoding, workbook structure, size, row count, headers) are
raised as :class:`IncidentImportError`, which mirrors the
``DocumentError`` contract used by the chat attachment endpoint.

The import vocabulary is the seed-data vocabulary from
``data/incidents.json``, so the same records can be expressed as JSON,
CSV or XLSX without renaming anything.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from app.services.document_extraction import (
    MAX_XLSX_ENTRIES,
    MAX_XLSX_UNCOMPRESSED_BYTES,
    OLE2_SIGNATURE,
)


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5_000

# Absolute ceiling on worksheet rows walked by the reader. A workbook can
# report a formatted range of a million rows, so the reader stops early
# instead of streaming them all; real data is still capped by
# MAX_IMPORT_ROWS.
MAX_SCANNED_ROWS = 20_000

MAX_DOWNTIME_MINUTES = 1_440
ACCEPTED_STATUS = "Closed"

IMPORT_COLUMNS = (
    "Incident ID",
    "Date",
    "Production Line",
    "Machine",
    "Product",
    "Downtime (min)",
    "Issue Category",
    "Cause",
    "Action Taken",
    "Root Cause",
    "Responsible Team",
    "Status",
    "Preventive Action",
)

# "Machine Code" is the only accepted synonym; every other header must
# match a column name exactly (ignoring case and surrounding spaces).
HEADER_ALIASES = {"machine code": "Machine"}

REQUIRED_COLUMNS = (
    "Incident ID",
    "Date",
    "Machine",
    "Downtime (min)",
    "Cause",
    "Action Taken",
    "Root Cause",
    "Status",
)

# String columns without a database length limit (Text) are capped at
# 4,000 characters so one record cannot dominate a retrieval prompt.
MAX_LENGTHS = {
    "Incident ID": 50,
    "Production Line": 100,
    "Machine": 100,
    "Product": 150,
    "Issue Category": 100,
    "Cause": 4_000,
    "Action Taken": 4_000,
    "Root Cause": 4_000,
    "Responsible Team": 100,
    "Preventive Action": 4_000,
    "Status": 50,
}

DATA_SHEET_NAME = "Incidents"

# Sheets that belong to the template and must never be read as data.
RESERVED_SHEET_NAMES = frozenset({"instructions", "example", "examples"})

# The template's example rows use this reserved year so a copied example
# row is rejected instead of being imported as a real incident.
RESERVED_EXAMPLE_YEAR = "0000"
EXAMPLE_INCIDENT_IDS = (
    "DT-0000-001",
    "DT-0000-002",
    "DT-0000-003",
)

INCIDENT_ID_PATTERN = re.compile(r"DT-(\d{4})-(\d{3})")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
INTEGER_PATTERN = re.compile(r"-?\d+")

_CANONICAL_HEADERS = {
    column.casefold(): column
    for column in IMPORT_COLUMNS
}
_CANONICAL_HEADERS.update(HEADER_ALIASES)


class IncidentImportError(Exception):
    """A rejected import file with an HTTP status and a safe message."""

    def __init__(
        self,
        message: str,
        status_code: int = 422,
        code: str = "invalid_import",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class ImportRowError:
    """One field-level problem on one spreadsheet row."""

    row: int
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class IncidentImportRow:
    """One validated incident and its resolution, ready for commit."""

    row_number: int
    incident_id: str
    date: datetime.date
    machine: str
    status: str
    downtime_minutes: int
    cause: str
    root_cause: str
    action_taken: str
    production_line: str | None = None
    product: str | None = None
    issue_category: str | None = None
    responsible_team: str | None = None
    preventive_action: str | None = None


@dataclass(frozen=True)
class IncidentImportResult:
    """The validated rows plus every error found in the file."""

    file_name: str
    total_rows: int
    valid_rows: tuple[IncidentImportRow, ...]
    errors: tuple[ImportRowError, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def valid_row_count(self) -> int:
        return len(self.valid_rows)


@dataclass(frozen=True)
class _RawTable:
    """Rows read from the upload, before any field validation."""

    rows: list[tuple[int, list[object]]]
    formula_cells: frozenset[tuple[int, int]] = frozenset()


@dataclass
class _RowOutcome:
    """Validation result for one data row."""

    incident_id: str | None
    row: IncidentImportRow | None
    errors: list[ImportRowError]


def parse_incident_import(
    filename: str,
    content: bytes,
) -> IncidentImportResult:
    """Parse and validate one CSV or XLSX import file."""

    name = (filename or "").strip() or "import"
    extension = Path(name).suffix.lower()

    if extension not in (".csv", ".xlsx"):
        raise IncidentImportError(
            f"{name} cannot be imported. Upload a .csv or .xlsx file.",
            415,
            "unsupported_type",
        )

    if not content:
        raise IncidentImportError(
            f"{name} is empty. Fill in the template and try again.",
            422,
            "empty_file",
        )

    if len(content) > MAX_IMPORT_BYTES:
        raise IncidentImportError(
            f"{name} is larger than the "
            f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB import limit.",
            413,
            "file_too_large",
        )

    if extension == ".csv":
        table = _read_csv(name, content)
    else:
        table = _read_xlsx(name, content)

    return _validate_table(name, table)


# ---------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------


def _read_csv(filename: str, content: bytes) -> _RawTable:
    """Read a CSV upload into rows without validating any field."""

    text = _decode_csv(filename, content)
    rows = []

    try:
        for row_number, cells in enumerate(
            csv.reader(io.StringIO(text, newline="")),
            start=1,
        ):
            rows.append((row_number, list(cells)))
    except csv.Error as error:
        raise IncidentImportError(
            f"{filename} is not a valid CSV file ({error}). "
            "Save it again with comma separators and try.",
            422,
            "invalid_csv",
        ) from error

    return _RawTable(rows=rows)


def _decode_csv(filename: str, content: bytes) -> str:
    """Decode a CSV upload as strict UTF-8, tolerating a BOM."""

    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise IncidentImportError(
            f"{filename} is UTF-16 encoded. Save it as UTF-8 CSV "
            "(comma delimited) and try again.",
            422,
            "invalid_encoding",
        )

    if b"\x00" in content[:4096]:
        raise IncidentImportError(
            f"{filename} does not look like a text CSV file.",
            415,
            "binary_content",
        )

    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise IncidentImportError(
            f"{filename} is not valid UTF-8 text. Save it as UTF-8 CSV "
            "(comma delimited) and try again.",
            422,
            "invalid_encoding",
        ) from error


# ---------------------------------------------------------------------
# XLSX reading
# ---------------------------------------------------------------------


def _read_xlsx(filename: str, content: bytes) -> _RawTable:
    """Read the Incidents worksheet of a workbook into rows."""

    _verify_xlsx_package(filename, content)

    try:
        values_workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
        formulas_workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
    except Exception as error:
        raise IncidentImportError(
            f"{filename} could not be read as an Excel workbook. "
            "Save it again as .xlsx and try.",
            422,
            "invalid_workbook",
        ) from error

    try:
        worksheet = _select_data_sheet(values_workbook, filename)
        formula_cells = _find_formula_cells(
            formulas_workbook,
            worksheet.title,
        )
        rows = list(_iter_sheet_rows(worksheet, filename))
    finally:
        values_workbook.close()
        formulas_workbook.close()

    return _RawTable(rows=rows, formula_cells=formula_cells)


def _verify_xlsx_package(filename: str, content: bytes) -> None:
    """Reject unsafe or non-XLSX containers before openpyxl parses them.

    The archive limits are shared with the chat attachment reader so the
    application has one set of ZIP safety caps.
    """

    if content.startswith(OLE2_SIGNATURE):
        raise IncidentImportError(
            f"{filename} is an encrypted workbook or an older .xls file "
            "renamed to .xlsx. Remove the password or save it as .xlsx.",
            415,
            "unsupported_type",
        )

    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise IncidentImportError(
            f"{filename} is not a valid .xlsx file.",
            415,
            "invalid_workbook",
        )

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError) as error:
        raise IncidentImportError(
            f"{filename} is not a valid .xlsx file.",
            415,
            "invalid_workbook",
        ) from error

    names = {entry.filename for entry in entries}

    if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
        raise IncidentImportError(
            f"{filename} is a ZIP archive without an Excel workbook "
            "inside it.",
            415,
            "invalid_workbook",
        )

    if len(entries) > MAX_XLSX_ENTRIES:
        raise IncidentImportError(
            f"{filename} contains more than {MAX_XLSX_ENTRIES} archive "
            "entries and was rejected.",
            413,
            "archive_too_large",
        )

    expanded = sum(entry.file_size for entry in entries)

    if expanded > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise IncidentImportError(
            f"{filename} expands to more than "
            f"{MAX_XLSX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB and was "
            "rejected.",
            413,
            "archive_too_large",
        )

    if any(
        entry.filename.lower().endswith("vbaproject.bin")
        or entry.filename.lower().startswith("xl/macrosheets/")
        for entry in entries
    ):
        raise IncidentImportError(
            f"{filename} is a macro-enabled workbook. Save it as a plain "
            ".xlsx file before importing.",
            415,
            "macro_enabled",
        )


def _select_data_sheet(workbook, filename: str):
    """Return the worksheet that holds incident rows."""

    sheets = list(workbook.worksheets)

    if not sheets:
        raise IncidentImportError(
            f"{filename} contains no worksheets.",
            422,
            "invalid_workbook",
        )

    selected = None

    for sheet in sheets:
        if sheet.title.strip().casefold() == DATA_SHEET_NAME.casefold():
            selected = sheet
            break

    if selected is None:
        visible = [
            sheet
            for sheet in sheets
            if sheet.sheet_state == "visible"
        ]

        if (
            len(visible) == 1
            and visible[0].title.strip().casefold()
            not in RESERVED_SHEET_NAMES
        ):
            selected = visible[0]
        elif visible and all(
            sheet.title.strip().casefold() in RESERVED_SHEET_NAMES
            for sheet in visible
        ):
            raise IncidentImportError(
                f"{filename} has no incident data sheet. "
                f"'{visible[0].title}' is part of the template and is "
                "never imported. Enter the rows on a sheet named "
                f"{DATA_SHEET_NAME}.",
                422,
                "missing_data_sheet",
            )
        else:
            raise IncidentImportError(
                f"{filename} has no '{DATA_SHEET_NAME}' worksheet. "
                "Rename the sheet that holds the incident rows to "
                f"{DATA_SHEET_NAME}.",
                422,
                "missing_data_sheet",
            )

    if selected.sheet_state != "visible":
        raise IncidentImportError(
            f"The '{selected.title}' worksheet is hidden. Unhide it "
            "before importing.",
            422,
            "invalid_workbook",
        )

    return selected


def _find_formula_cells(
    workbook,
    sheet_name: str,
) -> frozenset[tuple[int, int]]:
    """Locate formula cells so a missing cached value can be reported."""

    positions = set()
    worksheet = workbook[sheet_name]

    for row_number, cells in enumerate(
        worksheet.iter_rows(values_only=True),
        start=1,
    ):
        if row_number > MAX_SCANNED_ROWS:
            break

        for column_number, value in enumerate(cells, start=1):
            if isinstance(value, str) and value.startswith("="):
                positions.add((row_number, column_number))

    return frozenset(positions)


def _iter_sheet_rows(worksheet, filename: str):
    """Yield numbered rows, refusing workbooks with huge row ranges."""

    for row_number, cells in enumerate(
        worksheet.iter_rows(values_only=True),
        start=1,
    ):
        if row_number > MAX_SCANNED_ROWS:
            raise IncidentImportError(
                f"{filename} has more than {MAX_SCANNED_ROWS:,} rows. "
                "Delete the unused rows and try again.",
                413,
                "too_many_rows",
            )

        yield row_number, list(cells)


# ---------------------------------------------------------------------
# Header and row validation
# ---------------------------------------------------------------------


def _validate_table(
    filename: str,
    table: _RawTable,
) -> IncidentImportResult:
    """Validate headers and every data row of a parsed table."""

    rows = table.rows

    header_position = next(
        (
            position
            for position, (_, cells) in enumerate(rows)
            if _has_content(cells)
        ),
        None,
    )

    if header_position is None:
        raise IncidentImportError(
            f"{filename} is empty. Fill in the template and try again.",
            422,
            "empty_file",
        )

    _, header_cells = rows[header_position]
    columns = _normalize_headers(filename, header_cells)
    used_columns = frozenset(columns.values())

    data_rows = []
    total_rows = 0

    for row_number, cells in rows[header_position + 1:]:
        if not _has_content(cells):
            continue

        total_rows += 1

        if total_rows > MAX_IMPORT_ROWS:
            raise IncidentImportError(
                f"{filename} contains more than {MAX_IMPORT_ROWS:,} data "
                "rows. Split the file and import it in parts.",
                413,
                "too_many_rows",
            )

        data_rows.append((row_number, cells))

    if not data_rows:
        raise IncidentImportError(
            f"{filename} has a header row but no incident rows. Add at "
            "least one incident below the header.",
            422,
            "no_data_rows",
        )

    errors: list[ImportRowError] = []
    valid_rows: list[IncidentImportRow] = []
    first_seen: dict[str, int] = {}

    for row_number, cells in data_rows:
        outcome = _validate_row(
            row_number,
            cells,
            columns,
            used_columns,
            table.formula_cells,
        )

        if outcome.incident_id is not None:
            first_row = first_seen.get(outcome.incident_id)

            if first_row is None:
                first_seen[outcome.incident_id] = row_number
            else:
                outcome.errors.append(
                    ImportRowError(
                        row_number,
                        "Incident ID",
                        "duplicate_incident_id",
                        f"Incident ID {outcome.incident_id} is used more "
                        f"than once in this file (first on row "
                        f"{first_row}).",
                    )
                )
                outcome.row = None

        if outcome.errors:
            errors.extend(outcome.errors)
        elif outcome.row is not None:
            valid_rows.append(outcome.row)

    return IncidentImportResult(
        file_name=filename,
        total_rows=total_rows,
        valid_rows=tuple(valid_rows),
        errors=tuple(errors),
    )


def _normalize_headers(
    filename: str,
    cells: list[object],
) -> dict[str, int]:
    """Map each canonical column to its cell index."""

    if len(cells) == 1 and isinstance(cells[0], str) and ";" in cells[0]:
        raise IncidentImportError(
            f"{filename} uses semicolons between columns. Save it as a "
            "comma-separated CSV or as an .xlsx file.",
            422,
            "invalid_headers",
        )

    columns: dict[str, int] = {}
    unknown: list[str] = []
    duplicates: list[str] = []

    for index, raw in enumerate(cells):
        if _is_blank(raw):
            continue

        if not isinstance(raw, str):
            unknown.append(str(raw))
            continue

        name = raw.strip()
        canonical = _CANONICAL_HEADERS.get(name.casefold())

        if canonical is None:
            unknown.append(name)
            continue

        if canonical in columns:
            duplicates.append(name)
            continue

        columns[canonical] = index

    missing = [
        column
        for column in REQUIRED_COLUMNS
        if column not in columns
    ]

    problems = []

    if missing:
        problems.append("missing " + ", ".join(missing))

    if unknown:
        problems.append(
            "unknown " + ", ".join(sorted(set(unknown)))
        )

    if duplicates:
        problems.append(
            "duplicate " + ", ".join(sorted(set(duplicates)))
        )

    if problems:
        raise IncidentImportError(
            f"{filename} has invalid column headers "
            f"({'; '.join(problems)}). Use the template headers "
            "unchanged, or rename 'Machine Code' to 'Machine'.",
            422,
            "invalid_headers",
        )

    return columns


def _validate_row(
    row_number: int,
    cells: list[object],
    columns: dict[str, int],
    used_columns: frozenset[int],
    formula_cells: frozenset[tuple[int, int]],
) -> _RowOutcome:
    """Validate every field of one row, collecting all problems."""

    errors: list[ImportRowError] = []

    for index, raw in enumerate(cells):
        if index in used_columns:
            continue

        if _has_value(raw):
            errors.append(
                ImportRowError(
                    row_number,
                    f"column {index + 1}",
                    "unknown_header",
                    f"Column {index + 1} contains data but has no "
                    "header. Add a header or remove the value.",
                )
            )

    def cell(column: str) -> object:
        index = columns[column]
        return cells[index] if index < len(cells) else None

    def is_formula(column: str) -> bool:
        return (row_number, columns[column] + 1) in formula_cells

    incident_id = _text_field(
        errors,
        row_number,
        "Incident ID",
        cell("Incident ID"),
        is_formula("Incident ID"),
    )

    if incident_id is not None:
        incident_id = _check_incident_id(
            errors,
            row_number,
            incident_id,
        )

    incident_date = _date_field(
        errors,
        row_number,
        cell("Date"),
        is_formula("Date"),
    )

    machine = _text_field(
        errors,
        row_number,
        "Machine",
        cell("Machine"),
        is_formula("Machine"),
    )

    if machine is not None and machine.isdigit():
        errors.append(
            ImportRowError(
                row_number,
                "Machine",
                "invalid_machine_reference",
                "Machine must be the machine code (for example AOI-01), "
                "not a numeric machine ID or a machine name.",
            )
        )
        machine = None

    downtime_minutes = _downtime_field(
        errors,
        row_number,
        cell("Downtime (min)"),
        is_formula("Downtime (min)"),
    )

    status = _text_field(
        errors,
        row_number,
        "Status",
        cell("Status"),
        is_formula("Status"),
    )

    if status is not None and status != ACCEPTED_STATUS:
        errors.append(
            ImportRowError(
                row_number,
                "Status",
                "invalid_status",
                f"Status must be {ACCEPTED_STATUS}; this version imports "
                f"closed incidents only (found {status}).",
            )
        )
        status = None

    cause = _text_field(
        errors,
        row_number,
        "Cause",
        cell("Cause"),
        is_formula("Cause"),
    )
    action_taken = _text_field(
        errors,
        row_number,
        "Action Taken",
        cell("Action Taken"),
        is_formula("Action Taken"),
    )
    root_cause = _text_field(
        errors,
        row_number,
        "Root Cause",
        cell("Root Cause"),
        is_formula("Root Cause"),
    )

    production_line = _optional_text(
        errors,
        row_number,
        "Production Line",
        cell("Production Line"),
        is_formula("Production Line"),
    )
    product = _optional_text(
        errors,
        row_number,
        "Product",
        cell("Product"),
        is_formula("Product"),
    )
    issue_category = _optional_text(
        errors,
        row_number,
        "Issue Category",
        cell("Issue Category"),
        is_formula("Issue Category"),
    )
    responsible_team = _optional_text(
        errors,
        row_number,
        "Responsible Team",
        cell("Responsible Team"),
        is_formula("Responsible Team"),
    )
    preventive_action = _optional_text(
        errors,
        row_number,
        "Preventive Action",
        cell("Preventive Action"),
        is_formula("Preventive Action"),
    )

    row = None

    if not errors:
        row = IncidentImportRow(
            row_number=row_number,
            incident_id=incident_id,
            date=incident_date,
            machine=machine,
            status=status,
            downtime_minutes=downtime_minutes,
            cause=cause,
            root_cause=root_cause,
            action_taken=action_taken,
            production_line=production_line,
            product=product,
            issue_category=issue_category,
            responsible_team=responsible_team,
            preventive_action=preventive_action,
        )

    return _RowOutcome(
        incident_id=incident_id,
        row=row,
        errors=errors,
    )


def _check_incident_id(
    errors: list[ImportRowError],
    row_number: int,
    value: str,
) -> str | None:
    """Enforce the DT-YYYY-NNN business key."""

    match = INCIDENT_ID_PATTERN.fullmatch(value)

    if match is None:
        errors.append(
            ImportRowError(
                row_number,
                "Incident ID",
                "invalid_format",
                "Incident ID must look like DT-2026-001 "
                "(DT-YYYY-NNN).",
            )
        )
        return None

    if match.group(1) == RESERVED_EXAMPLE_YEAR:
        errors.append(
            ImportRowError(
                row_number,
                "Incident ID",
                "reserved_example_id",
                f"Incident ID {value} is reserved for template examples; "
                "enter the real incident ID.",
            )
        )
        return None

    return value


def _text_field(
    errors: list[ImportRowError],
    row_number: int,
    field: str,
    raw: object,
    is_formula: bool,
) -> str | None:
    """Validate one required text cell."""

    if _is_blank(raw):
        if is_formula:
            _formula_error(errors, row_number, field)
        else:
            errors.append(
                ImportRowError(
                    row_number,
                    field,
                    "required",
                    f"{field} is required.",
                )
            )
        return None

    if not isinstance(raw, str):
        errors.append(
            ImportRowError(
                row_number,
                field,
                "invalid_type",
                f"{field} must be text. Format the cell as Text.",
            )
        )
        return None

    text = raw.strip()
    max_length = MAX_LENGTHS[field]

    if len(text) > max_length:
        errors.append(
            ImportRowError(
                row_number,
                field,
                "too_long",
                f"{field} must be at most {max_length:,} characters "
                f"(found {len(text):,}).",
            )
        )
        return None

    return text


def _optional_text(
    errors: list[ImportRowError],
    row_number: int,
    field: str,
    raw: object,
    is_formula: bool,
) -> str | None:
    """Validate one optional text cell."""

    if _is_blank(raw):
        if is_formula:
            _formula_error(errors, row_number, field)
        return None

    if not isinstance(raw, str):
        errors.append(
            ImportRowError(
                row_number,
                field,
                "invalid_type",
                f"{field} must be text. Format the cell as Text.",
            )
        )
        return None

    text = raw.strip()
    max_length = MAX_LENGTHS[field]

    if len(text) > max_length:
        errors.append(
            ImportRowError(
                row_number,
                field,
                "too_long",
                f"{field} must be at most {max_length:,} characters "
                f"(found {len(text):,}).",
            )
        )
        return None

    return text


def _date_field(
    errors: list[ImportRowError],
    row_number: int,
    raw: object,
    is_formula: bool,
) -> datetime.date | None:
    """Accept a date-only cell or strict YYYY-MM-DD text."""

    if _is_blank(raw):
        if is_formula:
            _formula_error(errors, row_number, "Date")
        else:
            errors.append(
                ImportRowError(
                    row_number,
                    "Date",
                    "required",
                    "Date is required.",
                )
            )
        return None

    if isinstance(raw, datetime.datetime):
        if raw.time() != datetime.time(0, 0):
            errors.append(
                ImportRowError(
                    row_number,
                    "Date",
                    "date_has_time",
                    "Date must not include a time "
                    f"(found {raw.isoformat(sep=' ')}).",
                )
            )
            return None

        return raw.date()

    if isinstance(raw, datetime.date):
        return raw

    if isinstance(raw, str):
        text = raw.strip()

        if not DATE_PATTERN.fullmatch(text):
            errors.append(
                ImportRowError(
                    row_number,
                    "Date",
                    "invalid_date",
                    "Date must use the YYYY-MM-DD format, for example "
                    "2026-07-01.",
                )
            )
            return None

        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            errors.append(
                ImportRowError(
                    row_number,
                    "Date",
                    "invalid_date",
                    f"Date {text} is not a real calendar date.",
                )
            )
            return None

    errors.append(
        ImportRowError(
            row_number,
            "Date",
            "invalid_date",
            "Date must be a calendar date such as 2026-07-01; do not "
            "use a serial number or a time.",
        )
    )
    return None


def _downtime_field(
    errors: list[ImportRowError],
    row_number: int,
    raw: object,
    is_formula: bool,
) -> int | None:
    """Accept a whole number of minutes from 0 through 1,440."""

    field = "Downtime (min)"

    if _is_blank(raw):
        if is_formula:
            _formula_error(errors, row_number, field)
        else:
            errors.append(
                ImportRowError(
                    row_number,
                    field,
                    "required",
                    f"{field} is required.",
                )
            )
        return None

    if isinstance(raw, bool):
        errors.append(
            ImportRowError(
                row_number,
                field,
                "invalid_integer",
                f"{field} must be a whole number of minutes.",
            )
        )
        return None

    if isinstance(raw, int):
        minutes = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            errors.append(
                ImportRowError(
                    row_number,
                    field,
                    "invalid_integer",
                    f"{field} must be a whole number; fractions are not "
                    "accepted.",
                )
            )
            return None

        minutes = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()

        if not INTEGER_PATTERN.fullmatch(text):
            errors.append(
                ImportRowError(
                    row_number,
                    field,
                    "invalid_integer",
                    f"{field} must be a whole number of minutes between "
                    f"0 and {MAX_DOWNTIME_MINUTES}.",
                )
            )
            return None

        minutes = int(text)
    else:
        errors.append(
            ImportRowError(
                row_number,
                field,
                "invalid_integer",
                f"{field} must be a whole number of minutes between "
                f"0 and {MAX_DOWNTIME_MINUTES}.",
            )
        )
        return None

    if not 0 <= minutes <= MAX_DOWNTIME_MINUTES:
        errors.append(
            ImportRowError(
                row_number,
                field,
                "out_of_range",
                f"{field} must be between 0 and {MAX_DOWNTIME_MINUTES} "
                f"(found {minutes}).",
            )
        )
        return None

    return minutes


def _formula_error(
    errors: list[ImportRowError],
    row_number: int,
    field: str,
) -> None:
    """Report a formula whose cached result the workbook does not store."""

    errors.append(
        ImportRowError(
            row_number,
            field,
            "formula_without_value",
            f"{field} contains a formula with no stored result. Paste "
            "the value instead of the formula.",
        )
    )


def _is_blank(value: object) -> bool:
    """True for an empty cell or whitespace-only text."""

    return value is None or (
        isinstance(value, str) and not value.strip()
    )


def _has_value(value: object) -> bool:
    return not _is_blank(value)


def _has_content(cells: list[object]) -> bool:
    return any(_has_value(cell) for cell in cells)
