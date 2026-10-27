
from app.services.incident_retrieval_service import (
    search_incidents
)


def main():
    question = "AOI camera calibration failed"

    results = search_incidents(question, top_k=5)

    print(f"Question: {question}")
    print(f"Matching incidents: {len(results)}")
    print("-" * 50)

    for result in results:
        incident = result["incident"]

        print(f"Incident: {incident['Incident ID']}")
        print(f"Machine: {incident['Machine']}")
        print(f"Cause: {incident['Cause']}")
        print(f"Root Cause: {incident['Root Cause']}")
        print(f"Action: {incident['Action Taken']}")
        print(f"Score: {result['score']}")
        print("-" * 50)


if __name__ == "__main__":
    main()
