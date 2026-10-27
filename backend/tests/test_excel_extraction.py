"""Unit tests for Excel workbook validation and extraction.

The workbooks are generated in memory (openpyxl for .xlsx, xlwt for .xls),
so no binary fixture is stored in the repository. No network, database or
ChromaDB access happens here: the extractor is a pure function over bytes.
"""

import datetime

import pytest
import xlrd

from app.services import document_extraction
from app.services.document_extraction import (
    FORMULA_WITHOUT_VALUE,
    MAX_EXCEL_CELL_CHARS,
    MAX_EXCEL_COLUMNS,
    MAX_EXCEL_ROWS,
    MAX_EXTRACTED_CHARS,
    MAX_WORKSHEETS,
    DocumentError,
    UploadedFile,
    extract_document,
)
from tests.documents import (
    ExcelError,
    build_xls,
    build_xlsx,
    build_zip_without_document,
    rebuild_zip,
)

XLSX_TYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
XLS_TYPES = "application/vnd.ms-excel"


def make_upload(
    filename: str,
    content: bytes,
    content_type: str = "",
) -> UploadedFile:
    """Build an in-memory upload the way the API layer does."""

    return UploadedFile(
        filename=filename,
        content_type=content_type,
        content=content,
    )


def extract_error(upload: UploadedFile) -> DocumentError:
    """Run extraction and return the documented rejection."""

    with pytest.raises(DocumentError) as excinfo:
        extract_document(upload)

    return excinfo.value


def extract_xlsx(content: bytes) -> str:
    """Extract a generated .xlsx workbook and return its text."""

    return extract_document(
        make_upload("incidents.xlsx", content, XLSX_TYPES)
    ).text


def extract_xls(content: bytes) -> str:
    """Extract a generated .xls workbook and return its text."""

    return extract_document(make_upload("incidents.xls", content, XLS_TYPES)).text


# ---------------------------------------------------------------------
# Valid .xlsx extraction
# ---------------------------------------------------------------------


def test_xlsx_multiple_worksheets_keep_names_and_rows():
    content = build_xlsx(
        [
            (
                "Incidents",
                [
                    ["machine", "incident"],
                    ["AOI-01", "DT-2026-004"],
                ],
            ),
            (
                "Actions",
                [
                    ["owner", "action"],
                    ["Maintenance", "Recalibrate camera"],
                ],
            ),
        ]
    )

    lines = extract_xlsx(content).splitlines()

    assert "### Worksheet: Incidents" in lines
    assert "### Worksheet: Actions" in lines
    assert "Row 1: machine | incident" in lines
    assert "Row 2: AOI-01 | DT-2026-004" in lines
    assert "Row 2: Maintenance | Recalibrate camera" in lines
    assert lines.index("### Worksheet: Incidents") < lines.index(
        "### Worksheet: Actions"
    )


def test_xlsx_header_and_incident_values_stay_understandable():
    content = build_xlsx(
        [
            (
                "Incidents",
                [
                    ["machine", "incident", "cause", "minutes"],
                    ["AOI-01", "DT-2026-004", "Camera drift", 45],
                ],
            )
        ]
    )

    document = extract_document(
        make_upload("incidents.xlsx", content, XLSX_TYPES)
    )

    assert document.extension == ".xlsx"
    assert document.truncated is False
    assert "Row 1: machine | incident | cause | minutes" in document.text
    assert "Row 2: AOI-01 | DT-2026-004 | Camera drift | 45" in document.text


def test_xlsx_numbers_and_booleans_are_rendered_readably():
    content = build_xlsx(
        [("Data", [["count", "ratio", "active"], [25.0, 0.1, False]])]
    )

    lines = extract_xlsx(content).splitlines()

    assert "Row 2: 25 | 0.1 | FALSE" in lines


def test_xlsx_dates_are_rendered_as_iso_text():
    content = build_xlsx(
        [
            (
                "Data",
                [
                    ["started", "closed"],
                    [
                        datetime.datetime(2026, 7, 1, 8, 30),
                        datetime.date(2026, 7, 2),
                    ],
                ],
            )
        ]
    )

    lines = extract_xlsx(content).splitlines()

    assert "Row 2: 2026-07-01 08:30:00 | 2026-07-02 00:00:00" in lines


def test_hidden_worksheet_is_included_and_labelled():
    content = build_xlsx(
        [
            ("Visible", [["public note"]]),
            ("Internal", [["internal cost 1200"]]),
        ],
        hidden=["Internal"],
    )

    lines = extract_xlsx(content).splitlines()

    assert "### Worksheet: Internal [hidden]" in lines
    assert "### Worksheet: Visible" in lines
    assert "Row 1: internal cost 1200" in lines


def test_empty_worksheet_is_marked_without_failing():
    content = build_xlsx([("Data", [["value"]]), ("Empty", [])])

    lines = extract_xlsx(content).splitlines()

    assert "### Worksheet: Empty" in lines
    assert "(empty worksheet)" in lines
    assert "Row 1: value" in lines


def test_blank_rows_are_collapsed_but_row_numbers_are_kept():
    content = build_xlsx(
        [
            (
                "Data",
                [
                    ["header"],
                    ["first"],
                    [],
                    ["third"],
                ],
            )
        ]
    )

    lines = extract_xlsx(content).splitlines()

    assert "Row 1: header" in lines
    assert "Row 2: first" in lines
    assert "[1 blank row(s)]" in lines
    assert "Row 4: third" in lines


def test_formula_without_a_cached_value_is_labelled():
    content = build_xlsx(
        [("Data", [["count", "double"], [3, "=A2*2"]])]
    )

    lines = extract_xlsx(content).splitlines()

    assert f"Row 2: 3 | {FORMULA_WITHOUT_VALUE}" in lines
    # The formula itself is never shown as if it were a value.
    assert "=A2*2" not in "\n".join(lines)


def test_formula_with_a_cached_value_uses_the_stored_result():
    content = build_xlsx(
        [("Data", [["count", "double"], [3, "=A2*2"]])],
        cached_formulas={"B2": "6"},
    )

    document = extract_document(
        make_upload("incidents.xlsx", content, XLSX_TYPES)
    )

    assert "Row 2: 3 | 6" in document.text.splitlines()
    assert FORMULA_WITHOUT_VALUE not in document.text


# ---------------------------------------------------------------------
# Excel limits
# ---------------------------------------------------------------------


def test_xlsx_row_limit_is_reported():
    rows = [[f"row {index}"] for index in range(MAX_EXCEL_ROWS + 5)]

    document = extract_document(
        make_upload("long.xlsx", build_xlsx([("Data", rows)]), XLSX_TYPES)
    )

    lines = document.text.splitlines()

    assert document.truncated is True
    assert f"[worksheet truncated after {MAX_EXCEL_ROWS} rows]" in lines
    assert f"Row {MAX_EXCEL_ROWS}: row {MAX_EXCEL_ROWS - 1}" in lines
    assert f"Row {MAX_EXCEL_ROWS + 1}:" not in document.text


def test_xlsx_column_limit_is_reported():
    rows = [list(range(MAX_EXCEL_COLUMNS + 5))]

    document = extract_document(
        make_upload("wide.xlsx", build_xlsx([("Data", rows)]), XLSX_TYPES)
    )

    lines = document.text.splitlines()

    assert document.truncated is True
    assert f"[worksheet truncated after {MAX_EXCEL_COLUMNS} columns]" in lines
    assert lines[1] == (
        "Row 1: " + " | ".join(str(value) for value in range(MAX_EXCEL_COLUMNS))
    )


def test_xlsx_worksheet_limit_is_reported():
    sheets = [
        (f"Sheet{index}", [["value"]])
        for index in range(MAX_WORKSHEETS + 2)
    ]

    document = extract_document(
        make_upload("many.xlsx", build_xlsx(sheets), XLSX_TYPES)
    )

    assert document.truncated is True
    assert f"### Worksheet: Sheet{MAX_WORKSHEETS - 1}" in document.text
    assert f"### Worksheet: Sheet{MAX_WORKSHEETS}" not in document.text
    assert "2 worksheet(s) omitted" in document.text


def test_workbook_cell_budget_is_reported(monkeypatch):
    monkeypatch.setattr(document_extraction, "MAX_EXCEL_CELLS", 4)

    content = build_xlsx([("Data", [["a", "b"], ["c", "d"], ["e", "f"]])])

    document = extract_document(
        make_upload("dense.xlsx", content, XLSX_TYPES)
    )

    assert document.truncated is True
    assert (
        "[worksheet truncated: the 4-cell workbook limit was reached]"
        in document.text
    )
    assert "Row 3" not in document.text


def test_long_cell_text_is_cut_and_reported():
    content = build_xlsx([("Data", [["note"], ["x" * 900]])])

    document = extract_document(
        make_upload("cells.xlsx", content, XLSX_TYPES)
    )

    assert document.truncated is True
    assert (
        f"[cell text truncated at {MAX_EXCEL_CELL_CHARS} characters]"
        in document.text
    )
    assert "x" * (MAX_EXCEL_CELL_CHARS + 1) not in document.text


def test_workbook_text_limit_is_reported():
    rows = [["y" * MAX_EXCEL_CELL_CHARS] for _ in range(60)]

    document = extract_document(
        make_upload("huge.xlsx", build_xlsx([("Data", rows)]), XLSX_TYPES)
    )

    assert document.truncated is True
    assert document.characters <= MAX_EXTRACTED_CHARS
    assert "extracted text limit reached" in document.text


# ---------------------------------------------------------------------
# Rejected .xlsx workbooks
# ---------------------------------------------------------------------


def test_xlsx_that_is_not_a_zip_is_rejected():
    error = extract_error(
        make_upload("fake.xlsx", b"not a workbook", XLSX_TYPES)
    )

    assert error.status_code == 415
    assert error.code == "invalid_signature"


def test_zip_without_an_excel_workbook_is_rejected():
    error = extract_error(
        make_upload("archive.xlsx", build_zip_without_document(), "application/zip")
    )

    assert error.status_code == 415
    assert error.code == "invalid_signature"


def test_macro_enabled_xlsx_is_rejected():
    content = rebuild_zip(
        build_xlsx([("Data", [["value"]])]),
        extra={"xl/vbaProject.bin": b"macro bytes"},
    )

    error = extract_error(make_upload("macro.xlsx", content, XLSX_TYPES))

    assert error.status_code == 415
    assert error.code == "macro_enabled"


def test_xlsx_with_malformed_sheet_xml_is_rejected():
    content = rebuild_zip(
        build_xlsx([("Data", [["value"]])]),
        replacements={"xl/worksheets/sheet1.xml": b"<worksheet><sheetData><row"},
    )

    error = extract_error(make_upload("broken.xlsx", content, XLSX_TYPES))

    assert error.status_code == 422
    assert error.code == "unreadable_document"


def test_xlsx_declared_as_plain_text_is_rejected():
    error = extract_error(
        make_upload(
            "sheet.xlsx",
            build_xlsx([("Data", [["value"]])]),
            "text/plain",
        )
    )

    assert error.status_code == 415
    assert error.code == "content_type_mismatch"


def test_xlsx_with_malformed_workbook_xml_is_rejected():
    content = rebuild_zip(
        build_xlsx([("Data", [["value"]])]),
        replacements={"xl/workbook.xml": b"<workbook><sheets>"},
    )

    error = extract_error(make_upload("broken.xlsx", content, XLSX_TYPES))

    assert error.status_code == 422
    assert error.code == "unreadable_document"


def test_compound_file_named_xlsx_is_reported_as_encrypted():
    content = document_extraction.OLE2_SIGNATURE + b"\x00" * 600

    error = extract_error(make_upload("locked.xlsx", content, XLSX_TYPES))

    assert error.status_code == 422
    assert error.code == "encrypted_document"
    assert "encrypted" in error.message


def test_xlsx_archive_expansion_is_limited(monkeypatch):
    monkeypatch.setattr(
        document_extraction,
        "MAX_XLSX_UNCOMPRESSED_BYTES",
        1_024,
    )

    error = extract_error(
        make_upload(
            "bomb.xlsx",
            build_xlsx([("Data", [["value"]])]),
            XLSX_TYPES,
        )
    )

    assert error.status_code == 413
    assert error.code == "archive_too_large"


def test_xlsx_archive_entry_count_is_limited(monkeypatch):
    monkeypatch.setattr(document_extraction, "MAX_XLSX_ENTRIES", 3)

    error = extract_error(
        make_upload(
            "entries.xlsx",
            build_xlsx([("Data", [["value"]])]),
            XLSX_TYPES,
        )
    )

    assert error.status_code == 413
    assert error.code == "archive_too_large"


def test_workbook_without_any_cell_value_is_rejected():
    error = extract_error(
        make_upload("empty.xlsx", build_xlsx([("Data", [])]), XLSX_TYPES)
    )

    assert error.status_code == 422
    assert error.code == "empty_document"


# ---------------------------------------------------------------------
# Valid legacy .xls extraction
# ---------------------------------------------------------------------


def test_xls_multiple_worksheets_keep_names_and_rows():
    content = build_xls(
        [
            (
                "Incidents",
                [
                    ["machine", "incident", "cause"],
                    ["AOI-01", "DT-2026-004", "Camera drift"],
                ],
            ),
            (
                "Actions",
                [
                    ["owner", "action"],
                    ["Maintenance", "Recalibrate camera"],
                ],
            ),
        ]
    )

    document = extract_document(make_upload("incidents.xls", content, XLS_TYPES))
    lines = document.text.splitlines()

    assert document.extension == ".xls"
    assert document.truncated is False
    assert "### Worksheet: Incidents" in lines
    assert "### Worksheet: Actions" in lines
    assert "Row 1: machine | incident | cause" in lines
    assert "Row 2: AOI-01 | DT-2026-004 | Camera drift" in lines


def test_xls_dates_booleans_and_errors_are_rendered():
    content = build_xls(
        [
            (
                "Incidents",
                [
                    ["started", "active", "minutes", "delta"],
                    [
                        datetime.datetime(2026, 7, 1, 8, 30),
                        True,
                        12.5,
                        ExcelError(0x07),
                    ],
                ],
            )
        ]
    )

    lines = extract_xls(content).splitlines()

    assert (
        "Row 1: started | active | minutes | delta" in lines
    )
    assert (
        "Row 2: 2026-07-01 08:30:00 | TRUE | 12.5 | [error #DIV/0!]" in lines
    )


def test_xls_hidden_worksheet_is_labelled():
    content = build_xls(
        [("Visible", [["public note"]]), ("Internal", [["internal cost"]])],
        hidden=["Internal"],
    )

    lines = extract_xls(content).splitlines()

    assert "### Worksheet: Internal [hidden]" in lines
    assert "Row 1: internal cost" in lines


def test_xls_alternate_content_type_is_accepted():
    content = build_xls([("Data", [["value"]])])

    document = extract_document(
        make_upload("legacy.xls", content, "application/msexcel")
    )

    assert "Row 1: value" in document.text


def test_xls_row_limit_is_reported():
    rows = [[f"row {index}"] for index in range(MAX_EXCEL_ROWS + 5)]

    document = extract_document(
        make_upload("long.xls", build_xls([("Data", rows)]), XLS_TYPES)
    )

    assert document.truncated is True
    assert f"[worksheet truncated after {MAX_EXCEL_ROWS} rows]" in document.text
    assert f"Row {MAX_EXCEL_ROWS + 1}:" not in document.text


def test_xls_column_limit_is_reported():
    rows = [list(range(MAX_EXCEL_COLUMNS + 5))]

    document = extract_document(
        make_upload("wide.xls", build_xls([("Data", rows)]), XLS_TYPES)
    )

    assert document.truncated is True
    assert (
        f"[worksheet truncated after {MAX_EXCEL_COLUMNS} columns]"
        in document.text
    )


# ---------------------------------------------------------------------
# Rejected legacy .xls workbooks
# ---------------------------------------------------------------------


def test_xlsx_bytes_named_xls_are_rejected():
    error = extract_error(
        make_upload(
            "renamed.xls",
            build_xlsx([("Data", [["value"]])]),
            XLS_TYPES,
        )
    )

    assert error.status_code == 415
    assert error.code == "invalid_signature"
    assert ".xlsx" in error.message


def test_corrupt_compound_file_is_rejected():
    content = document_extraction.OLE2_SIGNATURE + b"\x00" * 600

    error = extract_error(make_upload("corrupt.xls", content, XLS_TYPES))

    assert error.status_code == 422
    assert error.code == "unreadable_document"


def test_encrypted_xls_is_reported(monkeypatch):
    def raise_encrypted(*args, **kwargs):
        raise xlrd.XLRDError("Workbook is encrypted")

    monkeypatch.setattr(document_extraction.xlrd, "open_workbook", raise_encrypted)

    error = extract_error(
        make_upload(
            "locked.xls",
            build_xls([("Data", [["value"]])]),
            XLS_TYPES,
        )
    )

    assert error.status_code == 422
    assert error.code == "encrypted_document"


def test_xls_without_any_cell_value_is_rejected():
    error = extract_error(
        make_upload("empty.xls", build_xls([("Data", [])]), XLS_TYPES)
    )

    assert error.status_code == 422
    assert error.code == "empty_document"
