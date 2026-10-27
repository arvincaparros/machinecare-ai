
from chromadb.utils.embedding_functions import (
    DefaultEmbeddingFunction
)


def main():
    print("Initializing embedding model...")

    embedding_function = DefaultEmbeddingFunction()

    documents = [
        "AOI camera calibration failed",
        "Camera drift after extended operation",
        "Conveyor belt slipped during production"
    ]

    print("Generating embeddings...")

    embeddings = embedding_function(documents)

    print("Documents:", len(documents))
    print("Embeddings generated:", len(embeddings))
    print("Vector dimensions:", len(embeddings[0]))

    print("\nEmbedding test successful!")


if __name__ == "__main__":
    main()
