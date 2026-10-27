"""In-memory text extraction for documents attached to a question.

Uploads are never persisted: each file is read into memory, validated,
converted to plain text and discarded with the request. The limits below
are the single source of truth shared by the API layer and the tests.

DOCX is read with the standard library (``zipfile`` plus
``xml.etree``) instead of a third-party parser so that the archive can be
inspected and size-limited before any XML is parsed. The same reasoning
applies to ``.xlsx``: the ZIP package is checked (macro parts, entry
count, uncompressed size) before ``openpyxl`` sees it, and the workbook
is opened read-only with cached values only, so no formula is ever
calculated. Legacy ``.xls`` files are read with ``xlrd``, which parses
the BIFF records and returns the cached results stored in the file.

Hidden content: hidden and very-hidden worksheets are extracted next to
the visible ones and labelled ``[hidden]``. Row and column visibility is
not distinguishable in read-only mode, so hidden rows and columns are
included like any other cell. Nothing is ever executed: no macros, no
formulas, no external workbook links.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import xlrd
from openpyxl import load_workbook
from pypdf import PdfReader


MAX_FILES = 3
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 20_000
MAX_CSV_ROWS = 5_000
MAX_PDF_PAGES = 300
MAX_DOCX_ENTRY_BYTES = 20 * 1024 * 1024

# Excel caps: they bound both the work done per workbook and the amount
# of text handed to the prompt builder.
MAX_WORKSHEETS = 10
MAX_EXCEL_ROWS = 500
MAX_EXCEL_COLUMNS = 20
MAX_EXCEL_CELLS = 4_000
MAX_EXCEL_CELL_CHARS = 400
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_XLSX_ENTRIES = 2_000

FORMULA_WITHOUT_VALUE = "[formula without cached value]"

ALLOWED_EXTENSIONS = (".pdf", ".txt", ".csv", ".docx", ".xlsx", ".xls")

# The compound-file header that all legacy Excel (and encrypted OOXML)
# workbooks start with.
OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Generic types a browser may send when it does not know the format.
GENERIC_CONTENT_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "application/unknown",
    }
)

# Declared types are a weak signal, never proof: the signature check
# below is what decides whether the bytes are accepted.
ALLOWED_CONTENT_TYPES = {
    ".pdf": frozenset({"application/pdf", "application/x-pdf"}),
    ".txt": frozenset({"text/plain", "text/x-plain", "application/text"}),
    ".csv": frozenset(
        {
            "text/csv",
            "application/csv",
            "text/plain",
            "application/vnd.ms-excel",
        }
    ),
    ".docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
            "application/zip",
            "application/x-zip-compressed",
        }
    ),
    ".xlsx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet",
            "application/zip",
            "application/x-zip-compressed",
        }
    ),
    ".xls": frozenset(
        {
            "application/vnd.ms-excel",
            "application/msexcel",
            "application/x-msexcel",
            "application/excel",
            "application/xls",
            "application/x-xls",
        }
    ),
}

WORD_NAMESPACE = (
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
)

# Control characters that survive decoding but are never useful text.
CONTROL_CHARACTER_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0e-\x1f\x7f]"
)

BLANK_LINE_PATTERN = re.compile(r"\n{3,}")


class DocumentError(Exception):
    """A rejected upload with an HTTP status and a safe message."""

    def __init__(
        self,
        message: str,
        status_code: int = 422,
        code: str = "invalid_document",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class UploadedFile:
    """One uploaded file already read into memory."""

    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class ExtractedDocument:
    """Plain text extracted from one accepted upload."""

    filename: str
    extension: str
    text: str
    characters: int
    truncated: bool


@dataclass(frozen=True)
class ExtractedText:
    """Text from one format, plus whether a format limit cut it short."""

    text: str
    truncated: bool = False


def file_extension(filename: str) -> str:
    """Return the lowercase extension of a browser-supplied name."""

    return Path(filename or "").suffix.lower()


def normalize_content_type(content_type: str | None) -> str:
    """Return the media type without parameters, lowercased."""

    return (content_type or "").split(";", 1)[0].strip().lower()


def _reject_binary(content: bytes, filename: str) -> None:
    """Refuse content that is obviously not text."""

    if b"\x00" in content[:4096]:
        raise DocumentError(
            f"{filename} looks like a binary file and cannot be read as "
            "text. Upload a text-based .txt or .csv file.",
            415,
            "binary_content",
        )


def _decode_text(content: bytes, filename: str) -> str:
    """Decode a text upload without ever raising UnicodeDecodeError."""

    _reject_binary(content, filename)

    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError as error:
            raise DocumentError(
                f"{filename} uses an unsupported UTF-16 encoding.",
                422,
                "invalid_encoding",
            ) from error

    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    # cp1252 maps every byte, so this is the last-resort decoding.
    text = content.decode("cp1252")

    replacement_ratio = text.count("\ufffd") / max(len(text), 1)

    if replacement_ratio > 0.02:
        raise DocumentError(
            f"{filename} is not valid UTF-8 or Windows-1252 text.",
            422,
            "invalid_encoding",
        )

    return text


def _clean_text(text: str) -> str:
    """Drop control characters and collapse runs of blank lines."""

    cleaned = CONTROL_CHARACTER_PATTERN.sub(" ", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = BLANK_LINE_PATTERN.sub("\n\n", cleaned)

    return cleaned.strip()


def _check_signature(
    extension: str,
    content: bytes,
    filename: str,
) -> None:
    """Verify the bytes match the claimed format."""

    if extension == ".pdf" and not content.startswith(b"%PDF-"):
        raise DocumentError(
            f"{filename} does not start with a PDF header, so it is not "
            "a valid PDF file.",
            415,
            "invalid_signature",
        )

    if extension == ".docx":
        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise DocumentError(
                f"{filename} is not a valid DOCX archive.",
                415,
                "invalid_signature",
            )

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())

        if "word/document.xml" not in names:
            raise DocumentError(
                f"{filename} is a ZIP archive without a Word document "
                "inside it.",
                415,
                "invalid_signature",
            )

    if extension == ".xlsx":
        # An encrypted workbook is an OLE2 container, not a ZIP package.
        if content.startswith(OLE2_SIGNATURE):
            raise DocumentError(
                f"{filename} is either an encrypted workbook or a legacy "
                ".xls file renamed to .xlsx. Remove the password, or "
                "attach it with its original extension.",
                422,
                "encrypted_document",
            )

        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise DocumentError(
                f"{filename} is not a valid .xlsx archive.",
                415,
                "invalid_signature",
            )

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
        except (zipfile.BadZipFile, OSError) as error:
            raise DocumentError(
                f"{filename} is not a valid .xlsx archive.",
                415,
                "invalid_signature",
            ) from error

        names = {info.filename for info in infos}

        if (
            "xl/workbook.xml" not in names
            or "[Content_Types].xml" not in names
        ):
            raise DocumentError(
                f"{filename} is a ZIP archive without an Excel workbook "
                "inside it.",
                415,
                "invalid_signature",
            )

        if len(infos) > MAX_XLSX_ENTRIES:
            raise DocumentError(
                f"{filename} contains more than {MAX_XLSX_ENTRIES} "
                "archive entries and was rejected.",
                413,
                "archive_too_large",
            )

        expanded = sum(info.file_size for info in infos)

        if expanded > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise DocumentError(
                f"{filename} expands to more than "
                f"{MAX_XLSX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB and "
                "was rejected.",
                413,
                "archive_too_large",
            )

        if any(
            info.filename.lower().endswith("vbaproject.bin")
            or info.filename.lower().startswith("xl/macrosheets/")
            for info in infos
        ):
            raise DocumentError(
                f"{filename} is a macro-enabled workbook. Save it as a "
                "plain .xlsx file before uploading it.",
                415,
                "macro_enabled",
            )

    if extension == ".xls" and not content.startswith(OLE2_SIGNATURE):
        raise DocumentError(
            f"{filename} does not start with the legacy Excel file "
            "signature. If it is a newer workbook, rename it to .xlsx; "
            "otherwise export it again as .xls.",
            415,
            "invalid_signature",
        )


def _extract_pdf(content: bytes, filename: str) -> str:
    """Extract text from a PDF with a guard on page count."""

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)

        if reader.is_encrypted:
            raise DocumentError(
                f"{filename} is password-protected. Remove the "
                "password before uploading it.",
                422,
                "encrypted_document",
            )

        pages = []

        for index, page in enumerate(reader.pages):
            if index >= MAX_PDF_PAGES:
                break

            pages.append(page.extract_text() or "")

        return "\n".join(pages)
    except DocumentError:
        raise
    except Exception as error:
        # pypdf raises several unrelated types for damaged files.
        raise DocumentError(
            f"{filename} could not be read as a PDF. The file may be "
            "damaged or use an unsupported encoding.",
            422,
            "unreadable_document",
        ) from error


def _parse_document_xml(xml_bytes: bytes, filename: str) -> ElementTree.Element:
    """Parse WordprocessingML with entity handling disabled.

    ``ElementTree`` never resolves external entities, and both
    ``<!DOCTYPE>`` and ``<!ENTITY>`` declarations are rejected outright
    before parsing. Entity expansion (including "billion laughs") needs a
    DTD, so refusing the declaration removes the attack surface without
    adding a second XML library.
    """

    if len(xml_bytes) > MAX_DOCX_ENTRY_BYTES:
        raise DocumentError(
            f"{filename} has an unusually large document body and was "
            "rejected.",
            413,
            "archive_too_large",
        )

    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        raise DocumentError(
            f"{filename} contains an unsupported XML document type "
            "declaration.",
            422,
            "unsafe_document",
        )

    try:
        return ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as error:
        raise DocumentError(
            f"{filename} contains malformed document XML.",
            422,
            "unreadable_document",
        ) from error


def _extract_docx(content: bytes, filename: str) -> str:
    """Extract paragraph text from a DOCX archive safely."""

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()

            if any(
                info.filename.lower().endswith("vbaproject.bin")
                for info in infos
            ):
                raise DocumentError(
                    f"{filename} is a macro-enabled document. Save it as "
                    "a plain .docx file before uploading it.",
                    415,
                    "macro_enabled",
                )

            total_bytes = sum(info.file_size for info in infos)

            if total_bytes > MAX_DOCX_ENTRY_BYTES:
                raise DocumentError(
                    f"{filename} expands to more than "
                    f"{MAX_DOCX_ENTRY_BYTES // (1024 * 1024)} MB and was "
                    "rejected.",
                    413,
                    "archive_too_large",
                )

            xml_bytes = archive.read("word/document.xml")
    except DocumentError:
        raise
    except (zipfile.BadZipFile, KeyError) as error:
        raise DocumentError(
            f"{filename} is not a readable DOCX document.",
            422,
            "unreadable_document",
        ) from error

    root = _parse_document_xml(xml_bytes, filename)

    paragraphs = []

    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        text = "".join(
            node.text or ""
            for node in paragraph.iter(f"{WORD_NAMESPACE}t")
        ).strip()

        if text:
            paragraphs.append(text)

    return "\n".join(paragraphs)


def _extract_csv(content: bytes, filename: str) -> str:
    """Parse CSV with the standard library and cap the row count."""

    text = _decode_text(content, filename)

    try:
        reader = csv.reader(io.StringIO(text))
        rows = []

        for index, row in enumerate(reader):
            if index >= MAX_CSV_ROWS:
                break

            cells = [cell.strip() for cell in row if cell.strip()]

            if cells:
                rows.append(" | ".join(cells))

        return "\n".join(rows)
    except csv.Error as error:
        raise DocumentError(
            f"{filename} is not valid CSV and could not be parsed.",
            422,
            "unreadable_document",
        ) from error


def _format_excel_value(value: object) -> tuple[str, bool]:
    """Render one cell value as text without evaluating anything.

    Returns the text plus whether the cell text hit the per-cell cap.
    """

    if value is None:
        return "", False

    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), False

    if isinstance(value, float):
        # Excel stores every number as a float, so 25 must not read 25.0.
        text = str(int(value)) if value.is_integer() else format(value, ".15g")
    elif isinstance(value, datetime.datetime):
        text = value.isoformat(sep=" ")
    elif isinstance(value, (datetime.date, datetime.time)):
        text = value.isoformat()
    elif isinstance(value, datetime.timedelta):
        text = str(value)
    else:
        text = str(value).strip()

    if len(text) > MAX_EXCEL_CELL_CHARS:
        return text[:MAX_EXCEL_CELL_CHARS].rstrip() + "…", True

    return text, False


def _add_note(notes: list[str], note: str) -> None:
    """Keep one copy of each truncation note per worksheet."""

    if note not in notes:
        notes.append(note)


class _TextBuilder:
    """Accumulate worksheet lines without exceeding the text limit."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.length = 0
        self.truncated = False

    def add(self, line: str) -> bool:
        if self.truncated:
            return False

        if self.length + len(line) + 1 > MAX_EXTRACTED_CHARS:
            self.lines.append(
                "[... extracted text limit reached; remaining cells are "
                "omitted ...]"
            )
            self.truncated = True
            return False

        self.lines.append(line)
        self.length += len(line) + 1

        return True

    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class _ExcelSheet:
    """One worksheet already reduced to displayable rows."""

    name: str
    hidden: bool
    rows: tuple[tuple[int, list[str]], ...]
    notes: tuple[str, ...] = ()


def _render_sheet(sheet: _ExcelSheet, builder: _TextBuilder) -> bool:
    """Write one worksheet; return whether it held any cell value."""

    heading = f"### Worksheet: {sheet.name}"

    if sheet.hidden:
        heading += " [hidden]"

    builder.add(heading)

    has_content = False
    blank_rows = 0

    for row_number, cells in sheet.rows:
        if not cells:
            blank_rows += 1
            continue

        if blank_rows:
            builder.add(f"[{blank_rows} blank row(s)]")
            blank_rows = 0

        has_content = True
        builder.add(f"Row {row_number}: " + " | ".join(cells))

    if blank_rows:
        builder.add(f"[{blank_rows} blank row(s)]")

    if not has_content:
        builder.add("(empty worksheet)")

    for note in sheet.notes:
        builder.add(note)

    return has_content


def _render_workbook(
    sheets: Sequence[_ExcelSheet],
    omitted: int,
    filename: str,
    builder: _TextBuilder,
) -> ExtractedText:
    """Render every collected worksheet and report the truncation state."""

    has_content = False

    for sheet in sheets:
        # Keep rendering even when the builder is full: add() is a no-op
        # from that point on, and the truncation flag is already set.
        has_content = _render_sheet(sheet, builder) or has_content

    if omitted:
        builder.add(
            f"[{omitted} worksheet(s) omitted: the {MAX_WORKSHEETS}-"
            f"worksheet or {MAX_EXCEL_CELLS}-cell limit was reached]"
        )

    if not has_content:
        raise DocumentError(
            f"{filename} contains no cell values. Attach a workbook with "
            "report data in it.",
            422,
            "empty_document",
        )

    return ExtractedText(
        text=builder.text(),
        truncated=(
            builder.truncated
            or omitted > 0
            or any(sheet.notes for sheet in sheets)
        ),
    )


def _xlsx_formula_cells(content: bytes) -> dict[str, set[tuple[int, int]]]:
    """Locate formula cells with a second, formula-preserving pass.

    ``data_only=True`` only exposes cached results, so a formula whose
    cached value was never stored is indistinguishable from a blank cell.
    This pass records the coordinates so the extraction can label them
    instead of silently dropping them. Failed passes simply return no
    coordinates: the values pass has already succeeded at that point.
    """

    try:
        workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
    except Exception:
        return {}

    try:
        positions: dict[str, set[tuple[int, int]]] = {}

        for worksheet in workbook.worksheets[:MAX_WORKSHEETS]:
            coordinates: set[tuple[int, int]] = set()

            for row_index, row in enumerate(
                worksheet.iter_rows(max_col=MAX_EXCEL_COLUMNS)
            ):
                if row_index >= MAX_EXCEL_ROWS:
                    break

                for column_index, cell in enumerate(row):
                    if column_index >= MAX_EXCEL_COLUMNS:
                        break

                    if getattr(cell, "data_type", "") == "f":
                        coordinates.add((row_index, column_index))

            positions[worksheet.title] = coordinates

        return positions
    except Exception:
        return {}
    finally:
        workbook.close()


def _read_xlsx_sheet(
    worksheet,
    formulas: set[tuple[int, int]],
    cell_budget: int,
) -> tuple[list[tuple[int, list[str]]], int, list[str]]:
    """Read one read-only worksheet into row-number/text pairs."""

    rows: list[tuple[int, list[str]]] = []
    notes: list[str] = []
    used = 0

    if (worksheet.max_column or 0) > MAX_EXCEL_COLUMNS:
        _add_note(
            notes,
            f"[worksheet truncated after {MAX_EXCEL_COLUMNS} columns]",
        )

    for row_index, row in enumerate(
        worksheet.iter_rows(max_col=MAX_EXCEL_COLUMNS, values_only=True)
    ):
        if row_index >= MAX_EXCEL_ROWS:
            _add_note(
                notes,
                f"[worksheet truncated after {MAX_EXCEL_ROWS} rows]",
            )
            break

        if used >= cell_budget:
            _add_note(
                notes,
                "[worksheet truncated: the "
                f"{MAX_EXCEL_CELLS}-cell workbook limit was reached]",
            )
            break

        cells: list[str] = []

        for column_index, value in enumerate(row):
            text, shortened = _format_excel_value(value)

            if shortened:
                _add_note(
                    notes,
                    "[cell text truncated at "
                    f"{MAX_EXCEL_CELL_CHARS} characters]",
                )

            if not text and (row_index, column_index) in formulas:
                text = FORMULA_WITHOUT_VALUE

            cells.append(text)

        # Trailing blanks only pad the row; interior blanks keep columns
        # aligned so the header row stays meaningful.
        while cells and not cells[-1]:
            cells.pop()

        # Only populated cells count against the budget, otherwise the
        # padding openpyxl adds per row would deplete it.
        used += len(cells)

        rows.append((row_index + 1, cells))

    return rows, used, notes


def _extract_xlsx(content: bytes, filename: str) -> ExtractedText:
    """Extract every worksheet of an .xlsx package as labelled rows."""

    try:
        workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            # Cached values only: openpyxl never calculates a formula.
            data_only=True,
            keep_links=False,
        )
    except Exception as error:
        raise DocumentError(
            f"{filename} could not be read as an .xlsx workbook. The file "
            "may be damaged or password-protected.",
            422,
            "unreadable_document",
        ) from error

    try:
        formulas = _xlsx_formula_cells(content)
        worksheets = list(workbook.worksheets)
        sheets: list[_ExcelSheet] = []
        builder = _TextBuilder()
        budget = MAX_EXCEL_CELLS
        omitted = 0

        for worksheet in worksheets:
            if len(sheets) >= MAX_WORKSHEETS:
                omitted = len(worksheets) - len(sheets)
                break

            rows, used, notes = _read_xlsx_sheet(
                worksheet,
                formulas.get(worksheet.title, set()),
                budget,
            )
            budget -= used

            sheets.append(
                _ExcelSheet(
                    name=worksheet.title,
                    hidden=(
                        getattr(worksheet, "sheet_state", "visible")
                        != "visible"
                    ),
                    rows=tuple(rows),
                    notes=tuple(notes),
                )
            )

            if budget <= 0:
                omitted = len(worksheets) - len(sheets)
                break
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(
            f"{filename} contains a worksheet that could not be parsed.",
            422,
            "unreadable_document",
        ) from error
    finally:
        workbook.close()

    return _render_workbook(sheets, omitted, filename, builder)


def _format_xls_cell(
    worksheet,
    row_index: int,
    column_index: int,
    datemode: int,
) -> tuple[str, bool]:
    """Return one legacy cell as text, using the cached result only."""

    cell_type = worksheet.cell_type(row_index, column_index)
    value = worksheet.cell_value(row_index, column_index)

    if cell_type in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return "", False

    if cell_type == xlrd.XL_CELL_ERROR:
        error_text = xlrd.error_text_from_code.get(value, "#ERROR")

        return f"[error {error_text}]", False

    if cell_type == xlrd.XL_CELL_BOOLEAN:
        return ("TRUE" if value else "FALSE"), False

    if cell_type == xlrd.XL_CELL_DATE:
        try:
            value = xlrd.xldate_as_datetime(value, datemode)
        except Exception:
            return _format_excel_value(value)

    return _format_excel_value(value)


def _read_xls_sheet(
    worksheet,
    datemode: int,
    cell_budget: int,
) -> tuple[list[tuple[int, list[str]]], int, list[str]]:
    """Read one xlrd worksheet into row-number/text pairs."""

    rows: list[tuple[int, list[str]]] = []
    notes: list[str] = []
    used = 0

    row_count = min(worksheet.nrows, MAX_EXCEL_ROWS)
    column_count = min(worksheet.ncols, MAX_EXCEL_COLUMNS)

    if worksheet.nrows > MAX_EXCEL_ROWS:
        _add_note(
            notes,
            f"[worksheet truncated after {MAX_EXCEL_ROWS} rows]",
        )

    if worksheet.ncols > MAX_EXCEL_COLUMNS:
        _add_note(
            notes,
            f"[worksheet truncated after {MAX_EXCEL_COLUMNS} columns]",
        )

    for row_index in range(row_count):
        if used >= cell_budget:
            _add_note(
                notes,
                "[worksheet truncated: the "
                f"{MAX_EXCEL_CELLS}-cell workbook limit was reached]",
            )
            break

        cells: list[str] = []

        for column_index in range(column_count):
            text, shortened = _format_xls_cell(
                worksheet,
                row_index,
                column_index,
                datemode,
            )

            if shortened:
                _add_note(
                    notes,
                    "[cell text truncated at "
                    f"{MAX_EXCEL_CELL_CHARS} characters]",
                )

            cells.append(text)

        while cells and not cells[-1]:
            cells.pop()

        # Only populated cells count against the budget, so trailing
        # blanks reported by the reader cannot deplete it.
        used += len(cells)

        rows.append((row_index + 1, cells))

    return rows, used, notes


def _extract_xls(content: bytes, filename: str) -> ExtractedText:
    """Extract every worksheet of a legacy .xls workbook with xlrd."""

    try:
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    except xlrd.XLRDError as error:
        if "encrypt" in str(error).lower():
            raise DocumentError(
                f"{filename} is password-protected. Remove the password "
                "before uploading it.",
                422,
                "encrypted_document",
            ) from error

        raise DocumentError(
            f"{filename} could not be read as a legacy .xls workbook. "
            "The file may be damaged or use an unsupported Excel format.",
            422,
            "unreadable_document",
        ) from error
    except Exception as error:
        raise DocumentError(
            f"{filename} could not be read as a legacy .xls workbook.",
            422,
            "unreadable_document",
        ) from error

    try:
        names = workbook.sheet_names()
        sheets: list[_ExcelSheet] = []
        builder = _TextBuilder()
        budget = MAX_EXCEL_CELLS
        omitted = 0

        for index in range(len(names)):
            if len(sheets) >= MAX_WORKSHEETS:
                omitted = len(names) - len(sheets)
                break

            worksheet = workbook.sheet_by_index(index)
            rows, used, notes = _read_xls_sheet(
                worksheet,
                workbook.datemode,
                budget,
            )
            budget -= used

            sheets.append(
                _ExcelSheet(
                    name=worksheet.name,
                    hidden=getattr(worksheet, "visibility", 0) != 0,
                    rows=tuple(rows),
                    notes=tuple(notes),
                )
            )

            if budget <= 0:
                omitted = len(names) - len(sheets)
                break
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(
            f"{filename} contains a worksheet that could not be parsed.",
            422,
            "unreadable_document",
        ) from error
    finally:
        workbook.release_resources()

    return _render_workbook(sheets, omitted, filename, builder)


def extract_text(
    extension: str,
    content: bytes,
    filename: str,
) -> ExtractedText:
    """Dispatch to the extractor for one validated file."""

    if extension == ".pdf":
        return ExtractedText(_extract_pdf(content, filename))

    if extension == ".docx":
        return ExtractedText(_extract_docx(content, filename))

    if extension == ".xlsx":
        return _extract_xlsx(content, filename)

    if extension == ".xls":
        return _extract_xls(content, filename)

    if extension == ".csv":
        return ExtractedText(_extract_csv(content, filename))

    return ExtractedText(_decode_text(content, filename))


def extract_document(upload: UploadedFile) -> ExtractedDocument:
    """Validate one upload and return its extracted text."""

    filename = (upload.filename or "attachment").strip()
    extension = file_extension(filename)

    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(ALLOWED_EXTENSIONS)

        raise DocumentError(
            f"{filename} has an unsupported file type. Attach one of: "
            f"{allowed}.",
            415,
            "unsupported_type",
        )

    declared = normalize_content_type(upload.content_type)
    allowed_types = ALLOWED_CONTENT_TYPES[extension]

    if (
        declared not in GENERIC_CONTENT_TYPES
        and declared not in allowed_types
    ):
        raise DocumentError(
            f"{filename} was sent as '{declared}', which does not match "
            f"the {extension} file type.",
            415,
            "content_type_mismatch",
        )

    size = len(upload.content)

    if size == 0:
        raise DocumentError(
            f"{filename} is empty. Attach a document that contains "
            "text.",
            422,
            "empty_document",
        )

    if size > MAX_FILE_BYTES:
        raise DocumentError(
            f"{filename} is larger than the "
            f"{MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
            413,
            "file_too_large",
        )

    _check_signature(extension, upload.content, filename)

    try:
        extracted = extract_text(extension, upload.content, filename)
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(
            f"{filename} could not be processed. The file may be "
            "damaged or unsupported.",
            422,
            "extraction_failed",
        ) from error

    text = _clean_text(extracted.text)

    if not text:
        raise DocumentError(
            f"{filename} contains no extractable text. Scanned or "
            "image-only documents are not supported.",
            422,
            "empty_document",
        )

    truncated = extracted.truncated or len(text) > MAX_EXTRACTED_CHARS

    if truncated:
        text = text[:MAX_EXTRACTED_CHARS].rstrip()

    return ExtractedDocument(
        filename=filename,
        extension=extension,
        text=text,
        characters=len(text),
        truncated=truncated,
    )


def extract_documents(
    uploads: Sequence[UploadedFile],
) -> list[ExtractedDocument]:
    """Validate and extract every upload, up to the file-count limit."""

    if not uploads:
        raise DocumentError(
            "Attach at least one document, or use the question-only "
            "endpoint.",
            400,
            "no_files",
        )

    if len(uploads) > MAX_FILES:
        raise DocumentError(
            f"Attach at most {MAX_FILES} documents per question.",
            400,
            "too_many_files",
        )

    return [extract_document(upload) for upload in uploads]
