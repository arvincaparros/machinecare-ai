
import pytest

from app.services.rag_service import (
    INCIDENT_ID_PATTERN,
    MAX_DISTANCE,
    ask_machinecare,
    build_prompt,
    find_unavailable_document_references,
    validate_citations,
)


QUESTION = "AOI camera inspection is inaccurate"

# Evidence shaped like the records the service sends to the LLM.
VERIFIED_CONTEXT = [
    {
        "incident_id": "DT-2026-001",
        "cause": "Camera calibration failed",
    },
    {
        "incident_id": "DT-2026-025",
        "cause": "Camera drift",
    },
]


def test_closed_incident_produces_grounded_answer(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    mock_search.return_value = [
        make_match("DT-TEST-001", distance=0.21)
    ]
    mock_groq.return_value = (
        "DT-TEST-001 recorded a camera drift root cause."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert result["sources"] == [{
        "incident_id": "DT-TEST-001",
        "machine": "AOI-01",
        "distance": 0.21,
    }]
    assert result["cited_incident_ids"] == ["DT-TEST-001"]
    assert result["unsupported_citations"] == []

    prompt = mock_groq.call_args.args[0]

    assert "DT-TEST-001" in prompt
    assert "Camera calibration failed" in prompt
    assert "Camera drift after extended operation" in prompt
    assert "Recalibrated camera and verified inspection" in prompt
    assert "Weekly calibration schedule" in prompt
    assert "AOI-01" in prompt


def test_prompt_contains_grounding_rules():
    context = [{
        "incident_id": "DT-TEST-001",
        "machine": "AOI-01",
        "production_line": "Line 1",
        "date": "2026-07-01",
        "cause": "Camera calibration failed",
        "root_cause": "Camera drift after extended operation",
        "action_taken": "Recalibrated camera",
        "preventive_action": "Weekly calibration schedule",
        "status": "Closed",
    }]

    prompt = build_prompt(QUESTION, context)

    # Historical findings versus current hypotheses.
    assert "POSSIBLE CAUSE" in prompt
    assert "Historical findings are not current diagnoses" in prompt
    assert "Do not attribute a finding" in prompt

    # Exact actions and outcomes.
    assert "Describe corrective actions exactly as recorded" in prompt
    assert "Do not claim a repair succeeded" in prompt

    # No invented procedures.
    assert "maintenance manuals" in prompt
    assert "SOPs" in prompt
    assert "calibration" in prompt
    assert "thresholds" in prompt
    assert "machine-specific repair procedures" in prompt

    # Recommendations versus documented facts.
    assert "RECOMMENDATIONS" in prompt
    assert "label them as recommendations" in prompt

    # Insufficient evidence and untrusted data.
    assert "records are insufficient to answer" in prompt
    assert "untrusted data" in prompt

    # Only the supplied PostgreSQL evidence is present.
    assert "DT-TEST-001" in prompt
    assert "Camera drift after extended operation" in prompt


def test_fabricated_incident_ids_are_not_verified_sources(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    mock_search.return_value = [
        make_match("DT-TEST-001")
    ]
    mock_groq.return_value = (
        "DT-TEST-001 and DT-2026-999 show the same issue."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    source_ids = {
        source["incident_id"]
        for source in result["sources"]
    }

    assert source_ids == {"DT-TEST-001"}
    assert "DT-2026-999" not in source_ids
    assert result["unsupported_citations"] == ["DT-2026-999"]

    # The fabricated ID may only appear inside the unverified note.
    assert "not verified" in result["answer"]
    assert "DT-2026-999" in result["answer"]


def test_sources_match_postgres_records_used_as_evidence(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    add_incident(
        "DT-TEST-002",
        cause="Camera image drift",
        root_cause="Loose camera mount",
        action_taken="Tightened camera mount",
        preventive_action="Mount inspection",
    )

    mock_search.return_value = [
        make_match("DT-TEST-001", distance=0.10),
        make_match("DT-TEST-002", distance=0.30),
    ]
    mock_groq.return_value = (
        "DT-TEST-001 and DT-TEST-002 were both recorded."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    source_ids = [
        source["incident_id"]
        for source in result["sources"]
    ]

    assert source_ids == ["DT-TEST-001", "DT-TEST-002"]
    assert result["cited_incident_ids"] == [
        "DT-TEST-001",
        "DT-TEST-002",
    ]
    assert result["unsupported_citations"] == []

    prompt = mock_groq.call_args.args[0]

    for source in result["sources"]:
        assert source["incident_id"] in prompt

    assert "Loose camera mount" in prompt
    assert "Tightened camera mount" in prompt


def test_no_relevant_evidence_returns_fallback_without_groq(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    # Distance is above MAX_DISTANCE.
    mock_search.return_value = [
        make_match("DT-TEST-001", distance=0.90)
    ]

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    mock_groq.assert_not_called()

    assert result["sources"] == []
    assert result["cited_incident_ids"] == []
    assert result["unsupported_citations"] == []
    assert "couldn't find" in result["answer"]


def test_stale_chroma_document_is_not_used_as_evidence(
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

    # ChromaDB still holds an outdated copy of the resolution.
    mock_search.return_value = [
        make_match(
            "DT-TEST-001",
            document=(
                "Root Cause: STALE CHROMA ROOT CAUSE\n"
                "Action Taken: STALE CHROMA ACTION"
            ),
        )
    ]
    mock_groq.return_value = "DT-TEST-001 answer."

    ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    prompt = mock_groq.call_args.args[0]

    assert "STALE CHROMA ROOT CAUSE" not in prompt
    assert "STALE CHROMA ACTION" not in prompt
    assert "Camera drift after extended operation" in prompt


def test_evidence_just_above_threshold_returns_fallback_without_groq(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    mock_search.return_value = [
        make_match(
            "DT-TEST-001",
            distance=MAX_DISTANCE + 0.0007,
        )
    ]

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    mock_groq.assert_not_called()

    assert result["sources"] == []
    assert result["cited_incident_ids"] == []
    assert result["unsupported_citations"] == []
    assert "couldn't find" in result["answer"]


def test_evidence_at_threshold_is_accepted(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    mock_search.return_value = [
        make_match("DT-TEST-001", distance=MAX_DISTANCE)
    ]
    mock_groq.return_value = "DT-TEST-001 answer."

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    mock_groq.assert_called_once()

    assert [
        source["incident_id"]
        for source in result["sources"]
    ] == ["DT-TEST-001"]


def test_threshold_filter_preserves_machine_scope_and_citations(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001", machine_code="AOI-01")
    add_incident("DT-TEST-002", machine_code="Pick & Place-03")
    add_incident("DT-TEST-003", machine_code="AOI-01")

    mock_search.return_value = [
        make_match("DT-TEST-002", distance=0.20),
        make_match("DT-TEST-001", distance=0.30),
        make_match(
            "DT-TEST-003",
            distance=MAX_DISTANCE + 0.0007,
        ),
    ]
    mock_groq.return_value = (
        "DT-TEST-001 and DT-2026-999 were recorded."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    # Wrong-machine and above-threshold matches never become sources.
    assert [
        source["incident_id"]
        for source in result["sources"]
    ] == ["DT-TEST-001"]
    assert result["cited_incident_ids"] == ["DT-TEST-001"]
    assert result["unsupported_citations"] == ["DT-2026-999"]

    prompt = mock_groq.call_args.args[0]

    assert "DT-TEST-001" in prompt
    assert "DT-TEST-002" not in prompt
    assert "DT-TEST-003" not in prompt


@pytest.mark.parametrize(
    "answer",
    [
        "DT-2026-001 and DT-2026-025.",
        "**DT-2026-001** and **DT-2026-025**",
        "- **DT-2026-001** camera drift\n- **DT-2026-025** calibration",
        "`DT-2026-001`, `DT-2026-025`",
        "[DT-2026-001]: calibration\n[DT-2026-025]: drift",
        "| DT-2026-001 | Camera drift |\n| DT-2026-025 | Calibration |",
        "See DT-2026-001. Also DT-2026-025:",
        "[DT-2026-001](http://example.invalid) and "
        "[DT-2026-025](http://example.invalid)",
    ],
)
def test_markdown_and_punctuation_do_not_hide_valid_citations(answer):
    result = validate_citations(answer, VERIFIED_CONTEXT)

    assert result["cited_incident_ids"] == [
        "DT-2026-001",
        "DT-2026-025",
    ]
    assert result["unsupported_citations"] == []


@pytest.mark.parametrize(
    "answer",
    [
        "DT\u20112026\u2011001 was recalibrated.",  # non-breaking hyphen
        "DT\u20132026\u2013001 was recalibrated.",  # en dash
        "DT\u20142026\u2013001 was recalibrated.",  # em dash
        "DT\u22122026\u2212001 was recalibrated.",  # minus sign
        "DT-\u200b2026-001 was recalibrated.",  # zero-width space
        "DT - 2026 - 001 was recalibrated.",  # spaced separators
    ],
)
def test_typographic_separators_do_not_hide_citations(answer):
    result = validate_citations(answer, VERIFIED_CONTEXT)

    assert result["cited_incident_ids"] == ["DT-2026-001"]
    assert result["unsupported_citations"] == []


@pytest.mark.parametrize(
    "answer",
    [
        "DT-2026-999 failed the same way.",
        "DT\u20112026\u2011999 failed the same way.",
        "DT\u20132026\u2013999 failed the same way.",
        "DT - 2026 - 999 failed the same way.",
    ],
)
def test_unverified_ids_are_reported_whatever_the_separator(answer):
    result = validate_citations(answer, VERIFIED_CONTEXT)

    assert result["cited_incident_ids"] == []
    assert result["unsupported_citations"] == ["DT-2026-999"]


def test_ascii_only_pattern_would_have_dropped_the_live_citations():
    answer = "**DT\u20112026\u2011001** and **DT\u20112026\u2011025**"

    # The previous extractor was this ASCII pattern alone, so it found
    # nothing and both citation lists stayed empty.
    assert INCIDENT_ID_PATTERN.findall(answer) == []

    result = validate_citations(answer, VERIFIED_CONTEXT)

    assert result["cited_incident_ids"] == [
        "DT-2026-001",
        "DT-2026-025",
    ]


def test_swagger_response_symptom_is_reproduced_and_fixed(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    """The live answer cited both records but returned an empty list."""

    add_incident("DT-2026-001")
    add_incident("DT-2026-025")

    mock_search.return_value = [
        make_match("DT-2026-001", distance=0.20),
        make_match("DT-2026-025", distance=0.30),
    ]
    mock_groq.return_value = (
        "### Similar Historical Incidents\n"
        "- **DT\u20112026\u2011001** camera calibration failed.\n"
        "- **DT\u20112026\u2011025** camera drift after extended "
        "operation.\n\n"
        "### Sources\n"
        "DT\u20112026\u2011001, DT\u20112026\u2011025\n"
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert result["cited_incident_ids"] == [
        "DT-2026-001",
        "DT-2026-025",
    ]
    assert result["unsupported_citations"] == []

    # The public response shape is unchanged.
    assert set(result) == {
        "answer",
        "sources",
        "cited_incident_ids",
        "unsupported_citations",
    }


def test_unindexed_sop_reference_is_disclosed(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-TEST-001")

    mock_search.return_value = [
        make_match("DT-TEST-001", distance=0.20)
    ]
    mock_groq.return_value = (
        "DT-TEST-001 was recalibrated. Follow the approved "
        "AOI-01 SOP before the next inspection."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    # Sources stay derived from the PostgreSQL record only.
    assert result["sources"] == [{
        "incident_id": "DT-TEST-001",
        "machine": "AOI-01",
        "distance": 0.2,
    }]

    assert result["answer"].startswith(
        "DT-TEST-001 was recalibrated."
    )
    assert "were not verified" in result["answer"]
    assert "sop" in result["answer"].lower()


def test_document_mentioned_by_evidence_is_not_disclosed(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident(
        "DT-TEST-001",
        action_taken="Recalibrated camera per SOP-114",
    )

    mock_search.return_value = [
        make_match("DT-TEST-001", distance=0.20)
    ]
    mock_groq.return_value = (
        "DT-TEST-001 was recalibrated per SOP-114."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert "were not verified" not in result["answer"]


def test_document_reference_detector_ignores_supported_terms():
    supported = [{
        "incident_id": "DT-TEST-001",
        "action_taken": "Recalibrated per SOP-114",
    }]
    unsupported = [{
        "incident_id": "DT-TEST-001",
        "cause": "Camera calibration failed",
    }]

    assert find_unavailable_document_references(
        "DT-TEST-001 was recalibrated per SOP-114.",
        supported,
    ) == []

    assert find_unavailable_document_references(
        "Follow the approved AOI-01 SOP and the service manual.",
        unsupported,
    ) == ["sop", "manual"]


def test_prompt_forbids_presenting_unavailable_documents():
    prompt = build_prompt(
        QUESTION,
        [{"incident_id": "DT-TEST-001"}],
    )

    assert "No manuals, SOPs," in prompt
    assert "work instructions, or procedures are available" in prompt
    assert "never state or imply that one exists" in prompt
    assert "never present one as a" in prompt

    # The previous instruction invited the model to name an approved SOP.
    assert "Direct the technician to approved SOPs" not in prompt

    assert "### Sources (incident IDs copied exactly" in prompt


# The answer observed in production: correct citations, but it infers a
# missed preventive action and prescribes a full recalibration.
AOI_MOCKED_ANSWER = (
    "### Similar Historical Incidents\n"
    "DT-2026-001 and DT-2026-025 both recorded camera calibration "
    "failures on AOI-01.\n\n"
    "### Historical Root Causes\n"
    "DT-2026-001 recorded camera drift after extended operation; "
    "DT-2026-025 recorded camera drift.\n\n"
    "### Previously Recorded Corrective Actions\n"
    "The camera was recalibrated in both incidents.\n\n"
    "### Possible Causes for the Current Problem "
    "(hypotheses, not documented facts)\n"
    "The weekly calibration may have been missed "
    "(POSSIBLE CAUSE).\n\n"
    "### Recommended Next Steps "
    "(recommendations, not documented facts)\n"
    "Perform a full recalibration before the next production run.\n\n"
    "### Sources\n"
    "DT-2026-001, DT-2026-025\n"
)


def test_prompt_separates_hypotheses_from_documented_facts():
    prompt = build_prompt(QUESTION, VERIFIED_CONTEXT)

    assert "a HYPOTHESIS, never a conclusion" in prompt
    assert "never imply that the current problem is the same fault" in prompt
    assert "historical sections contain documented facts only" in prompt
    assert "Possible Causes section contains hypotheses" in prompt
    assert "Recommended Next Steps section contains suggestions" in prompt


def test_prompt_forbids_inferring_a_missed_preventive_action():
    prompt = build_prompt(QUESTION, VERIFIED_CONTEXT)

    assert "A preventive action in a record is a past plan or" in prompt
    assert "It is not evidence that the work was done" in prompt
    assert "calibration, inspection, or maintenance was missed" in prompt
    assert "or skipped unless the record says so" in prompt


def test_prompt_forbids_mandatory_actions_from_history():
    prompt = build_prompt(QUESTION, VERIFIED_CONTEXT)

    assert "never present it as a required, standard, or" in prompt
    assert "Never state that a repair, verification, recalibration," in prompt
    assert "required, necessary, or" in prompt
    assert "Offer it at most as a cautious suggestion that" in prompt


def test_prompt_prefers_records_or_personnel_over_invented_procedure():
    prompt = build_prompt(QUESTION, VERIFIED_CONTEXT)

    assert "records do not establish a specific corrective" in prompt
    assert "inspecting the relevant incident records or consulting" in prompt
    assert "qualified maintenance personnel" in prompt
    assert "Do not manufacture a" in prompt


def test_aoi_overreach_answer_keeps_authoritative_grounding(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    """The production AOI answer: citations valid, reasoning overreached."""

    add_incident(
        "DT-2026-001",
        cause="Camera calibration failed",
        root_cause="Camera drift after extended operation",
        action_taken="Recalibrated camera and verified inspection",
        preventive_action="Weekly calibration schedule",
    )
    add_incident(
        "DT-2026-025",
        cause="Camera calibration failed",
        root_cause="Camera drift",
        action_taken="Recalibrated camera",
        preventive_action="Weekly calibration",
    )

    mock_search.return_value = [
        make_match("DT-2026-001", distance=0.45),
        make_match("DT-2026-025", distance=0.48),
    ]
    mock_groq.return_value = AOI_MOCKED_ANSWER

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    prompt = mock_groq.call_args.args[0]

    # The model receives the recorded facts plus the rules that keep a
    # preventive action and a past fix from becoming current claims.
    assert "Recalibrated camera and verified inspection" in prompt
    assert "Weekly calibration schedule" in prompt
    assert "a HYPOTHESIS, never a conclusion" in prompt
    assert "A preventive action in a record is a past plan or" in prompt
    assert (
        "Never state that a repair, verification, recalibration,"
        in prompt
    )

    # Sources remain authoritative and citations stay consistent.
    assert result["sources"] == [
        {
            "incident_id": "DT-2026-001",
            "machine": "AOI-01",
            "distance": 0.45,
        },
        {
            "incident_id": "DT-2026-025",
            "machine": "AOI-01",
            "distance": 0.48,
        },
    ]
    assert result["cited_incident_ids"] == [
        "DT-2026-001",
        "DT-2026-025",
    ]
    assert result["unsupported_citations"] == []

    # The service does not rewrite hypothesis wording; that constraint is
    # enforced by the prompt alone, which keeps the response predictable.
    assert result["answer"] == AOI_MOCKED_ANSWER
    mock_groq.assert_called_once()


def test_aoi_answer_still_applies_citation_and_document_guards(
    db_session_factory,
    add_incident,
    mock_search,
    mock_groq,
    make_match,
):
    add_incident("DT-2026-001")

    mock_search.return_value = [
        make_match("DT-2026-001", distance=0.30)
    ]
    mock_groq.return_value = (
        "DT-2026-001 was recalibrated. DT-2026-999 shows the same "
        "fault. Follow the AOI-01 SOP before recalibrating."
    )

    result = ask_machinecare(
        question=QUESTION,
        machine="AOI-01",
        session_factory=db_session_factory,
    )

    assert result["cited_incident_ids"] == ["DT-2026-001"]
    assert result["unsupported_citations"] == ["DT-2026-999"]
    assert [
        source["incident_id"]
        for source in result["sources"]
    ] == ["DT-2026-001"]

    assert "were not verified" in result["answer"]
    assert "not available in the retrieved evidence" in result["answer"]
