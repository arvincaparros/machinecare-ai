from unittest.mock import MagicMock

import pytest

from app.services.rag_service import MAX_DISTANCE
from retrieval_eval import (
    COLLECTION_NAME,
    RetrievalCase,
    build_eval_collection,
    create_eval_client,
    evaluate,
    filter_by_distance,
    load_cases,
    load_seed_incidents,
    score_case,
    summarize_threshold,
)


REQUIRED_CATEGORIES = {
    "machine-specific",
    "paraphrased-symptom",
    "root-cause",
    "corrective-action",
    "preventive-action",
    "multiple-incidents",
    "no-evidence",
}


def _case(
    expected,
    case_id="unit-case",
    machine=None,
):
    return RetrievalCase(
        case_id=case_id,
        category="unit",
        question="unit question",
        machine=machine,
        expected_incident_ids=tuple(expected),
        evidence="unit",
    )


def test_score_case_perfect_retrieval():
    recall, precision, reciprocal_rank = score_case(
        ["DT-2026-001", "DT-2026-025"],
        ["DT-2026-001", "DT-2026-025"],
    )

    assert recall == 1.0
    assert precision == 1.0
    assert reciprocal_rank == 1.0


def test_score_case_partial_retrieval_and_rank():
    recall, precision, reciprocal_rank = score_case(
        ["DT-2026-001", "DT-2026-025"],
        ["DT-2026-068", "DT-2026-001", "DT-2026-091"],
    )

    assert recall == 0.5
    assert precision == pytest.approx(1 / 3)
    assert reciprocal_rank == 0.5


def test_score_case_no_hits():
    assert score_case(["DT-2026-001"], ["DT-2026-068"]) == (
        0.0,
        0.0,
        0.0,
    )


def test_score_case_empty_expected_set_is_unscored():
    assert score_case([], ["DT-2026-001"]) == (
        None,
        None,
        None,
    )


def test_precision_uses_returned_count_when_fewer_than_five():
    recall, precision, reciprocal_rank = score_case(
        ["DT-2026-001"],
        ["DT-2026-001"],
    )

    assert recall == 1.0
    assert precision == 1.0
    assert reciprocal_rank == 1.0


def test_evaluate_mocks_chromadb_query_contract():
    collection = MagicMock()
    collection.get.return_value = {
        "ids": ["a", "b", "c", "d", "e"]
    }
    collection.query.return_value = {
        "ids": [["DT-2026-001"]],
        "distances": [[0.20]],
        "metadatas": [[{"incident_id": "DT-2026-001"}]],
    }

    results, summary = evaluate(
        collection,
        [_case(["DT-2026-001"], machine="AOI-01")],
    )

    collection.query.assert_called_once_with(
        query_texts=["unit question"],
        n_results=5,
        where={"machine": "AOI-01"},
        include=["metadatas", "distances"],
    )

    assert results[0].recall_at_5 == 1.0
    assert summary.evidence_case_count == 1
    assert summary.no_evidence_case_count == 0


def test_no_evidence_case_detects_leaked_evidence():
    collection = MagicMock()
    collection.count.return_value = 1
    collection.query.return_value = {
        "ids": [["DT-2026-001"]],
        "distances": [[0.40]],
        "metadatas": [[{"incident_id": "DT-2026-001"}]],
    }

    results, summary = evaluate(
        collection,
        [_case([])],
        max_distance=0.50,
    )

    assert results[0].no_evidence_passed is False
    assert summary.no_evidence_pass_rate == 0.0


def test_dataset_matches_seed_data_and_covers_categories():
    incidents = load_seed_incidents()
    by_id = {
        incident["Incident ID"]: incident
        for incident in incidents
    }
    machines = {
        incident["Machine"]
        for incident in incidents
    }
    cases = load_cases()

    assert len(cases) >= 10
    assert len({case.case_id for case in cases}) == len(cases)
    assert {case.category for case in cases} >= REQUIRED_CATEGORIES

    for case in cases:
        assert case.question.strip()

        if case.machine is not None:
            assert case.machine in machines

        for incident_id in case.expected_incident_ids:
            assert incident_id in by_id
            assert by_id[incident_id]["Status"] == "Closed"

        if case.category == "no-evidence":
            assert case.expected_incident_ids == ()
        else:
            assert case.expected_incident_ids

        assert case.expected_incident_ids == tuple(
            dict.fromkeys(case.expected_incident_ids)
        )


@pytest.fixture(scope="module")
def evaluation():
    """Run the full evaluation against an isolated in-memory client."""

    collection = build_eval_collection(
        create_eval_client(),
        load_seed_incidents(),
    )
    cases = load_cases()
    results, summary = evaluate(collection, cases)

    return collection, cases, results, summary


def test_evaluation_uses_dedicated_collection(evaluation):
    collection, cases, results, summary = evaluation

    assert collection.name == COLLECTION_NAME
    assert collection.name != "machine_downtime_incidents"
    assert collection.count() == 100
    assert len(results) == len(cases) == 13


def test_every_case_is_classified_as_evidence_or_no_evidence(
    evaluation,
):
    _, _, results, _ = evaluation

    for result in results:
        if result.case.expected_incident_ids:
            assert result.recall_at_5 is not None
            assert result.precision_at_5 is not None
            assert result.reciprocal_rank is not None
            assert result.no_evidence_passed is None
        else:
            assert result.recall_at_5 is None
            assert result.precision_at_5 is None
            assert result.reciprocal_rank is None
            assert result.no_evidence_passed is not None


def test_retrieval_metric_regression_baseline(evaluation):
    _, _, _, summary = evaluation

    assert summary.evidence_case_count == 10
    assert summary.no_evidence_case_count == 3

    # Coarse regression guards. Actual values are printed by the CLI
    # report; these floors intentionally leave margin below the
    # recorded baseline.
    assert summary.recall_at_5 >= 0.75
    assert summary.precision_at_5 >= 0.50
    assert summary.mrr >= 0.85


def test_machine_scoped_camera_question_retrieves_both_incidents(
    evaluation,
):
    _, _, results, _ = evaluation

    result = next(
        item
        for item in results
        if item.case.case_id == "aoi-camera-calibration"
    )

    top_two = {
        match["incident_id"]
        for match in result.retrieved[:2]
    }

    assert top_two == {"DT-2026-001", "DT-2026-025"}


def test_no_evidence_questions_do_not_surface_verified_evidence(
    evaluation,
):
    _, _, results, summary = evaluation

    no_evidence = [
        result
        for result in results
        if result.no_evidence_passed is not None
    ]

    leaks = [
        {
            "case": result.case.case_id,
            "retrieved": result.retrieved,
        }
        for result in no_evidence
        if not result.no_evidence_passed
    ]

    assert not leaks
    assert summary.no_evidence_pass_rate == 1.0


def test_filter_by_distance_uses_inclusive_threshold():
    matches = [
        {
            "incident_id": "at-threshold",
            "distance": MAX_DISTANCE,
        },
        {
            "incident_id": "just-above",
            "distance": MAX_DISTANCE + 0.0001,
        },
    ]

    assert [
        match["incident_id"]
        for match in filter_by_distance(matches)
    ] == ["at-threshold"]


def test_relevant_evidence_just_above_threshold_is_rejected(
    evaluation,
):
    _, _, results, _ = evaluation

    result = next(
        item
        for item in results
        if item.case.case_id
        == "camera-drift-extended-operation"
    )
    match = next(
        item
        for item in result.retrieved
        if item["incident_id"] == "DT-2026-001"
    )

    assert match["distance"] == pytest.approx(
        0.5007,
        abs=1e-4,
    )
    assert match["distance"] > MAX_DISTANCE
    assert result.rejected_relevant_ids == ["DT-2026-001"]
    assert result.filtered == []
    assert result.filtered_recall_at_5 == 0.0


def test_irrelevant_evidence_just_below_threshold_is_accepted(
    evaluation,
):
    _, _, results, _ = evaluation

    result = next(
        item
        for item in results
        if item.case.case_id
        == "conveyor-belt-slipped-conveyor"
    )
    match = next(
        item
        for item in result.retrieved
        if item["incident_id"] == "DT-2026-051"
    )
    incident = next(
        record
        for record in load_seed_incidents()
        if record["Incident ID"] == "DT-2026-051"
    )

    # The accepted record is a Conveyor incident, but its recorded
    # issue is a temperature excursion, not belt slippage.
    assert incident["Machine"] == "Conveyor"
    assert incident["Cause"] == "Temperature exceeded limit"
    assert incident["Root Cause"] == "Faulty sensor"

    assert match["distance"] == pytest.approx(
        0.4970,
        abs=1e-4,
    )
    assert match["distance"] <= MAX_DISTANCE
    assert "DT-2026-051" not in result.case.expected_incident_ids
    assert "DT-2026-051" in result.accepted_irrelevant_ids


def test_threshold_filtered_evidence_only_contains_passing_records(
    evaluation,
):
    _, _, results, _ = evaluation

    for result in results:
        raw_by_id = {
            match["incident_id"]: match["distance"]
            for match in result.retrieved
        }

        assert {
            match["incident_id"]
            for match in result.filtered
        } == {
            match["incident_id"]
            for match in result.retrieved
            if match["distance"] <= MAX_DISTANCE
        }

        for match in result.filtered:
            assert match["distance"] <= MAX_DISTANCE
            assert (
                raw_by_id[match["incident_id"]]
                == match["distance"]
            )


def test_threshold_sweep_exposes_boundary_effects(evaluation):
    _, _, results, _ = evaluation

    def filtered_ids(case_id, threshold):
        result = next(
            item
            for item in results
            if item.case.case_id == case_id
        )
        return [
            match["incident_id"]
            for match in filter_by_distance(
                result.retrieved,
                threshold,
            )
        ]

    # Relevant evidence just above the configured threshold.
    assert "DT-2026-001" not in filtered_ids(
        "camera-drift-extended-operation",
        0.50,
    )
    assert "DT-2026-001" in filtered_ids(
        "camera-drift-extended-operation",
        0.51,
    )

    # Irrelevant evidence just below the configured threshold.
    assert "DT-2026-051" in filtered_ids(
        "conveyor-belt-slipped-conveyor",
        0.50,
    )
    assert "DT-2026-051" not in filtered_ids(
        "conveyor-belt-slipped-conveyor",
        0.49,
    )


def test_threshold_sweep_is_monotonic(evaluation):
    _, _, results, _ = evaluation

    summaries = [
        summarize_threshold(results, threshold)
        for threshold in (
            0.45,
            0.475,
            0.49,
            0.50,
            0.51,
            0.52,
            0.53,
            0.55,
            0.60,
            0.65,
            0.70,
        )
    ]

    for lower, higher in zip(summaries, summaries[1:]):
        assert (
            higher.retained_relevant_count
            >= lower.retained_relevant_count
        )
        assert (
            higher.accepted_irrelevant_count
            >= lower.accepted_irrelevant_count
        )
        assert (
            higher.no_evidence_leak_count
            >= lower.no_evidence_leak_count
        )
        assert (
            higher.no_evidence_pass_rate
            <= lower.no_evidence_pass_rate
        )

    current = summarize_threshold(results, MAX_DISTANCE)

    assert current.retrieved_relevant_count == 30
    assert current.retained_relevant_count == 22
    assert current.accepted_irrelevant_count == 5
    assert current.no_evidence_leak_count == 0
    assert current.no_evidence_pass_rate == 1.0


def test_no_distance_threshold_separates_all_relevant_from_irrelevant(
    evaluation,
):
    _, _, results, _ = evaluation

    evidence_results = [
        result
        for result in results
        if result.recall_at_5 is not None
    ]

    rejected_relevant_distances = [
        match["distance"]
        for result in evidence_results
        for match in result.retrieved
        if match["incident_id"]
        in result.rejected_relevant_ids
    ]
    accepted_irrelevant_distances = [
        match["distance"]
        for result in evidence_results
        for match in result.filtered
        if match["incident_id"]
        in result.accepted_irrelevant_ids
    ]

    # The distance ranges overlap: no single cutoff can both keep
    # every relevant record and reject every irrelevant record.
    assert min(accepted_irrelevant_distances) < max(
        rejected_relevant_distances
    )
