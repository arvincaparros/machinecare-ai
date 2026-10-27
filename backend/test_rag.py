
from app.services.rag_service import ask_machinecare


def main():
    question = "AOI camera inspection is inaccurate"

    print(f"Question: {question}")
    print("-" * 60)

    result = ask_machinecare(
        question=question,
        machine="AOI-01"
    )

    print("\nMACHINECARE AI RESPONSE:")
    print(result["answer"])

    print("\nRETRIEVED SOURCES:")

    for source in result["sources"]:
        print(
            f"{source['incident_id']} | "
            f"{source['machine']} | "
            f"Distance: {source['distance']}"
        )


if __name__ == "__main__":
    main()
