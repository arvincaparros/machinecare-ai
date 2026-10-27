"""API tests for the MachineCare RAG endpoints.

The RAG service is replaced through a FastAPI dependency override, so no
test here calls Groq, connects to PostgreSQL or touches ChromaDB.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.main import app, get_ask_service
from app.api.schemas import MAX_QUESTION_LENGTH


GROUNDED_RESPONSE = {
    "answer": "### Similar Historical Incidents\nDT-2026-001 camera drift.",
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

FALLBACK_RESPONSE = {
    "answer": (
        "I couldn't find a sufficiently relevant resolved downtime "
        "incident in the database."
    ),
    "sources": [],
    "cited_incident_ids": [],
    "unsupported_citations": [],
}


@pytest.fixture
def client():
    """Provide a test client and always discard overrides afterwards."""

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def service(client):
    """Install a recording fake RAG service.

    Calling the returned installer registers the fake; every invocation
    is recorded in ``install.calls``.
    """

    calls = []

    def install(return_value=None, error=None):
        def fake(**kwargs):
            calls.append(kwargs)

            if error is not None:
                raise error

            return return_value

        app.dependency_overrides[get_ask_service] = lambda: fake

    install.calls = calls

    return install


def test_health_reports_process_running(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"].startswith("application/json")


def test_health_does_not_call_the_rag_service(client, service):
    service(GROUNDED_RESPONSE)

    client.get("/health")

    assert service.calls == []


def test_valid_question_returns_the_service_response(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "Why did the AOI camera drift?"},
    )

    assert response.status_code == 200
    assert response.json() == GROUNDED_RESPONSE


def test_machine_filter_is_passed_through(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={
            "question": "Why did the AOI camera drift?",
            "machine": "AOI-01",
        },
    )

    assert response.status_code == 200
    assert service.calls == [
        {
            "question": "Why did the AOI camera drift?",
            "machine": "AOI-01",
        }
    ]


def test_omitted_machine_is_passed_as_none(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "Why did the AOI camera drift?"},
    )

    assert response.status_code == 200
    assert service.calls == [
        {
            "question": "Why did the AOI camera drift?",
            "machine": None,
        }
    ]


def test_blank_machine_means_no_filter(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "Why did the AOI camera drift?", "machine": "   "},
    )

    assert response.status_code == 200
    assert service.calls[0]["machine"] is None


def test_surrounding_whitespace_is_trimmed(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "  Why did the AOI camera drift?  "},
    )

    assert response.status_code == 200
    assert service.calls[0]["question"] == "Why did the AOI camera drift?"


@pytest.mark.parametrize("question", ["", "   ", "\t\n "])
def test_blank_question_is_rejected(client, service, question):
    service(GROUNDED_RESPONSE)

    response = client.post("/api/v1/ask", json={"question": question})

    assert response.status_code == 422
    assert service.calls == []


def test_missing_question_is_rejected(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post("/api/v1/ask", json={"machine": "AOI-01"})

    assert response.status_code == 422
    assert service.calls == []


def test_overlong_question_is_rejected(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "a" * (MAX_QUESTION_LENGTH + 1)},
    )

    assert response.status_code == 422
    assert service.calls == []


def test_question_at_maximum_length_is_accepted(client, service):
    service(GROUNDED_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "a" * MAX_QUESTION_LENGTH},
    )

    assert response.status_code == 200


def test_fallback_answer_with_empty_sources_is_returned(client, service):
    service(FALLBACK_RESPONSE)

    response = client.post(
        "/api/v1/ask",
        json={"question": "Does the forklift battery overheat?"},
    )

    assert response.status_code == 200
    assert response.json() == FALLBACK_RESPONSE
    assert response.json()["sources"] == []


def test_citation_metadata_is_preserved(client, service):
    payload = {
        "answer": (
            "DT-2026-001 drifted. DT-2026-999 was also cited."
        ),
        "sources": [
            {
                "incident_id": "DT-2026-001",
                "machine": "AOI-01",
                "distance": 0.4999,
            }
        ],
        "cited_incident_ids": ["DT-2026-001"],
        "unsupported_citations": ["DT-2026-999"],
    }
    service(payload)

    response = client.post(
        "/api/v1/ask",
        json={"question": "Which camera incidents were verified?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cited_incident_ids"] == ["DT-2026-001"]
    assert body["unsupported_citations"] == ["DT-2026-999"]
    assert body["sources"] == payload["sources"]


def test_service_error_does_not_leak_internals(client, service):
    service(
        error=RuntimeError(
            "connection failed: postgresql://user:hunter2@db:5432/prod"
        )
    )

    response = client.post(
        "/api/v1/ask",
        json={"question": "Why did the AOI camera drift?"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to answer the question."}

    text = response.text
    assert "hunter2" not in text
    assert "postgresql" not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text


def test_malformed_service_response_returns_generic_error(client, service):
    service({"answer": "Answer without the rest of the contract."})

    response = client.post(
        "/api/v1/ask",
        json={"question": "Why did the AOI camera drift?"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to answer the question."}
    assert "sources" not in response.text
