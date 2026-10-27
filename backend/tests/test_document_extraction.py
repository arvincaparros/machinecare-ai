"""Unit tests for in-memory document validation and extraction.

No network, database or ChromaDB access happens here: the extractor is
a pure function over bytes.
"""

import io

import pytest
from pypdf import PdfWriter

from app.services.document_extraction import (
    ALLOWED_EXTENSIONS,
    MAX_EXTRACTED_CHARS,
    MAX_FILES,
    MAX_FILE_BYTES,
    DocumentError,
    ExtractedDocument,
    UploadedFile,
    extract_document,
    extract_documents,
)
from tests.documents import (
    build_docx,
    build_pdf,
    build_zip_without_document,
)


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


# ---------------------------------------------------------------------
# Valid extraction
# ---------------------------------------------------------------------


def test_txt_is_decoded_as_plain_text():
    document = extract_document(
        make_upload("report.txt", b"Camera drift caused the stop.")
    )

    assert isinstance(document, ExtractedDocument)
    assert document.extension == ".txt"
    assert document.text == "Camera drift caused the stop."
    assert document.characters == len("Camera drift caused the stop.")
    assert document.truncated is False


def test_txt_handles_utf8_bom_and_windows_1252():
    with_bom = extract_document(
        make_upload("bom.txt", b"\xef\xbb\xbfCamera drift")
    )
    western = extract_document(
        make_upload("accent.txt", "Température élevée".encode("cp1252"))
    )

    assert with_bom.text == "Camera drift"
    assert western.text == "Température élevée"


def test_txt_normalizes_line_endings_and_blank_line_runs():
    document = extract_document(
        make_upload(
            "lines.txt",
            b"Line one\r\nLine two\r\n\r\n\r\n\r\nLine three",
        )
    )

    assert document.text == "Line one\nLine two\n\nLine three"


def test_control_characters_are_replaced_without_rejecting():
    document = extract_document(
        make_upload("controls.txt", b"Photo eye\x07 fault\x1f cleared")
    )

    assert "\x07" not in document.text
    assert "\x1f" not in document.text
    assert "Photo eye" in document.text


def test_csv_rows_are_flattened_to_readable_lines():
    content = (
        b"machine,incident,cause\n"
        b"AOI-01,DT-2026-004,Camera drift\n"
        b"PRESS-02,DT-2026-009,Hydraulic leak\n"
    )

    document = extract_document(
        make_upload("incidents.csv", content, "text/csv")
    )

    lines = document.text.splitlines()

    assert lines[0] == "machine | incident | cause"
    assert lines[1] == "AOI-01 | DT-2026-004 | Camera drift"
    assert lines[2] == "PRESS-02 | DT-2026-009 | Hydraulic leak"


def test_csv_quoted_cells_survive_parsing():
    content = b'machine,note\nAOI-01,"Camera drift, then stop"\n'

    document = extract_document(make_upload("quoted.csv", content))

    assert "Camera drift, then stop" in document.text


def test_pdf_text_layer_is_extracted():
    content = build_pdf("Camera drift detected on AOI-01")

    document = extract_document(
        make_upload("report.pdf", content, "application/pdf")
    )

    assert "Camera drift detected on AOI-01" in document.text
    assert document.characters > 0


def test_docx_paragraphs_are_extracted_in_order():
    content = build_docx(
        ["Downtime report", "Root cause: camera drift"]
    )

    document = extract_document(
        make_upload(
            "report.docx",
            content,
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
        )
    )

    assert document.text.splitlines() == [
        "Downtime report",
        "Root cause: camera drift",
    ]


def test_long_documents_are_truncated_to_the_extraction_limit():
    content = b"a" * (MAX_EXTRACTED_CHARS + 5_000)

    document = extract_document(make_upload("long.txt", content))

    assert document.truncated is True
    assert document.characters == MAX_EXTRACTED_CHARS
    assert len(document.text) == MAX_EXTRACTED_CHARS


# ---------------------------------------------------------------------
# Rejected formats
# ---------------------------------------------------------------------


def test_unsupported_extension_is_rejected_before_reading():
    error = extract_error(make_upload("payload.exe", b"MZ binary"))

    assert error.status_code == 415
    assert error.code == "unsupported_type"
    assert ".pdf" in error.message


def test_extension_list_matches_the_documented_formats():
    assert ALLOWED_EXTENSIONS == (
        ".pdf",
        ".txt",
        ".csv",
        ".docx",
        ".xlsx",
        ".xls",
    )


def test_declared_content_type_must_match_the_extension():
    error = extract_error(
        make_upload("report.pdf", build_pdf("text"), "image/png")
    )

    assert error.status_code == 415
    assert error.code == "content_type_mismatch"


def test_generic_octet_stream_is_accepted_when_bytes_are_valid():
    document = extract_document(
        make_upload(
            "report.txt",
            b"Camera drift",
            "application/octet-stream",
        )
    )

    assert document.text == "Camera drift"


def test_pdf_without_a_pdf_header_is_rejected():
    error = extract_error(
        make_upload(
            "fake.pdf",
            b"This is plain text pretending to be a PDF.",
            "application/pdf",
        )
    )

    assert error.status_code == 415
    assert error.code == "invalid_signature"


def test_docx_that_is_not_a_zip_is_rejected():
    error = extract_error(
        make_upload(
            "fake.docx",
            b"not a zip archive at all",
            "application/zip",
        )
    )

    assert error.status_code == 415
    assert error.code == "invalid_signature"


def test_zip_without_word_document_is_rejected():
    error = extract_error(
        make_upload(
            "archive.docx",
            build_zip_without_document(),
            "application/zip",
        )
    )

    assert error.status_code == 415
    assert error.code == "invalid_signature"


def test_macro_enabled_docx_is_rejected():
    content = build_docx(
        ["Report"],
        extra_entries={"word/vbaProject.bin": b"macro bytes"},
    )

    error = extract_error(
        make_upload("macro.docx", content, "application/zip")
    )

    assert error.status_code == 415
    assert error.code == "macro_enabled"


def test_docx_with_a_doctype_declaration_is_rejected():
    document_xml = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE w:document [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )

    error = extract_error(
        make_upload(
            "xxe.docx",
            build_docx([], document_xml=document_xml),
            "application/zip",
        )
    )

    assert error.status_code == 422
    assert error.code == "unsafe_document"


def test_docx_with_malformed_xml_is_rejected():
    error = extract_error(
        make_upload(
            "broken.docx",
            build_docx([], document_xml="<w:document><w:body>"),
            "application/zip",
        )
    )

    assert error.status_code == 422
    assert error.code == "unreadable_document"


def test_password_protected_pdf_is_rejected():
    writer = PdfWriter()
    writer.append(io.BytesIO(build_pdf("Camera drift")))
    writer.encrypt("secret")

    buffer = io.BytesIO()
    writer.write(buffer)

    error = extract_error(
        make_upload(
            "locked.pdf",
            buffer.getvalue(),
            "application/pdf",
        )
    )

    assert error.status_code == 422
    assert error.code == "encrypted_document"


def test_corrupted_pdf_is_rejected_without_raising():
    error = extract_error(
        make_upload(
            "corrupt.pdf",
            b"%PDF-1.4\n1 0 obj << /Type /Catalog",
            "application/pdf",
        )
    )

    assert error.status_code == 422
    assert error.code in {
        "unreadable_document",
        "empty_document",
        "extraction_failed",
    }


# ---------------------------------------------------------------------
# Size, emptiness and count limits
# ---------------------------------------------------------------------


def test_empty_file_is_rejected():
    error = extract_error(make_upload("empty.txt", b""))

    assert error.status_code == 422
    assert error.code == "empty_document"


def test_whitespace_only_document_is_rejected():
    error = extract_error(make_upload("blank.txt", b"   \n\n \t "))

    assert error.status_code == 422
    assert error.code == "empty_document"


def test_binary_content_in_a_text_file_is_rejected():
    error = extract_error(
        make_upload("binary.txt", b"\x00\x01\x02binary payload")
    )

    assert error.status_code == 415
    assert error.code == "binary_content"


def test_file_over_ten_megabytes_is_rejected():
    content = b"a" * (MAX_FILE_BYTES + 1)

    error = extract_error(make_upload("huge.txt", content))

    assert error.status_code == 413
    assert error.code == "file_too_large"


def test_file_at_exactly_the_limit_is_accepted():
    content = b"a" * MAX_FILE_BYTES

    document = extract_document(make_upload("limit.txt", content))

    assert document.characters == MAX_EXTRACTED_CHARS


def test_more_than_three_documents_are_rejected():
    uploads = [
        make_upload(f"note-{index}.txt", b"Camera drift")
        for index in range(MAX_FILES + 1)
    ]

    with pytest.raises(DocumentError) as excinfo:
        extract_documents(uploads)

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "too_many_files"


def test_no_documents_is_rejected():
    with pytest.raises(DocumentError) as excinfo:
        extract_documents([])

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "no_files"


def test_three_documents_are_all_extracted():
    uploads = [
        make_upload("one.txt", b"First note"),
        make_upload("two.csv", b"machine,cause\nAOI-01,Camera drift"),
        make_upload("three.txt", b"Third note"),
    ]

    documents = extract_documents(uploads)

    assert [document.filename for document in documents] == [
        "one.txt",
        "two.csv",
        "three.txt",
    ]


def test_uppercase_extension_is_normalized():
    document = extract_document(
        make_upload("REPORT.TXT", b"Camera drift")
    )

    assert document.extension == ".txt"
    assert document.text == "Camera drift"
