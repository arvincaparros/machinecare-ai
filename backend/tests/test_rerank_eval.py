import math

import pytest

from app.services.rag_service import MAX_DISTANCE
from tests.rerank_eval import (
    BASELINE_CONFIG,
    HIGH_RECALL_CONFIG,
    SELECTED_CONFIG,
    Candidate,
    GateConfig,
    LexicalIndex,
    build_lexical_index,
    evaluate_config,
    rescue_config,
    stem,
    tokenize,
)
from tests.retrieval_eval import (
    build_eval_collection,
    create_eval_client,
    load_cases,
    load_seed_incidents,
)


CONVEYOR_CASE = "conveyor-belt-slipped-conveyor"
VERIFIED_CASE = "camera-recalibration-verified"
COMMUNICATION_CASE = "communication-timeout-root-cause"
CAMERA_DRIFT_CASE = "camera-drift-extended-operation"
DRIFT_SYMPTOM_CASE = "aoi-camera-drift-symptom"


def test_stemmer_collapses_inflections():
    assert stem("slipping") == stem("slipped") == "slipp"
    assert stem("verifying") == stem("verified") == "verifi"
    assert stem("timeouts") == stem("timeout") == "timeout"
    assert stem("causes") == stem("cause") == "caus"
    assert stem("failures") == stem("failure") == "failur"


def test_tokenizer_lowercases_splits_and_stopwords():
    assert tokenize(
        "The AOI-01 is emitting a burning smell!"
    ) == ["aoi", "01", "emitt", "burn", "smell"]


def test_lexical_score_full_and_zero_coverage():
    index = LexicalIndex.from_documents(
        documents={
            "DT-A": "alpha beta gamma",
            "DT-B": "delta",
        },
        machines={"DT-A": "M", "DT-B": "M"},
    )

    assert index.score("alpha beta", "DT-A") == 1.0
    assert index.score("alpha beta", "DT-B") == 0.0
    assert index.score("the is of", "DT-A") == 0.0


def test_lexical_score_weights_unmatched_terms_by_idf():
    index = LexicalIndex.from_documents(
        documents={
            "DT-A": "alpha beta",
            "DT-B": "alpha",
        },
        machines={"DT-A": "M", "DT-B": "M"},
    )

    # alpha: df=2 -> log(1 + 2/2); beta: df=1 -> log(1 + 2/1);
    # gamma: df=0 -> max idf log(1 + 2/1).
    total = math.log(2) + math.log(3) + math.log(3)

    assert index.score("alpha beta gamma", "DT-A") == pytest.approx(
        (math.log(2) + math.log(3)) / total,
        abs=1e-9,
    )
    assert index.score("alpha beta gamma", "DT-B") == pytest.approx(
        math.log(2) / total,
        abs=1e-9,
    )


def test_lexical_top_breaks_ties_deterministically():
    index = LexicalIndex.from_documents(
        documents={
            "DT-B": "alpha",
            "DT-A": "alpha",
        },
        machines={"DT-B": "M", "DT-A": "M"},
    )

    first = index.top("alpha", 2)
    second = index.top("alpha", 2)

    assert first == second
    assert [incident_id for incident_id, _ in first] == [
        "DT-A",
        "DT-B",
    ]
    assert first[0][1] == first[1][1]


def test_gate_rules():
    distance_passer = Candidate("DT-X", 0.40, 0.10)

    strict_floor = GateConfig(
        name="unit",
        dense_pool_k=5,
        lexical_pool_k=0,
        lex_floor=0.25,
        lex_rescue=0.55,
        dense_relax=0.70,
        order="hybrid",
    )
    lax_floor = GateConfig(
        name="unit",
        dense_pool_k=5,
        lexical_pool_k=0,
        lex_floor=0.0,
        lex_rescue=0.55,
        dense_relax=0.70,
        order="hybrid",
    )

    assert not strict_floor.accepts(distance_passer)
    assert lax_floor.accepts(distance_passer)

    rescued = Candidate("DT-X", 0.60, 0.90)

    assert lax_floor.accepts(rescued)

    assert not GateConfig(
        name="unit",
        dense_pool_k=5,
        lexical_pool_k=0,
        lex_floor=0.0,
        lex_rescue=0.95,
        dense_relax=0.70,
        order="hybrid",
    ).accepts(rescued)

    assert not GateConfig(
        name="unit",
        dense_pool_k=5,
        lexical_pool_k=0,
        lex_floor=0.0,
        lex_rescue=0.55,
        dense_relax=0.55,
        order="hybrid",
    ).accepts(rescued)

    # Candidates without a dense distance are never accepted.
    assert not lax_floor.accepts(Candidate("DT-X", None, 1.0))

    # No lexical score can rescue beyond the relaxed distance bound.
    assert not lax_floor.accepts(Candidate("DT-X", 0.90, 1.0))


@pytest.fixture(scope="module")
def corpus():
    """Isolated collection and lexical index over the seed incidents."""

    incidents = load_seed_incidents()
    collection = build_eval_collection(
        create_eval_client(),
        incidents,
    )
    index = build_lexical_index(incidents)

    return collection, index


@pytest.fixture(scope="module")
def runs(corpus):
    """Baseline and experimental runs over the 13 evaluation cases."""

    collection, index = corpus
    cases = load_cases()
    runs_by_name = {}

    for config in (
        BASELINE_CONFIG,
        HIGH_RECALL_CONFIG,
        SELECTED_CONFIG,
    ):
        outcomes, summary = evaluate_config(
            collection,
            index,
            cases,
            config,
        )
        runs_by_name[config.name] = (outcomes, summary)

    return cases, runs_by_name


def _outcome(runs, config_name, case_id):
    cases, runs_by_name = runs
    outcomes = runs_by_name[config_name][0]

    return next(
        outcome
        for outcome in outcomes
        if outcome.case.case_id == case_id
    )


def _case(runs, case_id):
    cases, _ = runs

    return next(
        case
        for case in cases
        if case.case_id == case_id
    )


def test_conveyor_false_positive_is_lexically_distinguishable(
    runs,
    corpus,
):
    _, index = corpus
    case = _case(runs, CONVEYOR_CASE)

    false_positive_score = index.score(
        case.question,
        "DT-2026-051",
    )
    belt_score = index.score(
        case.question,
        "DT-2026-006",
    )

    assert false_positive_score == pytest.approx(
        0.2175,
        abs=1e-3,
    )
    assert false_positive_score < belt_score
    assert (
        false_positive_score < SELECTED_CONFIG.lex_floor
    )

    baseline = _outcome(runs, BASELINE_CONFIG.name, CONVEYOR_CASE)
    selected = _outcome(runs, SELECTED_CONFIG.name, CONVEYOR_CASE)

    assert "DT-2026-051" in baseline.accepted_irrelevant_ids
    assert selected.accepted_irrelevant_ids == []
    assert [
        candidate.incident_id
        for candidate in selected.accepted
    ] == ["DT-2026-006", "DT-2026-042", "DT-2026-030"]


def test_camera_verified_question_ranks_true_incident_first(
    runs,
    corpus,
):
    _, index = corpus
    case = _case(runs, VERIFIED_CASE)

    verified_score = index.score(
        case.question,
        "DT-2026-001",
    )
    camera_only_score = index.score(
        case.question,
        "DT-2026-061",
    )

    assert verified_score == pytest.approx(0.5525, abs=1e-3)
    assert camera_only_score == pytest.approx(
        0.2419,
        abs=1e-3,
    )
    assert verified_score > camera_only_score

    baseline = _outcome(runs, BASELINE_CONFIG.name, VERIFIED_CASE)
    selected = _outcome(runs, SELECTED_CONFIG.name, VERIFIED_CASE)

    # Baseline accepts five camera-family records with the true
    # incident ranked second.
    assert len(baseline.accepted) == 5
    assert baseline.precision_at_5 == 0.2
    assert baseline.reciprocal_rank == 0.5
    assert "DT-2026-061" in baseline.accepted_irrelevant_ids

    # The reranked gate keeps only the record that documents the
    # verified inspection.
    assert [
        candidate.incident_id
        for candidate in selected.accepted
    ] == ["DT-2026-001"]
    assert selected.precision_at_5 == 1.0
    assert selected.reciprocal_rank == 1.0
    assert selected.accepted_irrelevant_ids == []


def test_communication_timeout_is_fully_rescued(runs):
    selected = _outcome(
        runs,
        SELECTED_CONFIG.name,
        COMMUNICATION_CASE,
    )

    assert selected.recall_at_5 == 1.0
    assert selected.precision_at_5 == 1.0
    assert len(selected.accepted) == 8

    # Every accepted record sits beyond the production distance gate,
    # so only the lexical rescue path can have accepted it.
    for candidate in selected.accepted:
        assert candidate.distance > MAX_DISTANCE
        assert candidate.lexical_score >= SELECTED_CONFIG.lex_rescue

    baseline = _outcome(
        runs,
        BASELINE_CONFIG.name,
        COMMUNICATION_CASE,
    )

    assert baseline.accepted == []
    assert baseline.recall_at_5 == 0.0


def test_camera_drift_record_is_rescued_above_threshold(runs):
    selected = _outcome(
        runs,
        SELECTED_CONFIG.name,
        CAMERA_DRIFT_CASE,
    )
    baseline = _outcome(
        runs,
        BASELINE_CONFIG.name,
        CAMERA_DRIFT_CASE,
    )

    assert baseline.rejected_relevant_ids == ["DT-2026-001"]
    assert baseline.accepted == []

    assert [
        candidate.incident_id
        for candidate in selected.accepted
    ] == ["DT-2026-001"]
    assert selected.accepted[0].distance == pytest.approx(
        0.5007,
        abs=1e-3,
    )
    assert selected.accepted[0].distance > MAX_DISTANCE
    assert selected.accepted[0].lexical_score == 1.0
    assert selected.recall_at_5 == 1.0


def test_paraphrase_drift_symptom_is_known_regression(runs):
    """Document the trade-off of the selected configuration.

    The paraphrased drift question matches its records almost entirely
    through dense similarity; its lexical scores (0.194 and 0.142) sit
    below the 0.25 floor, so the selected gate rejects records the
    baseline accepts.
    """

    case = _case(runs, DRIFT_SYMPTOM_CASE)
    selected = _outcome(
        runs,
        SELECTED_CONFIG.name,
        DRIFT_SYMPTOM_CASE,
    )
    baseline = _outcome(
        runs,
        BASELINE_CONFIG.name,
        DRIFT_SYMPTOM_CASE,
    )

    assert [
        candidate.incident_id
        for candidate in baseline.accepted
    ] == ["DT-2026-001", "DT-2026-025"]

    assert selected.accepted == []
    assert selected.rejected_relevant_ids == [
        "DT-2026-001",
        "DT-2026-025",
    ]
    assert selected.recall_at_5 == 0.0

    # The rejected records genuinely score below the floor (0.194 and
    # 0.142), so this is a property of the data, not of an unstable
    # boundary.
    assert "DT-2026-001" in selected.rejected_relevant_ids


def test_no_evidence_cases_still_fall_back(runs, corpus):
    _, index = corpus
    cases, runs_by_name = runs

    no_evidence_ids = [
        "no-evidence-forklift",
        "no-evidence-hydraulic-press",
        "no-evidence-aoi-burning-smell",
    ]

    for config_name in (
        BASELINE_CONFIG.name,
        HIGH_RECALL_CONFIG.name,
        SELECTED_CONFIG.name,
    ):
        outcomes = runs_by_name[config_name][0]

        for outcome in outcomes:
            if outcome.case.case_id in no_evidence_ids:
                assert outcome.no_evidence_passed is True
                assert outcome.accepted == []

    summary = runs_by_name[SELECTED_CONFIG.name][1]
    assert summary.no_evidence_leak_count == 0
    assert summary.no_evidence_pass_rate == 1.0

    # The nearest burning-smell distractor scores 0.3017 lexically:
    # above the floor, but far below the rescue bound, and its dense
    # distance stays beyond MAX_DISTANCE.
    burning = next(
        case
        for case in cases
        if case.case_id == "no-evidence-aoi-burning-smell"
    )
    max_lexical = max(
        index.score(burning.question, incident_id)
        for incident_id in index.machine_ids("AOI-01")
    )

    assert max_lexical == pytest.approx(0.3017, abs=1e-3)
    assert max_lexical < SELECTED_CONFIG.lex_rescue


def test_baseline_reproduces_production_gate_metrics(runs):
    summary = runs[1][BASELINE_CONFIG.name][1]

    assert summary.recall_at_5 == pytest.approx(
        0.6625,
        abs=1e-3,
    )
    assert summary.precision_at_5 == pytest.approx(
        0.695,
        abs=1e-3,
    )
    assert summary.mrr == pytest.approx(0.75, abs=1e-3)
    assert summary.accepted_relevant_count == 22
    assert summary.accepted_irrelevant_count == 5
    assert summary.rejected_relevant_count == 8
    assert summary.out_of_pool_relevant_count == 12
    assert summary.no_evidence_leak_count == 0


def test_selected_config_summary(runs):
    summary = runs[1][SELECTED_CONFIG.name][1]

    assert summary.recall_at_5 == pytest.approx(
        0.8375,
        abs=1e-3,
    )
    assert summary.precision_at_5 == pytest.approx(
        0.9,
        abs=1e-3,
    )
    assert summary.mrr == pytest.approx(0.9, abs=1e-3)
    assert summary.accepted_relevant_count == 35
    assert summary.accepted_irrelevant_count == 0
    assert summary.rejected_relevant_count == 7
    assert summary.out_of_pool_relevant_count == 0
    assert summary.no_evidence_leak_count == 0
    assert summary.no_evidence_pass_rate == 1.0


def test_rescue_grid_is_stable(runs, corpus):
    """Identical results across the whole rescue grid."""

    collection, index = corpus
    cases = runs[0]

    summaries = [
        evaluate_config(
            collection,
            index,
            cases,
            rescue_config(lex_rescue),
        )[1]
        for lex_rescue in (0.45, 0.50, 0.55, 0.60, 0.65)
    ]

    fingerprints = {
        (
            summary.recall_at_5,
            summary.precision_at_5,
            summary.mrr,
            summary.accepted_relevant_count,
            summary.accepted_irrelevant_count,
            summary.rejected_relevant_count,
            summary.out_of_pool_relevant_count,
            summary.no_evidence_leak_count,
        )
        for summary in summaries
    }

    assert len(fingerprints) == 1


def test_rescue_threshold_is_monotonic(runs, corpus):
    """Raising the rescue bound never increases acceptance."""

    collection, index = corpus
    cases = runs[0]

    accepted_counts = []

    for lex_rescue in (0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95):
        outcomes, _ = evaluate_config(
            collection,
            index,
            cases,
            rescue_config(lex_rescue),
        )

        evidence = [
            outcome
            for outcome in outcomes
            if outcome.recall_at_5 is not None
        ]

        accepted_counts.append(sum(
            len(outcome.accepted)
            for outcome in evidence
        ))

    assert accepted_counts == sorted(
        accepted_counts,
        reverse=True,
    )


def test_pipeline_is_deterministic(runs, corpus):
    collection, index = corpus
    cases = runs[0]

    first_outcomes, _ = evaluate_config(
        collection,
        index,
        cases,
        SELECTED_CONFIG,
    )
    second_outcomes, _ = evaluate_config(
        collection,
        index,
        cases,
        SELECTED_CONFIG,
    )

    assert [
        [
            candidate.incident_id
            for candidate in outcome.accepted
        ]
        for outcome in first_outcomes
    ] == [
        [
            candidate.incident_id
            for candidate in outcome.accepted
        ]
        for outcome in second_outcomes
    ]
