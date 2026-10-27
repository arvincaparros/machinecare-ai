
from unittest.mock import MagicMock

from sqlalchemy import select

from app.models import DowntimeIncident, DowntimeResolution
from app.services import vector_store_service
from app.services.rag_service import ask_machinecare


QUESTION = "AOI camera inspection is inaccurate"


def test_root_cause_update_is_reflected_without_reindex(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident(
        "DT-TEST-001",
        root_cause="Camera drift after extended operation",
    )

    # The same ChromaDB match is reused for both calls.
    mock_search.return_value = [
        make_match("DT-TEST-001")
    ]
    mock_groq.return_value = "DT-TEST-001 answer."

    ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    first_prompt = mock_groq.call_args.args[0]

    # Update PostgreSQL only. ChromaDB is not reindexed.
    with db_session_factory() as db:
        resolution = db.scalar(
            select(DowntimeResolution)
            .join(DowntimeIncident)
            .where(
                DowntimeIncident.incident_id == "DT-TEST-001"
            )
        )
        resolution.root_cause = "Loose camera mount bolt"
        db.commit()

    ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    second_prompt = mock_groq.call_args.args[0]

    assert "Camera drift after extended operation" in first_prompt
    assert "Loose camera mount bolt" not in first_prompt

    assert "Loose camera mount bolt" in second_prompt
    assert "Camera drift after extended operation" not in second_prompt

    assert mock_search.call_count == 2


def test_non_closed_incident_is_excluded(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001", status="Open")

    mock_search.return_value = [
        make_match("DT-TEST-001")
    ]

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    mock_groq.assert_not_called()

    assert result["sources"] == []
    assert "couldn't find" in result["answer"]


def test_reopened_incident_is_excluded_without_reindex(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001", status="Closed")

    mock_search.return_value = [
        make_match("DT-TEST-001")
    ]
    mock_groq.return_value = "DT-TEST-001 answer."

    first_result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert [
        source["incident_id"]
        for source in first_result["sources"]
    ] == ["DT-TEST-001"]
    assert mock_groq.call_count == 1

    # Reopen the incident in PostgreSQL only.
    with db_session_factory() as db:
        incident = db.scalar(
            select(DowntimeIncident).where(
                DowntimeIncident.incident_id == "DT-TEST-001"
            )
        )
        incident.status = "In Progress"
        db.commit()

    second_result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert second_result["sources"] == []
    assert "couldn't find" in second_result["answer"]

    # No second Groq call was made.
    assert mock_groq.call_count == 1


def test_missing_incident_is_skipped_safely(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    # ChromaDB references an incident no longer in PostgreSQL.
    mock_search.return_value = [
        make_match("DT-2026-999", distance=0.10),
        make_match("DT-TEST-001", distance=0.20),
    ]
    mock_groq.return_value = "DT-TEST-001 answer."

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert [
        source["incident_id"]
        for source in result["sources"]
    ] == ["DT-TEST-001"]

    prompt = mock_groq.call_args.args[0]

    assert "DT-2026-999" not in prompt
    assert "DT-TEST-001" in prompt


def test_missing_resolution_is_skipped_safely(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001", with_resolution=False)

    mock_search.return_value = [
        make_match("DT-TEST-001")
    ]

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    mock_groq.assert_not_called()

    assert result["sources"] == []
    assert "couldn't find" in result["answer"]


def test_machine_filter_is_preserved(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001", machine_code="AOI-01")
    add_incident("DT-TEST-002", machine_code="Pick & Place-03")

    # Even if the index returns another machine, PostgreSQL wins.
    mock_search.return_value = [
        make_match("DT-TEST-001"),
        make_match("DT-TEST-002"),
    ]
    mock_groq.return_value = "DT-TEST-001 answer."

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert [
        source["incident_id"]
        for source in result["sources"]
    ] == ["DT-TEST-001"]

    mock_search.assert_called_once_with(
        question=QUESTION,
        top_k=5,
        machine="AOI-01",
    )

    prompt = mock_groq.call_args.args[0]

    assert "DT-TEST-002" not in prompt


def test_semantic_search_returns_incident_ids(monkeypatch):
    fake_collection = MagicMock()
    fake_collection.get.return_value = {
        "ids": ["DT-TEST-001"]
    }
    fake_collection.query.return_value = {
        "ids": [["DT-TEST-001"]],
        "documents": [["indexed document text"]],
        "metadatas": [[{
            "incident_id": "DT-TEST-001",
            "machine": "AOI-01",
            "status": "Closed",
        }]],
        "distances": [[0.12]],
    }

    monkeypatch.setattr(
        vector_store_service,
        "collection",
        fake_collection,
    )

    matches = vector_store_service.search_similar_incidents(
        question="camera calibration",
        top_k=5,
        machine="AOI-01",
    )

    assert [
        match["incident_id"]
        for match in matches
    ] == ["DT-TEST-001"]
    assert matches[0]["distance"] == 0.12
