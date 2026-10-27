
from app.services.groq_service import generate_response


def main():
    question = "What is machine downtime in manufacturing?"

    print("Sending question to Groq...")

    answer = generate_response(question)

    print("\nAI Response:")
    print(answer)


if __name__ == "__main__":
    main()
