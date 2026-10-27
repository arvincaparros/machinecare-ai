"""API tests for ``POST /api/v1/ask-with-files``.

The document-aware RAG service is replaced through a FastAPI dependency
override, so nothing here calls Groq, PostgreSQL or ChromaDB. The
question-only endpoint is exercised as a regression check.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.main import (
    app,
    get_ask_service,
    get_document_ask_service,
)
from app.api.schemas import (
    MAX_MACHINE_LENGTH,
    MAX_QUESTION_LENGTH,
)
from app.services.document_extraction import MAX_FILES, MAX_FILE_BYTES
from tests.documents import build_docx, build_pdf, build_xls, build_xlsx


DOCUMENT_RESPONSE = {
    "answer": (
        "### Document Summary\n"
        "The attached report states the camera drifted."
    ),
    "sources": [
        {
            "incident_id": "DT-2026-001",
            "machine": "AOI-01",
            "distance": 0.3125,
        }
    ],
    "cited_incident_ids": ["DT-2026-001"],
    "unsupported_citations": [],
}


@pytest.fixture
def client():
    """Provide a test client and always discard overrides afterwards."""

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def document_service(client):
    """Install a recording fake for the document-aware service."""

    calls = []

    def install(return_value=None, error=None):
        def fake(**kwargs):
            calls.append(kwargs)

            if error is not None:
                raise error

            return return_value

        app.dependency_overrides[get_document_ask_service] = (
            lambda: fake
        )

    install.calls = calls

    return install


@pytest.fixture
def plain_service(client):
    """Install a fake for the question-only service."""

    calls = []

    def install(return_value):
        def fake(**kwargs):
            calls.append(kwargs)
            return return_value

        app.dependency_overrides[get_ask_service] = lambda: fake

    install.calls = calls

    return install


def post_files(client, files, data=None):
    """Post a multipart request to the attachment endpoint."""

    payload = {"question": "What does this report say?"}

    if data is not None:
        payload = data

    return client.post(
        "/api/v1/ask-with-files",
        data=payload,
        files=files,
    )


# ---------------------------------------------------------------------
# Successful document-aware requests
# ---------------------------------------------------------------------


def test_txt_upload_returns_answer_and_document_metadata(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                (
                    "downtime-report.txt",
                    b"Camera drift caused the stop.",
                    "text/plain",
                ),
            )
        ],
    )

    assert response.status_code == 200

    body = response.json()

    assert body["answer"] == DOCUMENT_RESPONSE["answer"]
    assert body["sources"] == DOCUMENT_RESPONSE["sources"]
    assert body["cited_incident_ids"] == ["DT-2026-001"]
    assert body["documents"] == [
        {
            "filename": "downtime-report.txt",
            "characters": len("Camera drift caused the stop."),
            "truncated": False,
        }
    ]


def test_extracted_text_is_passed_to_the_service(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    post_files(
        client,
        [
            (
                "files",
                (
                    "report.txt",
                    b"Camera drift caused the stop.",
                    "text/plain",
                ),
            )
        ],
    )

    assert len(document_service.calls) == 1

    call = document_service.calls[0]

    assert call["question"] == "What does this report say?"
    assert call["machine"] is None
    assert call["documents"] == [
        {
            "filename": "report.txt",
            "text": "Camera drift caused the stop.",
            "characters": len("Camera drift caused the stop."),
            "truncated": False,
        }
    ]


def test_machine_filter_is_passed_through(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                ("report.txt", b"Camera drift.", "text/plain"),
            )
        ],
        data={
            "question": "What does this report say?",
            "machine": "AOI-01",
        },
    )

    assert response.status_code == 200
    assert document_service.calls[0]["machine"] == "AOI-01"


def test_blank_machine_becomes_no_filter(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    post_files(
        client,
        [("files", ("report.txt", b"Camera drift.", "text/plain"))],
        data={"question": "What does this report say?", "machine": "   "},
    )

    assert document_service.calls[0]["machine"] is None


def test_csv_and_docx_are_extracted_together(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                (
                    "rows.csv",
                    b"machine,cause\nAOI-01,Camera drift\n",
                    "text/csv",
                ),
            ),
            (
                "files",
                (
                    "notes.docx",
                    build_docx(["Root cause: camera drift"]),
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document",
                ),
            ),
        ],
    )

    assert response.status_code == 200

    documents = document_service.calls[0]["documents"]

    assert documents[0]["text"].splitlines()[1] == "AOI-01 | Camera drift"
    assert documents[1]["text"] == "Root cause: camera drift"


def test_pdf_upload_is_extracted(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                (
                    "report.pdf",
                    build_pdf("Camera drift detected"),
                    "application/pdf",
                ),
            )
        ],
    )

    assert response.status_code == 200
    assert "Camera drift detected" in (
        document_service.calls[0]["documents"][0]["text"]
    )


XLSX_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
XLS_TYPE = "application/vnd.ms-excel"


def test_xlsx_upload_is_extracted_into_the_rag_payload(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    content = build_xlsx(
        [
            (
                "Incidents",
                [
                    ["machine", "incident", "cause"],
                    ["AOI-01", "DT-2026-004", "Camera drift"],
                ],
            )
        ]
    )

    response = post_files(
        client,
        [("files", ("incidents.xlsx", content, XLSX_TYPE))],
    )

    assert response.status_code == 200

    body = response.json()
    extracted = document_service.calls[0]["documents"][0]

    assert extracted["filename"] == "incidents.xlsx"
    assert extracted["truncated"] is False
    assert "### Worksheet: Incidents" in extracted["text"]
    assert "Row 2: AOI-01 | DT-2026-004 | Camera drift" in extracted["text"]
    assert body["documents"] == [
        {
            "filename": "incidents.xlsx",
            "characters": extracted["characters"],
            "truncated": False,
        }
    ]
    assert body["sources"] == DOCUMENT_RESPONSE["sources"]


def test_xls_upload_is_extracted_into_the_rag_payload(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    content = build_xls(
        [
            (
                "Incidents",
                [
                    ["machine", "incident"],
                    ["PRESS-02", "DT-2026-009"],
                ],
            )
        ]
    )

    response = post_files(
        client,
        [("files", ("incidents.xls", content, XLS_TYPE))],
    )

    assert response.status_code == 200

    extracted = document_service.calls[0]["documents"][0]

    assert "### Worksheet: Incidents" in extracted["text"]
    assert "Row 2: PRESS-02 | DT-2026-009" in extracted["text"]
    assert response.json()["documents"][0]["filename"] == "incidents.xls"


def test_excel_truncation_is_reported_in_document_metadata(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    content = build_xlsx([("Data", [["note"], ["x" * 900]])])

    response = post_files(
        client,
        [("files", ("long.xlsx", content, XLSX_TYPE))],
    )

    assert response.status_code == 200
    assert response.json()["documents"][0]["truncated"] is True


def test_excel_with_the_wrong_content_type_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                (
                    "incidents.xlsx",
                    build_xlsx([("Data", [["value"]])]),
                    "text/plain",
                ),
            )
        ],
    )

    assert response.status_code == 415
    assert document_service.calls == []


def test_three_files_are_accepted(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                (f"note-{index}.txt", b"Camera drift.", "text/plain"),
            )
            for index in range(MAX_FILES)
        ],
    )

    assert response.status_code == 200
    assert len(document_service.calls[0]["documents"]) == MAX_FILES


# ---------------------------------------------------------------------
# Preserved question-only contract
# ---------------------------------------------------------------------


def test_question_only_endpoint_still_returns_the_original_shape(
    client,
    plain_service,
):
    plain_service(DOCUMENT_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={
            "question": "Why did the AOI camera drift?",
            "machine": "AOI-01",
        },
    )

    assert response.status_code == 200

    body = response.json()

    assert body == DOCUMENT_RESPONSE
    assert "documents" not in body
    assert plain_service.calls == [
        {
            "question": "Why did the AOI camera drift?",
            "machine": "AOI-01",
        }
    ]


# ---------------------------------------------------------------------
# Malformed requests
# ---------------------------------------------------------------------


def test_request_without_files_is_rejected(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(client, [])

    assert response.status_code == 400
    assert "/api/v1/ask" in response.json()["detail"]
    assert document_service.calls == []


def test_more_than_three_files_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                (f"note-{index}.txt", b"Camera drift.", "text/plain"),
            )
            for index in range(MAX_FILES + 1)
        ],
    )

    assert response.status_code == 400
    assert str(MAX_FILES) in response.json()["detail"]
    assert document_service.calls == []


def test_blank_question_is_rejected(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("report.txt", b"Camera drift.", "text/plain"))],
        data={"question": "   "},
    )

    assert response.status_code == 422
    assert "blank" in response.json()["detail"]


def test_missing_question_is_rejected(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = client.post(
        "/api/v1/ask-with-files",
        files=[
            ("files", ("report.txt", b"Camera drift.", "text/plain"))
        ],
    )

    assert response.status_code == 422
    assert document_service.calls == []


def test_question_over_the_limit_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("report.txt", b"Camera drift.", "text/plain"))],
        data={"question": "a" * (MAX_QUESTION_LENGTH + 1)},
    )

    assert response.status_code == 422
    assert document_service.calls == []


def test_machine_over_the_limit_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("report.txt", b"Camera drift.", "text/plain"))],
        data={
            "question": "What does this report say?",
            "machine": "M" * (MAX_MACHINE_LENGTH + 1),
        },
    )

    assert response.status_code == 422
    assert document_service.calls == []


def test_json_body_is_not_accepted_by_the_upload_endpoint(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = client.post(
        "/api/v1/ask-with-files",
        json={"question": "What does this report say?"},
    )

    assert response.status_code == 422
    assert document_service.calls == []


# ---------------------------------------------------------------------
# Rejected files
# ---------------------------------------------------------------------


def test_unsupported_extension_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("script.exe", b"MZ binary", "application/x-msdownload"))],
    )

    assert response.status_code == 415
    assert "unsupported file type" in response.json()["detail"]
    assert document_service.calls == []


def test_content_type_mismatch_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("photo.pdf", b"%PDF-1.4 bytes", "image/png"))],
    )

    assert response.status_code == 415
    assert "does not match" in response.json()["detail"]


def test_invalid_pdf_content_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                ("report.pdf", b"plain text, not a PDF", "application/pdf"),
            )
        ],
    )

    assert response.status_code == 415
    assert "PDF header" in response.json()["detail"]
    assert document_service.calls == []


def test_empty_document_is_rejected(client, document_service):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("empty.txt", b"", "text/plain"))],
    )

    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()
    assert document_service.calls == []


def test_whitespace_only_document_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [("files", ("blank.txt", b"  \n\n\t ", "text/plain"))],
    )

    assert response.status_code == 422
    assert "no extractable text" in response.json()["detail"]


def test_file_over_ten_megabytes_is_rejected(
    client,
    document_service,
):
    document_service(DOCUMENT_RESPONSE)

    response = post_files(
        client,
        [
            (
                "files",
                ("huge.txt", b"a" * (MAX_FILE_BYTES + 1), "text/plain"),
            )
        ],
    )

    assert response.status_code == 413
    assert "10 MB" in response.json()["detail"]
    assert document_service.calls == []


# ---------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------


def test_service_failure_returns_a_safe_error(
    client,
    document_service,
):
    document_service(error=RuntimeError("GROQ_API_KEY=secret-value"))

    response = post_files(
        client,
        [
            (
                "files",
                ("report.txt", b"Internal only text.", "text/plain"),
            )
        ],
    )

    assert response.status_code == 500

    detail = response.json()["detail"]

    assert detail == (
        "Failed to answer the question with the attached documents."
    )
    assert "secret-value" not in response.text
    assert "Internal only text." not in response.text
    assert "Traceback" not in response.text


def test_service_failure_does_not_leak_the_exception_message(
    client,
    document_service,
):
    document_service(error=ValueError("boom"))

    response = post_files(
        client,
        [("files", ("report.txt", b"Camera drift.", "text/plain"))],
    )

    assert response.status_code == 500
    assert "boom" not in response.text
