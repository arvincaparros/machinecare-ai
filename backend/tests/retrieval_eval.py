"""Retrieval evaluation for MachineCare AI semantic incident search.

This module measures raw ChromaDB retrieval and the effect of the RAG
distance filter. It never calls Groq or any other LLM API.

Metric conventions
------------------
Two layers are measured separately.

Raw retrieval quality (top-5 before any distance filter):
- TOP_K is fixed at 5.
- A case with a non-empty expected set is an "evidence case":
  * Recall@5 = |top-5 results intersect expected| / |expected|.
  * Precision@5 = |top-5 results intersect expected| / |returned|,
    where |returned| is the number of results actually returned. It is
    at most 5 and may be smaller when the collection holds fewer matching
    records. If no result is returned, precision is 0.0.
  * Reciprocal rank = 1 / rank of the first relevant result, or 0.0
    when no relevant result is returned.
  * Summary recall, precision and MRR are macro averages over evidence
    cases only.

Threshold-filtered evidence quality (matches with distance <=
rag_service.MAX_DISTANCE, the records the RAG service actually sends to
the LLM):
- Filtered recall@5, precision@5 and MRR reuse the raw definitions after
  the distance filter.
- Relevant retention = expected records retained by the filter /
  expected records retrieved in the raw top-5.
- Irrelevant acceptance = non-expected records retained by the filter /
  all records retained by the filter across evidence cases.
- These metrics isolate the distance filter. The raw metrics isolate
  retrieval. Neither replaces the other.

A case with an empty expected set is a "no-evidence case". It is
excluded from recall, precision, MRR and retention. It passes when every
retrieved match has distance > the configured threshold, meaning the RAG
service would fall back without calling the LLM. The no-evidence pass
rate is the fraction of no-evidence cases that pass.

The threshold sweep recomputes threshold-filtered metrics for several
candidate thresholds over the same raw top-5 results without re-querying
ChromaDB.

The evaluation collection is built in an isolated in-memory client from
data/incidents.json, so the project's persistent ChromaDB collection is
never queried, cleared or overwritten.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import chromadb
from chromadb.config import Settings
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from app.services.rag_service import MAX_DISTANCE
from app.services.vector_store_service import build_incident_text


BASE_DIR = Path(__file__).resolve().parents[1]
SEED_DATA_PATH = BASE_DIR / "data" / "incidents.json"
QUESTIONS_PATH = Path(__file__).with_name(
    "retrieval_eval_questions.json"
)

TOP_K = 5
COLLECTION_NAME = "machinecare_retrieval_eval"

# Candidate thresholds compared over the same raw top-5 results.
THRESHOLD_SWEEP = (
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


@dataclass(frozen=True)
class RetrievalCase:
    """One evaluation question with ground-truth incident IDs."""

    case_id: str
    category: str
    question: str
    machine: str | None
    expected_incident_ids: tuple[str, ...]
    evidence: str


@dataclass
class CaseResult:
    """Retrieval outcome and metrics for one evaluation case."""

    case: RetrievalCase
    retrieved: list[dict]
    recall_at_5: float | None
    precision_at_5: float | None
    reciprocal_rank: float | None
    missed_expected_ids: list[str]
    unexpected_retrieved_ids: list[str]
    no_evidence_passed: bool | None
    filtered: list[dict]
    filtered_recall_at_5: float | None
    filtered_precision_at_5: float | None
    filtered_reciprocal_rank: float | None
    rejected_relevant_ids: list[str]
    accepted_irrelevant_ids: list[str]


@dataclass(frozen=True)
class EvaluationSummary:
    """Macro-averaged retrieval metrics."""

    case_count: int
    evidence_case_count: int
    no_evidence_case_count: int
    recall_at_5: float
    precision_at_5: float
    mrr: float
    no_evidence_pass_rate: float


@dataclass(frozen=True)
class ThresholdSummary:
    """Threshold-filtered evidence metrics for one threshold."""

    max_distance: float
    retrieved_relevant_count: int
    retained_relevant_count: int
    relevant_retention: float
    accepted_match_count: int
    accepted_irrelevant_count: int
    irrelevant_acceptance: float
    no_evidence_leak_count: int
    no_evidence_case_count: int
    no_evidence_pass_rate: float
    filtered_recall_at_5: float
    filtered_precision_at_5: float
    filtered_mrr: float


def load_cases(path: Path = QUESTIONS_PATH) -> list[RetrievalCase]:
    """Load the deterministic evaluation questions."""

    payload = json.loads(path.read_text(encoding="utf-8"))

    return [
        RetrievalCase(
            case_id=item["id"],
            category=item["category"],
            question=item["question"],
            machine=item["machine"],
            expected_incident_ids=tuple(
                item["expected_incident_ids"]
            ),
            evidence=item["evidence"],
        )
        for item in payload["questions"]
    ]


def load_seed_incidents(
    path: Path = SEED_DATA_PATH,
) -> list[dict]:
    """Load the project's historical incident seed data."""

    return json.loads(path.read_text(encoding="utf-8"))


def incident_document(record: dict) -> str:
    """Build the same searchable text used by the production index."""

    incident = SimpleNamespace(
        machine=SimpleNamespace(
            machine_code=record["Machine"]
        ),
        production_line=record["Production Line"],
        issue_category=record["Issue Category"],
        cause=record["Cause"],
        resolution=SimpleNamespace(
            root_cause=record["Root Cause"],
            action_taken=record["Action Taken"],
            preventive_action=record["Preventive Action"],
        ),
    )

    return build_incident_text(incident)


def create_eval_client() -> chromadb.ClientAPI:
    """Create an isolated in-memory ChromaDB client."""

    return chromadb.EphemeralClient(
        settings=Settings(anonymized_telemetry=False),
    )


def build_eval_collection(client, incidents: list[dict]):
    """Populate a dedicated collection from controlled seed fixtures."""

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=DefaultEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )

    closed = [
        record
        for record in incidents
        if record["Status"] == "Closed"
    ]

    if not closed:
        raise ValueError("No closed incidents to index.")

    collection.add(
        ids=[record["Incident ID"] for record in closed],
        documents=[
            incident_document(record)
            for record in closed
        ],
        metadatas=[
            {
                "incident_id": record["Incident ID"],
                "machine": record["Machine"],
                "status": record["Status"],
            }
            for record in closed
        ],
    )

    return collection


def retrieve(
    collection,
    case: RetrievalCase,
    top_k: int = TOP_K,
) -> list[dict]:
    """Retrieve top-k incident IDs for one case."""

    where = {"machine": case.machine} if case.machine else None

    if where is not None:
        available = len(
            collection.get(where=where, include=[])["ids"]
        )
    else:
        available = collection.count()

    if available == 0:
        return []

    results = collection.query(
        query_texts=[case.question],
        n_results=min(top_k, available),
        where=where,
        include=["metadatas", "distances"],
    )

    return [
        {
            "incident_id": incident_id,
            "distance": results["distances"][0][index],
        }
        for index, incident_id in enumerate(results["ids"][0])
    ]


def score_case(
    expected_incident_ids,
    retrieved_incident_ids: list[str],
) -> tuple[float | None, float | None, float | None]:
    """Compute recall@5, precision@5 and reciprocal rank."""

    expected = list(expected_incident_ids)

    if not expected:
        return None, None, None

    expected_set = set(expected)

    hits = [
        incident_id
        for incident_id in retrieved_incident_ids
        if incident_id in expected_set
    ]

    recall = len(hits) / len(expected)

    precision = (
        len(hits) / len(retrieved_incident_ids)
        if retrieved_incident_ids
        else 0.0
    )

    reciprocal_rank = 0.0

    if hits:
        first_rank = retrieved_incident_ids.index(hits[0])
        reciprocal_rank = 1.0 / (first_rank + 1)

    return recall, precision, reciprocal_rank


def filter_by_distance(
    matches: list[dict],
    max_distance: float = MAX_DISTANCE,
) -> list[dict]:
    """Keep only the matches the RAG service would accept."""

    return [
        match
        for match in matches
        if match["distance"] <= max_distance
    ]


def has_verified_evidence(
    retrieved: list[dict],
    max_distance: float = MAX_DISTANCE,
) -> bool:
    """Return True when a match would pass the RAG distance filter."""

    return bool(filter_by_distance(retrieved, max_distance))


def evaluate(
    collection,
    cases: list[RetrievalCase],
    top_k: int = TOP_K,
    max_distance: float = MAX_DISTANCE,
) -> tuple[list[CaseResult], EvaluationSummary]:
    """Run every case and aggregate retrieval metrics."""

    results = []

    for case in cases:
        retrieved = retrieve(collection, case, top_k)
        retrieved_ids = [
            match["incident_id"]
            for match in retrieved
        ]

        recall, precision, reciprocal_rank = score_case(
            case.expected_incident_ids,
            retrieved_ids,
        )

        expected = set(case.expected_incident_ids)

        missed = [
            incident_id
            for incident_id in case.expected_incident_ids
            if incident_id not in retrieved_ids
        ]

        unexpected = [
            incident_id
            for incident_id in retrieved_ids
            if incident_id not in expected
        ]

        filtered = filter_by_distance(
            retrieved,
            max_distance,
        )
        filtered_ids = [
            match["incident_id"]
            for match in filtered
        ]

        (
            filtered_recall,
            filtered_precision,
            filtered_reciprocal_rank,
        ) = score_case(
            case.expected_incident_ids,
            filtered_ids,
        )

        rejected_relevant = [
            incident_id
            for incident_id in case.expected_incident_ids
            if (
                incident_id in retrieved_ids
                and incident_id not in filtered_ids
            )
        ]

        accepted_irrelevant = [
            incident_id
            for incident_id in filtered_ids
            if incident_id not in expected
        ]

        no_evidence_passed = None

        if not case.expected_incident_ids:
            no_evidence_passed = not has_verified_evidence(
                retrieved,
                max_distance,
            )

        results.append(
            CaseResult(
                case=case,
                retrieved=retrieved,
                recall_at_5=recall,
                precision_at_5=precision,
                reciprocal_rank=reciprocal_rank,
                missed_expected_ids=missed,
                unexpected_retrieved_ids=unexpected,
                no_evidence_passed=no_evidence_passed,
                filtered=filtered,
                filtered_recall_at_5=filtered_recall,
                filtered_precision_at_5=filtered_precision,
                filtered_reciprocal_rank=(
                    filtered_reciprocal_rank
                ),
                rejected_relevant_ids=rejected_relevant,
                accepted_irrelevant_ids=accepted_irrelevant,
            )
        )

    evidence_results = [
        result
        for result in results
        if result.recall_at_5 is not None
    ]

    no_evidence_results = [
        result
        for result in results
        if result.no_evidence_passed is not None
    ]

    summary = EvaluationSummary(
        case_count=len(results),
        evidence_case_count=len(evidence_results),
        no_evidence_case_count=len(no_evidence_results),
        recall_at_5=_mean([
            result.recall_at_5
            for result in evidence_results
        ]),
        precision_at_5=_mean([
            result.precision_at_5
            for result in evidence_results
        ]),
        mrr=_mean([
            result.reciprocal_rank
            for result in evidence_results
        ]),
        no_evidence_pass_rate=_mean([
            1.0 if result.no_evidence_passed else 0.0
            for result in no_evidence_results
        ]),
    )

    return results, summary


def _mean(values: list[float | None]) -> float:
    """Return the mean of non-empty values, or 0.0."""

    present = [
        value
        for value in values
        if value is not None
    ]

    if not present:
        return 0.0

    return sum(present) / len(present)


def summarize_threshold(
    results: list[CaseResult],
    max_distance: float,
) -> ThresholdSummary:
    """Measure the distance filter over already-retrieved results."""

    evidence_results = [
        result
        for result in results
        if result.recall_at_5 is not None
    ]

    no_evidence_results = [
        result
        for result in results
        if result.no_evidence_passed is not None
    ]

    retrieved_relevant = 0
    retained_relevant = 0
    accepted_matches = 0
    accepted_irrelevant = 0
    filtered_recall = []
    filtered_precision = []
    filtered_rr = []
    no_evidence_leaks = 0

    for result in evidence_results:
        expected = set(result.case.expected_incident_ids)
        filtered = filter_by_distance(
            result.retrieved,
            max_distance,
        )
        filtered_ids = [
            match["incident_id"]
            for match in filtered
        ]

        retrieved_relevant += sum(
            1
            for match in result.retrieved
            if match["incident_id"] in expected
        )
        retained_relevant += sum(
            1
            for match in filtered
            if match["incident_id"] in expected
        )
        accepted_matches += len(filtered)
        accepted_irrelevant += sum(
            1
            for match in filtered
            if match["incident_id"] not in expected
        )

        recall, precision, reciprocal_rank = score_case(
            result.case.expected_incident_ids,
            filtered_ids,
        )
        filtered_recall.append(recall)
        filtered_precision.append(precision)
        filtered_rr.append(reciprocal_rank)

    for result in no_evidence_results:
        if filter_by_distance(result.retrieved, max_distance):
            no_evidence_leaks += 1

    return ThresholdSummary(
        max_distance=max_distance,
        retrieved_relevant_count=retrieved_relevant,
        retained_relevant_count=retained_relevant,
        relevant_retention=(
            retained_relevant / retrieved_relevant
            if retrieved_relevant
            else 0.0
        ),
        accepted_match_count=accepted_matches,
        accepted_irrelevant_count=accepted_irrelevant,
        irrelevant_acceptance=(
            accepted_irrelevant / accepted_matches
            if accepted_matches
            else 0.0
        ),
        no_evidence_leak_count=no_evidence_leaks,
        no_evidence_case_count=len(no_evidence_results),
        no_evidence_pass_rate=(
            1.0 - no_evidence_leaks / len(no_evidence_results)
            if no_evidence_results
            else 0.0
        ),
        filtered_recall_at_5=_mean(filtered_recall),
        filtered_precision_at_5=_mean(filtered_precision),
        filtered_mrr=_mean(filtered_rr),
    )


def format_report(
    results: list[CaseResult],
    summary: EvaluationSummary,
    max_distance: float = MAX_DISTANCE,
) -> str:
    """Render a human-readable evaluation report."""

    lines = [
        "MachineCare retrieval evaluation",
        f"TOP_K={TOP_K} MAX_DISTANCE={max_distance:.2f}",
        "=" * 72,
    ]

    for result in results:
        case = result.case

        if result.no_evidence_passed is None:
            status = (
                "OK"
                if result.recall_at_5 == 1.0
                else (
                    "PARTIAL"
                    if result.recall_at_5
                    else "MISS"
                )
            )
        else:
            status = (
                "OK"
                if result.no_evidence_passed
                else "LEAK"
            )

        lines.append("")
        lines.append(
            f"[{status}] {case.case_id} "
            f"({case.category})"
        )
        lines.append(f"  question: {case.question}")
        lines.append(
            f"  machine: {case.machine or '(all)'}"
        )
        lines.append(
            "  expected: "
            + (
                ", ".join(case.expected_incident_ids)
                or "(none)"
            )
        )
        lines.append(
            "  retrieved: "
            + (
                ", ".join(
                    f"{match['incident_id']} "
                    f"({match['distance']:.4f})"
                    for match in result.retrieved
                )
                or "(none)"
            )
        )

        if result.no_evidence_passed is None:
            lines.append(
                f"  raw recall@5={result.recall_at_5:.3f} "
                f"precision@5={result.precision_at_5:.3f} "
                f"rr={result.reciprocal_rank:.3f}"
            )
            lines.append(
                f"  filtered recall@5="
                f"{result.filtered_recall_at_5:.3f} "
                f"precision@5="
                f"{result.filtered_precision_at_5:.3f} "
                f"rr={result.filtered_reciprocal_rank:.3f}"
            )
            lines.append(
                "  missed (raw): "
                + (
                    ", ".join(result.missed_expected_ids)
                    or "(none)"
                )
            )
            lines.append(
                "  unexpected (raw): "
                + (
                    ", ".join(result.unexpected_retrieved_ids)
                    or "(none)"
                )
            )
            rejected = [
                f"{match['incident_id']} "
                f"({match['distance']:.4f})"
                for match in result.retrieved
                if match["incident_id"]
                in result.rejected_relevant_ids
            ]
            lines.append(
                "  rejected relevant: "
                + (", ".join(rejected) or "(none)")
            )
            accepted = [
                f"{match['incident_id']} "
                f"({match['distance']:.4f})"
                for match in result.filtered
                if match["incident_id"]
                in result.accepted_irrelevant_ids
            ]
            lines.append(
                "  accepted irrelevant: "
                + (", ".join(accepted) or "(none)")
            )
        else:
            accepted = [
                f"{match['incident_id']} "
                f"({match['distance']:.4f})"
                for match in result.filtered
            ]
            lines.append(
                "  accepted evidence: "
                + (", ".join(accepted) or "(none)")
            )

    threshold_summary = summarize_threshold(
        results,
        max_distance,
    )

    lines.append("")
    lines.append("=" * 72)
    lines.append(
        "Raw retrieval summary (top-5, no distance filter)"
    )
    lines.append(
        f"  cases={summary.case_count} "
        f"evidence={summary.evidence_case_count} "
        f"no_evidence={summary.no_evidence_case_count}"
    )
    lines.append(
        f"  recall@5={summary.recall_at_5:.3f}"
    )
    lines.append(
        f"  precision@5={summary.precision_at_5:.3f}"
    )
    lines.append(f"  mrr={summary.mrr:.3f}")

    lines.append("")
    lines.append(
        "Threshold-filtered evidence summary "
        f"(MAX_DISTANCE={max_distance:.2f})"
    )
    lines.append(
        f"  relevant retention="
        f"{threshold_summary.retained_relevant_count}/"
        f"{threshold_summary.retrieved_relevant_count} "
        f"({threshold_summary.relevant_retention:.3f})"
    )
    lines.append(
        f"  irrelevant acceptance="
        f"{threshold_summary.accepted_irrelevant_count}/"
        f"{threshold_summary.accepted_match_count} "
        f"({threshold_summary.irrelevant_acceptance:.3f})"
    )
    lines.append(
        f"  recall@5="
        f"{threshold_summary.filtered_recall_at_5:.3f}"
    )
    lines.append(
        f"  precision@5="
        f"{threshold_summary.filtered_precision_at_5:.3f}"
    )
    lines.append(
        f"  mrr={threshold_summary.filtered_mrr:.3f}"
    )
    lines.append(
        "  no_evidence_pass_rate="
        f"{threshold_summary.no_evidence_pass_rate:.3f}"
    )
    lines.append(
        f"  no_evidence_leaks="
        f"{threshold_summary.no_evidence_leak_count}/"
        f"{threshold_summary.no_evidence_case_count}"
    )

    return "\n".join(lines)


def format_threshold_report(
    results: list[CaseResult],
    thresholds=THRESHOLD_SWEEP,
) -> str:
    """Render the threshold sweep over the raw top-5 results."""

    lines = [
        "Threshold sweep (same raw top-5 results, no re-query)",
        (
            "  threshold  rel_kept  retention  irr_taken  "
            "irr_rate  leaks  filt_recall  filt_prec  filt_mrr"
        ),
    ]

    for threshold in thresholds:
        summary = summarize_threshold(results, threshold)
        lines.append(
            f"  {threshold:>9.3f}  "
            f"{summary.retained_relevant_count:>3}/"
            f"{summary.retrieved_relevant_count:<4}  "
            f"{summary.relevant_retention:>9.3f}  "
            f"{summary.accepted_irrelevant_count:>3}/"
            f"{summary.accepted_match_count:<5}  "
            f"{summary.irrelevant_acceptance:>8.3f}  "
            f"{summary.no_evidence_leak_count:>2}/"
            f"{summary.no_evidence_case_count:<2}  "
            f"{summary.filtered_recall_at_5:>11.3f}  "
            f"{summary.filtered_precision_at_5:>9.3f}  "
            f"{summary.filtered_mrr:>8.3f}"
        )

    return "\n".join(lines)


def main() -> None:
    """Run the evaluation against an isolated in-memory collection."""

    cases = load_cases()
    incidents = load_seed_incidents()
    client = create_eval_client()
    collection = build_eval_collection(client, incidents)

    results, summary = evaluate(collection, cases)

    print(
        f"Indexed {collection.count()} closed incidents "
        "into an isolated in-memory collection."
    )
    print(format_report(results, summary))
    print()
    print(format_threshold_report(results))


if __name__ == "__main__":
    main()
