
from app.services.vector_store_service import (
    search_similar_incidents
)


def main():
    question = "AOI camera inspection is inaccurate"

    print(f"Question: {question}")
    print("-" * 60)

    results = search_similar_incidents(
        question=question,
        top_k=5,
        machine="AOI-01"
    )

    for result in results:
        print(f"Incident: {result['incident_id']}")
        print(f"Machine: {result['metadata']['machine']}")
        print(f"Distance: {result['distance']:.4f}")
        print(result["document"])
        print("-" * 60)


if __name__ == "__main__":
    main()
