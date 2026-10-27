
import json
import re
from pathlib import Path

# Resolve path relative to the project, not the terminal directory.
DATA_FILE = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "incidents.json"
)


def load_incidents() -> list[dict]:
    """Load historical downtime records from JSON."""
    with DATA_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def search_incidents(
    query: str,
    top_k: int = 5
) -> list[dict]:
    """Find incidents matching terms from a search query."""

    if not query.strip() or top_k <= 0:
        return []

    incidents = load_incidents()

    # Only search these relevant fields.
    searchable_fields = [
        "Machine",
        "Issue Category",
        "Cause",
        "Root Cause",
        "Action Taken",
        "Preventive Action",
    ]

    # Extract words, ignoring common stop words.
    stop_words = {
        "the", "a", "an", "is", "are", "how", "what",
        "was", "were", "our", "has", "have", "and",
        "for", "to", "of", "in", "it", "we", "again",
        "previously", "before", "this", "issue"
    }

    query_terms = set(re.findall(r"\w+", query.lower()))
    query_terms -= stop_words

    results = []

    for incident in incidents:
        score = 0

        for field in searchable_fields:
            field_terms = set(
                re.findall(
                    r"\w+",
                    str(incident.get(field, "")).lower()
                )
            )

            score += len(query_terms & field_terms)

        if score > 0:
            results.append({
                "score": score,
                "incident": incident
            })

    results.sort(
        key=lambda result: result["score"],
        reverse=True
    )

    return results[:top_k]
