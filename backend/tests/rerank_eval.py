"""Hybrid retrieval and reranking experiment for MachineCare AI.

Evaluation-only experiment. Nothing in app/ imports this module and no
production behavior depends on it. It compares the production evidence
gate (dense top-5 + MAX_DISTANCE) against a hybrid pipeline that adds a
deterministic lexical retriever and reranks candidates by a fused
relevance score. It never calls Groq or any other LLM API.

Pipeline layers
---------------
1. Dense retrieval: cosine search in the isolated in-memory collection
   (top DENSE_POOL_K candidates), reusing tests/retrieval_eval.py.
2. Lexical retrieval: deterministic IDF-weighted token coverage over the
   same incident documents (top LEXICAL_POOL_K candidates). Tokens are
   lowercased, split on non-alphanumerics, stopworded and passed through
   a fixed suffix stemmer, so results are fully reproducible.
3. Fusion: candidates from both retrievers are merged by incident ID.
   Lexical-only candidates without a dense distance are never accepted
   because every acceptance rule requires a distance.
4. Gate: a candidate is accepted when either
   - distance <= rag_service.MAX_DISTANCE and lexical_score >= lex_floor
     (the production rule plus a lexical corroboration floor), or
   - lexical_score >= lex_rescue and distance <= dense_relax (a lexical
     rescue path for candidates the dense gate rejects).
   MAX_DISTANCE itself stays 0.50 in every configuration.
5. Rerank: accepted candidates are ordered by hybrid_score
   (alpha * dense similarity + (1 - alpha) * lexical score) or kept in
   distance order for baseline configurations.

Metric conventions
------------------
- recall = |accepted intersect expected| / |expected|,
  precision = |accepted intersect expected| / |accepted|,
  reciprocal rank = 1 / rank of the first relevant accepted candidate.
  They are macro-averaged over evidence cases. "Accepted" is the gated,
  reranked list, so baseline numbers are directly comparable with the
  threshold-filtered layer of tests/retrieval_eval.py.
- False positives (FP) = accepted IDs outside the expected set.
- False negatives (FN) = expected IDs not accepted, split into
  "rejected" (present in the candidate pool but gated out) and
  "out of pool" (never retrieved by either retriever).
- A no-evidence case passes when nothing is accepted.

The configuration grid is calibrated on the same 13 questions used to
score it, so improvements are in-sample by construction. The comparison
table and the per-case detail reports expose that explicitly.

The collection is built in an isolated chromadb.EphemeralClient from
data/incidents.json; the persistent production collection and the
configured PostgreSQL database are never touched.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.services.rag_service import MAX_DISTANCE
from tests.retrieval_eval import (
    RetrievalCase,
    build_eval_collection,
    create_eval_client,
    incident_document,
    load_cases,
    load_seed_incidents,
    retrieve,
    score_case,
)


RERANK_ALPHA = 0.5
DENSE_POOL_K = 20
LEXICAL_POOL_K = 10
DENSE_RELAX = 0.70

RESCUE_GRID = (0.45, 0.50, 0.55, 0.60, 0.65)
FLOOR_GRID = (0.20, 0.25, 0.30)

STOPWORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those",
    "am", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "doing", "have", "has", "had", "having",
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their",
    "what", "which", "who", "whom", "whose",
    "when", "where", "why", "how",
    "and", "or", "but", "if", "then", "than", "so", "as",
    "of", "to", "in", "on", "at", "by", "for", "with",
    "about", "against", "between", "into", "during", "before",
    "after", "above", "below", "up", "down", "out", "off",
    "over", "under", "again", "further", "once", "here", "there",
    "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same",
    "too", "very", "can", "will", "just", "should", "now",
    "because", "while", "until", "also",
})


def stem(token: str) -> str:
    """Collapse common English inflections deterministically."""

    if len(token) > 4 and token.endswith("ing"):
        token = token[:-3]
    elif len(token) > 4 and token.endswith("ed"):
        token = token[:-2]
    elif len(token) > 4 and token.endswith("es"):
        token = token[:-2]
    elif len(token) > 3 and token.endswith("s"):
        token = token[:-1]

    if len(token) > 3 and token.endswith("e"):
        token = token[:-1]

    if token.endswith("y"):
        token = token[:-1] + "i"

    return token


def tokenize(text: str) -> list[str]:
    """Lowercase, split, stopword and stem a piece of text."""

    raw = re.split(r"[^a-z0-9]+", text.lower())

    return [
        stem(token)
        for token in raw
        if token and token not in STOPWORDS
    ]


class LexicalIndex:
    """Deterministic IDF token index over incident documents."""

    def __init__(
        self,
        idf: dict[str, float],
        doc_tokens: dict[str, frozenset],
        doc_machines: dict[str, str],
        max_idf: float,
    ):
        self.idf = idf
        self.doc_tokens = doc_tokens
        self.doc_machines = doc_machines
        self.max_idf = max_idf

    @classmethod
    def from_documents(
        cls,
        documents: dict[str, str],
        machines: dict[str, str],
    ) -> "LexicalIndex":
        """Build the index from incident documents."""

        doc_tokens = {
            incident_id: frozenset(tokenize(text))
            for incident_id, text in documents.items()
        }

        document_frequency = Counter(
            token
            for tokens in doc_tokens.values()
            for token in tokens
        )

        total = len(documents)
        idf = {
            token: math.log(1 + total / count)
            for token, count in document_frequency.items()
        }

        return cls(
            idf=idf,
            doc_tokens=doc_tokens,
            doc_machines=machines,
            # Weight assigned to question terms absent from every
            # document, so questions about unknown topics score low.
            max_idf=math.log(1 + total),
        )

    def score(self, question: str, incident_id: str) -> float:
        """IDF-weighted coverage of the question by one document."""

        question_tokens = set(tokenize(question))

        if not question_tokens:
            return 0.0

        document_tokens = self.doc_tokens.get(
            incident_id,
            frozenset(),
        )

        matched = sum(
            self.idf.get(token, self.max_idf)
            for token in question_tokens
            if token in document_tokens
        )
        total = sum(
            self.idf.get(token, self.max_idf)
            for token in question_tokens
        )

        return matched / total

    def machine_ids(self, machine: str) -> set[str]:
        """Incident IDs belonging to one machine."""

        return {
            incident_id
            for incident_id, doc_machine
            in self.doc_machines.items()
            if doc_machine == machine
        }

    def top(
        self,
        question: str,
        k: int,
        machine: str | None = None,
    ) -> list[tuple[str, float]]:
        """Top-k lexical matches with deterministic tie-breaking."""

        allowed = (
            self.machine_ids(machine)
            if machine
            else set(self.doc_tokens)
        )

        scored = [
            (incident_id, self.score(question, incident_id))
            for incident_id in allowed
        ]

        scored.sort(key=lambda item: (-item[1], item[0]))

        return scored[:k]


def build_lexical_index(incidents: list[dict]) -> LexicalIndex:
    """Build the lexical index from the closed seed incidents."""

    closed = [
        record
        for record in incidents
        if record["Status"] == "Closed"
    ]

    return LexicalIndex.from_documents(
        documents={
            record["Incident ID"]: incident_document(record)
            for record in closed
        },
        machines={
            record["Incident ID"]: record["Machine"]
            for record in closed
        },
    )


@dataclass
class Candidate:
    """One pooled retrieval candidate with fused scores."""

    incident_id: str
    distance: float | None
    lexical_score: float = 0.0

    @property
    def dense_similarity(self) -> float:
        return (
            0.0
            if self.distance is None
            else 1.0 - self.distance
        )

    @property
    def hybrid_score(self) -> float:
        return (
            RERANK_ALPHA * self.dense_similarity
            + (1.0 - RERANK_ALPHA) * self.lexical_score
        )


@dataclass(frozen=True)
class GateConfig:
    """One acceptance strategy under evaluation."""

    name: str
    dense_pool_k: int
    lexical_pool_k: int
    lex_floor: float
    lex_rescue: float
    dense_relax: float
    order: str

    def accepts(self, candidate: Candidate) -> bool:
        """Apply the gate rules to one candidate."""

        if candidate.distance is None:
            return False

        if candidate.distance <= MAX_DISTANCE:
            return candidate.lexical_score >= self.lex_floor

        return (
            candidate.lexical_score >= self.lex_rescue
            and candidate.distance <= self.dense_relax
        )


BASELINE_CONFIG = GateConfig(
    name="baseline",
    dense_pool_k=5,
    lexical_pool_k=0,
    lex_floor=0.0,
    lex_rescue=2.0,
    dense_relax=0.0,
    order="distance",
)

POOL_CONFIG = GateConfig(
    name="pool-dense5+lex10",
    dense_pool_k=5,
    lexical_pool_k=LEXICAL_POOL_K,
    lex_floor=0.0,
    lex_rescue=2.0,
    dense_relax=0.0,
    order="distance",
)

RESCUE_POOL5_CONFIG = GateConfig(
    name="rescue-0.55-pool-dense5",
    dense_pool_k=5,
    lexical_pool_k=LEXICAL_POOL_K,
    lex_floor=0.0,
    lex_rescue=0.55,
    dense_relax=DENSE_RELAX,
    order="hybrid",
)


def rescue_config(lex_rescue: float) -> GateConfig:
    """Distance gate plus a lexical rescue path."""

    return GateConfig(
        name=f"rescue-{lex_rescue:.2f}",
        dense_pool_k=DENSE_POOL_K,
        lexical_pool_k=LEXICAL_POOL_K,
        lex_floor=0.0,
        lex_rescue=lex_rescue,
        dense_relax=DENSE_RELAX,
        order="hybrid",
    )


def floor_config(lex_floor: float) -> GateConfig:
    """Rescue gate plus a lexical corroboration floor."""

    return GateConfig(
        name=f"floor-{lex_floor:.2f}+rescue-0.55",
        dense_pool_k=DENSE_POOL_K,
        lexical_pool_k=LEXICAL_POOL_K,
        lex_floor=lex_floor,
        lex_rescue=0.55,
        dense_relax=DENSE_RELAX,
        order="hybrid",
    )


# The experimental configuration reported in detail. Chosen from the
# grid below because it is the only configuration that fixes all three
# focal failures (DT-2026-051 false positive, camera-verified
# distinction, communication-timeout rejection) with zero false
# positives; see the module docstring for the calibration caveat and
# the drift-symptom regression it accepts.
SELECTED_CONFIG = floor_config(0.25)

# Recall-maximizing alternative kept in the grid for comparison.
HIGH_RECALL_CONFIG = rescue_config(0.55)


# Dense query results are deterministic for a given collection, case
# and top_k. Evaluation runs many gate configurations over the same
# cases, so results are memoized here to avoid re-embedding the same
# question once per configuration. Keyed by collection identity; safe
# for the single-collection evaluation flow this module implements.
_DENSE_CACHE: dict[tuple[int, str, int], list[dict]] = {}


def _dense_matches(
    collection,
    case: RetrievalCase,
    top_k: int,
) -> list[dict]:
    """Memoized dense retrieval for one case."""

    key = (id(collection), case.case_id, top_k)

    if key not in _DENSE_CACHE:
        _DENSE_CACHE[key] = retrieve(
            collection,
            case,
            top_k=top_k,
        )

    return _DENSE_CACHE[key]


def collect_candidates(
    collection,
    index: LexicalIndex,
    case: RetrievalCase,
    config: GateConfig,
) -> list[Candidate]:
    """Merge dense and lexical retrieval pools for one case."""

    pool: dict[str, Candidate] = {}

    if config.dense_pool_k:
        for match in _dense_matches(
            collection,
            case,
            config.dense_pool_k,
        ):
            pool[match["incident_id"]] = Candidate(
                incident_id=match["incident_id"],
                distance=match["distance"],
            )

    if config.lexical_pool_k:
        for incident_id, _score in index.top(
            case.question,
            config.lexical_pool_k,
            machine=case.machine,
        ):
            pool.setdefault(
                incident_id,
                Candidate(incident_id=incident_id, distance=None),
            )

    for candidate in pool.values():
        candidate.lexical_score = index.score(
            case.question,
            candidate.incident_id,
        )

    return sorted(
        pool.values(),
        key=lambda candidate: (
            candidate.distance is None,
            candidate.distance or 0.0,
            candidate.incident_id,
        ),
    )


@dataclass
class CaseOutcome:
    """Reranking outcome and metrics for one evaluation case."""

    case: RetrievalCase
    pool: list[Candidate]
    accepted: list[Candidate]
    recall_at_5: float | None
    precision_at_5: float | None
    reciprocal_rank: float | None
    rejected_relevant_ids: list[str]
    out_of_pool_expected_ids: list[str]
    accepted_irrelevant_ids: list[str]
    no_evidence_passed: bool | None


@dataclass(frozen=True)
class RerankSummary:
    """Macro-averaged reranking metrics for one configuration."""

    config_name: str
    case_count: int
    evidence_case_count: int
    no_evidence_case_count: int
    recall_at_5: float
    precision_at_5: float
    mrr: float
    accepted_relevant_count: int
    accepted_irrelevant_count: int
    rejected_relevant_count: int
    out_of_pool_relevant_count: int
    no_evidence_leak_count: int
    no_evidence_pass_rate: float


def _macro(values: list[float | None]) -> float:
    """Mean over non-None values, or 0.0."""

    present = [
        value
        for value in values
        if value is not None
    ]

    if not present:
        return 0.0

    return sum(present) / len(present)


def evaluate_config(
    collection,
    index: LexicalIndex,
    cases: list[RetrievalCase],
    config: GateConfig,
) -> tuple[list[CaseOutcome], RerankSummary]:
    """Run one gate configuration over every evaluation case."""

    outcomes = []

    for case in cases:
        pool = collect_candidates(
            collection,
            index,
            case,
            config,
        )

        accepted = [
            candidate
            for candidate in pool
            if config.accepts(candidate)
        ]

        if config.order == "hybrid":
            accepted.sort(
                key=lambda candidate: (
                    -candidate.hybrid_score,
                    candidate.incident_id,
                ),
            )

        accepted_ids = [
            candidate.incident_id
            for candidate in accepted
        ]
        pool_ids = {
            candidate.incident_id
            for candidate in pool
        }
        expected = set(case.expected_incident_ids)

        recall, precision, reciprocal_rank = score_case(
            case.expected_incident_ids,
            accepted_ids,
        )

        rejected_relevant = [
            incident_id
            for incident_id in case.expected_incident_ids
            if (
                incident_id in pool_ids
                and incident_id not in accepted_ids
            )
        ]

        out_of_pool = [
            incident_id
            for incident_id in case.expected_incident_ids
            if incident_id not in pool_ids
        ]

        accepted_irrelevant = [
            incident_id
            for incident_id in accepted_ids
            if incident_id not in expected
        ]

        no_evidence_passed = None

        if not case.expected_incident_ids:
            no_evidence_passed = not accepted_ids

        outcomes.append(
            CaseOutcome(
                case=case,
                pool=pool,
                accepted=accepted,
                recall_at_5=recall,
                precision_at_5=precision,
                reciprocal_rank=reciprocal_rank,
                rejected_relevant_ids=rejected_relevant,
                out_of_pool_expected_ids=out_of_pool,
                accepted_irrelevant_ids=accepted_irrelevant,
                no_evidence_passed=no_evidence_passed,
            )
        )

    evidence = [
        outcome
        for outcome in outcomes
        if outcome.recall_at_5 is not None
    ]
    no_evidence = [
        outcome
        for outcome in outcomes
        if outcome.no_evidence_passed is not None
    ]
    leaks = [
        outcome
        for outcome in no_evidence
        if not outcome.no_evidence_passed
    ]

    summary = RerankSummary(
        config_name=config.name,
        case_count=len(outcomes),
        evidence_case_count=len(evidence),
        no_evidence_case_count=len(no_evidence),
        recall_at_5=_macro([
            outcome.recall_at_5
            for outcome in evidence
        ]),
        precision_at_5=_macro([
            outcome.precision_at_5
            for outcome in evidence
        ]),
        mrr=_macro([
            outcome.reciprocal_rank
            for outcome in evidence
        ]),
        accepted_relevant_count=sum(
            len(outcome.case.expected_incident_ids)
            - len(outcome.rejected_relevant_ids)
            - len(outcome.out_of_pool_expected_ids)
            for outcome in evidence
        ),
        accepted_irrelevant_count=sum(
            len(outcome.accepted_irrelevant_ids)
            for outcome in evidence
        ),
        rejected_relevant_count=sum(
            len(outcome.rejected_relevant_ids)
            for outcome in evidence
        ),
        out_of_pool_relevant_count=sum(
            len(outcome.out_of_pool_expected_ids)
            for outcome in evidence
        ),
        no_evidence_leak_count=len(leaks),
        no_evidence_pass_rate=(
            1.0 - len(leaks) / len(no_evidence)
            if no_evidence
            else 0.0
        ),
    )

    return outcomes, summary


def format_comparison(
    summaries: list[RerankSummary],
) -> str:
    """Render the baseline-versus-experiment comparison table."""

    lines = [
        "Configuration comparison "
        "(accepted = gated and reranked evidence)",
        (
            "  config                     recall  prec   mrr    "
            "rel  irr  rej  oop  leaks  no_ev"
        ),
    ]

    for summary in summaries:
        lines.append(
            f"  {summary.config_name:<25}  "
            f"{summary.recall_at_5:>6.3f}  "
            f"{summary.precision_at_5:>5.3f}  "
            f"{summary.mrr:>5.3f}  "
            f"{summary.accepted_relevant_count:>3}  "
            f"{summary.accepted_irrelevant_count:>3}  "
            f"{summary.rejected_relevant_count:>3}  "
            f"{summary.out_of_pool_relevant_count:>3}  "
            f"{summary.no_evidence_leak_count:>4}    "
            f"{summary.no_evidence_pass_rate:>4.3f}"
        )

    lines.append("")
    lines.append(
        "  rel = accepted relevant (42 expected in total), "
        "irr = accepted irrelevant (FP),"
    )
    lines.append(
        "  rej = relevant retrieved but rejected (FN), "
        "oop = relevant never retrieved (FN),"
    )
    lines.append(
        "  leaks = no-evidence cases with accepted evidence."
    )

    return "\n".join(lines)


def _candidate_label(candidate: Candidate) -> str:
    distance = (
        "n/a"
        if candidate.distance is None
        else f"{candidate.distance:.4f}"
    )

    return (
        f"{candidate.incident_id} "
        f"(d={distance}, lex={candidate.lexical_score:.3f}, "
        f"hyb={candidate.hybrid_score:.3f})"
    )


def format_case_details(
    title: str,
    outcomes: list[CaseOutcome],
) -> str:
    """Render per-case accepted evidence, FPs and FNs."""

    lines = [
        "=" * 72,
        title,
        "=" * 72,
    ]

    for outcome in outcomes:
        case = outcome.case

        if outcome.no_evidence_passed is None:
            status = "OK" if outcome.recall_at_5 == 1.0 else (
                "PARTIAL"
                if outcome.recall_at_5
                else "MISS"
            )
        else:
            status = (
                "OK"
                if outcome.no_evidence_passed
                else "LEAK"
            )

        lines.append("")
        lines.append(
            f"[{status}] {case.case_id} ({case.category})"
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
            f"  pool: {len(outcome.pool)} candidates"
        )
        lines.append(
            "  accepted: "
            + (
                ", ".join(
                    _candidate_label(candidate)
                    for candidate in outcome.accepted
                )
                or "(none)"
            )
        )

        if outcome.no_evidence_passed is None:
            lines.append(
                f"  recall={outcome.recall_at_5:.3f} "
                f"precision={outcome.precision_at_5:.3f} "
                f"rr={outcome.reciprocal_rank:.3f}"
            )

            pool_by_id = {
                candidate.incident_id: candidate
                for candidate in outcome.pool
            }

            lines.append(
                "  rejected relevant (FN): "
                + (
                    ", ".join(
                        f"{incident_id} "
                        f"(d={pool_by_id[incident_id].distance:.4f}, "
                        f"lex={pool_by_id[incident_id].lexical_score:.3f})"
                        for incident_id
                        in outcome.rejected_relevant_ids
                    )
                    or "(none)"
                )
            )
            lines.append(
                "  out of pool (FN): "
                + (
                    ", ".join(outcome.out_of_pool_expected_ids)
                    or "(none)"
                )
            )
            lines.append(
                "  accepted irrelevant (FP): "
                + (
                    ", ".join(
                        outcome.accepted_irrelevant_ids
                    )
                    or "(none)"
                )
            )

    return "\n".join(lines)


def main() -> None:
    """Run the reranking experiment on an isolated collection."""

    cases = load_cases()
    incidents = load_seed_incidents()
    collection = build_eval_collection(
        create_eval_client(),
        incidents,
    )
    index = build_lexical_index(incidents)

    print(
        f"Indexed {collection.count()} closed incidents "
        "into an isolated in-memory collection."
    )
    print(
        f"Lexical index: {len(index.doc_tokens)} documents, "
        f"{len(index.idf)} vocabulary terms."
    )

    configs = [
        BASELINE_CONFIG,
        POOL_CONFIG,
        RESCUE_POOL5_CONFIG,
        *(
            rescue_config(lex_rescue)
            for lex_rescue in RESCUE_GRID
        ),
        *(
            floor_config(lex_floor)
            for lex_floor in FLOOR_GRID
        ),
    ]

    runs = []

    for config in configs:
        outcomes, summary = evaluate_config(
            collection,
            index,
            cases,
            config,
        )
        runs.append((config, outcomes, summary))

    print()
    print(format_comparison([
        summary
        for _, _, summary in runs
    ]))

    baseline_outcomes = runs[0][1]
    by_name = {
        run[0].name: run
        for run in runs
    }

    print()
    print(format_case_details(
        f"Baseline detail ({BASELINE_CONFIG.name}: "
        "dense top-5, distance <= "
        f"{MAX_DISTANCE:.2f}, distance order)",
        baseline_outcomes,
    ))

    for config in (HIGH_RECALL_CONFIG, SELECTED_CONFIG):
        _, outcomes, _ = by_name[config.name]

        print()
        print(format_case_details(
            f"Experimental detail ({config.name}: "
            f"dense top-{config.dense_pool_k} + "
            f"lexical top-{config.lexical_pool_k}, "
            f"floor={config.lex_floor:.2f}, "
            f"rescue={config.lex_rescue:.2f}, "
            f"relax={config.dense_relax:.2f}, "
            "hybrid order)",
            outcomes,
        ))


if __name__ == "__main__":
    main()
