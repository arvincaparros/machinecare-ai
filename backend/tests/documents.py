"""Builders for valid and deliberately broken test documents.

Everything here is generated in memory so the tests never depend on
binary fixtures checked into the repository.
"""

import datetime
import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape

import xlwt
from openpyxl import Workbook


def build_pdf(text: str) -> bytes:
    """Build a one-page PDF whose text layer contains ``text``."""

    stream = (
        f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    ).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        ),
        (
            b"<< /Type /Font /Subtype /Type1 "
            b"/BaseFont /Helvetica >>"
        ),
    ]

    document = bytearray(b"%PDF-1.4\n")
    offsets = []

    for index, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document += f"{index} 0 obj\n".encode("ascii")
        document += body
        document += b"\nendobj\n"

    xref_offset = len(document)
    document += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    document += b"0000000000 65535 f \n"

    for offset in offsets:
        document += f"{offset:010d} 00000 n \n".encode("ascii")

    document += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    return bytes(document)


def build_docx(
    paragraphs: list[str],
    document_xml: str | None = None,
    extra_entries: dict[str, bytes] | None = None,
) -> bytes:
    """Build a minimal DOCX archive from plain paragraph text."""

    if document_xml is None:
        body = "".join(
            f"<w:p><w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>"
            for paragraph in paragraphs
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body>"
            "</w:document>"
        )

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/'
                'package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                "</Types>"
            ),
        )
        archive.writestr("word/document.xml", document_xml)

        for name, data in (extra_entries or {}).items():
            archive.writestr(name, data)

    return buffer.getvalue()


def build_zip_without_document() -> bytes:
    """Build a valid ZIP archive that is not a Word document."""

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "not a word document")

    return buffer.getvalue()


def rebuild_zip(
    content: bytes,
    replacements: dict[str, bytes] | None = None,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    """Rewrite entries of an OOXML package without touching the rest."""

    with zipfile.ZipFile(io.BytesIO(content)) as source:
        entries = {
            info.filename: source.read(info.filename)
            for info in source.infolist()
        }

    entries.update(replacements or {})
    entries.update(extra or {})

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in entries.items():
            target.writestr(name, data)

    return buffer.getvalue()


def _cache_cell(xml: str, reference: str, cached: str) -> str:
    """Insert a cached result next to the formula of one cell."""

    pattern = re.compile(
        rf'(<c r="{re.escape(reference)}"[^>]*>)(.*?)</c>',
        re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        opening, body = match.group(1), match.group(2)

        if "<f>" not in body or "<v>" in body:
            return match.group(0)

        body = re.sub(r"<v\s*/>", f"<v>{cached}</v>", body)

        if "<v>" not in body:
            body += f"<v>{cached}</v>"

        return f"{opening}{body}</c>"

    return pattern.sub(replace, xml)


def build_xlsx(
    sheets: list[tuple[str, list[list[object]]]],
    hidden: Sequence[str] = (),
    cached_formulas: dict[str, str] | None = None,
) -> bytes:
    """Build an .xlsx workbook from named worksheets of cell values.

    ``cached_formulas`` maps a cell reference (``"D2"``) to the result a
    spreadsheet application would have stored for the formula in that
    cell; openpyxl itself never writes cached values.
    """

    workbook = Workbook()
    workbook.remove(workbook.active)

    for name, rows in sheets:
        worksheet = workbook.create_sheet(name)

        if name in hidden:
            worksheet.sheet_state = "hidden"

        for row in rows:
            worksheet.append(list(row))

    buffer = io.BytesIO()
    workbook.save(buffer)
    content = buffer.getvalue()

    if not cached_formulas:
        return content

    entries = {}

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            data = archive.read(info.filename)

            if info.filename.startswith("xl/worksheets/sheet"):
                xml = data.decode("utf-8")

                for reference, cached in cached_formulas.items():
                    xml = _cache_cell(xml, reference, cached)

                data = xml.encode("utf-8")

            entries[info.filename] = data

    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in entries.items():
            target.writestr(name, data)

    return buffer.getvalue()


@dataclass(frozen=True)
class ExcelError:
    """An error value a spreadsheet application would have cached."""

    code: int


def build_xls(
    sheets: list[tuple[str, list[list[object]]]],
    hidden: Sequence[str] = (),
) -> bytes:
    """Build a legacy .xls workbook with xlwt (test-only dependency).

    Datetime values get a real date format so xlrd reports them as dates
    instead of raw serial numbers.
    """

    workbook = xlwt.Workbook()
    date_style = xlwt.easyxf(num_format_str="YYYY-MM-DD HH:MM:SS")

    for index, (name, rows) in enumerate(sheets):
        worksheet = workbook.add_sheet(name)

        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                if value is None:
                    continue

                if isinstance(value, datetime.datetime):
                    worksheet.write(
                        row_index,
                        column_index,
                        value,
                        date_style,
                    )
                    continue

                if isinstance(value, ExcelError):
                    worksheet.row(row_index).set_cell_error(
                        column_index,
                        value.code,
                    )
                    continue

                worksheet.write(row_index, column_index, value)

        if name in hidden:
            workbook.get_sheet(index).visibility = 1

    buffer = io.BytesIO()
    workbook.save(buffer)

    return buffer.getvalue()
