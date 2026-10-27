
import os

from dotenv import load_dotenv
from groq import Groq

# Load environment variables from .env
load_dotenv()


def generate_response(question: str) -> str:
    """
    Send a question to Groq and return the AI response.
    """

    # Get the API key from environment variables
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        raise ValueError("GROQ_API_KEY is missing in .env")

    # Initialize Groq client
    client = Groq(api_key=api_key)

    # Send request to Groq
    response = client.chat.completions.create(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        messages=[
            {
                "role": "system",
                "content": (
                    "You are MachineCare AI, a manufacturing "
                    "machine downtime troubleshooting assistant."
                ),
            },
            {
                "role": "user",
                "content": question,
            },
        ],
        temperature=0.2,
    )

    # Return generated answer
    return response.choices[0].message.content or ""
