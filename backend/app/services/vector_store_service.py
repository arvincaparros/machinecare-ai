
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from app.db.database import SessionLocal
from app.repositories.incident_repository import get_resolved_incidents


BASE_DIR = Path(__file__).resolve().parents[2]
VECTOR_DB_PATH = BASE_DIR / "data" / "chroma_db"

client = chromadb.PersistentClient(
    path=str(VECTOR_DB_PATH)
)

embedding_function = DefaultEmbeddingFunction()

collection = client.get_or_create_collection(
    name="machine_downtime_incidents",
    embedding_function=embedding_function,
    metadata={"hnsw:space": "cosine"}
)


def build_incident_text(incident) -> str:
    """Build searchable text from a PostgreSQL incident."""

    return (
        f"Machine: {incident.machine.machine_code}\n"
        f"Production Line: {incident.production_line or ''}\n"
        f"Issue Category: {incident.issue_category or ''}\n"
        f"Cause: {incident.cause}\n"
        f"Root Cause: {incident.resolution.root_cause if incident.resolution else ''}\n"
        f"Action Taken: {incident.resolution.action_taken if incident.resolution else ''}\n"
        f"Preventive Action: {incident.resolution.preventive_action if incident.resolution else ''}"
    )


def index_incidents() -> int:
    """Index resolved PostgreSQL incidents in ChromaDB."""

    with SessionLocal() as db:
        incidents = get_resolved_incidents(db)

        if not incidents:
            return collection.count()

        ids = []
        documents = []
        metadatas = []

        for incident in incidents:
            ids.append(incident.incident_id)
            documents.append(build_incident_text(incident))

            metadatas.append({
                "incident_id": incident.incident_id,
                "machine": incident.machine.machine_code,
                "status": incident.status,
            })

        collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas
        )

        return collection.count()


def search_similar_incidents(
    question: str,
    top_k: int = 5,
    machine: str | None = None
) -> list[dict]:
    """Retrieve semantically similar indexed incidents."""

    if not question.strip() or top_k <= 0:
        return []

    filters = {"machine": machine} if machine else None

    available = (
        collection.get(where=filters, include=[])
        if filters else None
    )

    count = (
        len(available["ids"])
        if available is not None
        else collection.count()
    )

    if count == 0:
        return []

    results = collection.query(
        query_texts=[question],
        n_results=min(top_k, count),
        where=filters,
        include=["documents", "metadatas", "distances"]
    )

    matches = []

    for i, incident_id in enumerate(results["ids"][0]):
        matches.append({
            "incident_id": incident_id,
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i]
        })

    return matches
