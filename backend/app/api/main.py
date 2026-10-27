"""FastAPI surface for the MachineCare RAG service.

This is a separate application from the phase-1 database API in
``app.main`` so exposing RAG does not change the existing incident
endpoints or their startup seeding. The RAG pipeline itself is never
reimplemented here: the endpoint calls ``ask_machinecare`` and returns
its result.

Run locally with:
    uvicorn app.api.main:app --reload
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)

from app.api.incident_import import router as incident_import_router
from app.api.schemas import (
    MAX_MACHINE_LENGTH,
    MAX_QUESTION_LENGTH,
    AskRequest,
    AskResponse,
    AskWithFilesResponse,
    DocumentInfo,
    HealthResponse,
)
from app.services.document_extraction import (
    MAX_FILES,
    MAX_FILE_BYTES,
    DocumentError,
    UploadedFile,
    extract_documents,
)


logger = logging.getLogger(__name__)

# Signature of the injected RAG callable.
AskService = Callable[..., dict]


def ask_machinecare_service(
    question: str,
    machine: str | None = None,
) -> dict:
    """Call the production RAG service.

    The import is deferred so that starting this process or answering
    ``/health`` does not open the ChromaDB collection or require the
    database settings that ``app.db.database`` reads at import time.
    """

    from app.services.rag_service import ask_machinecare

    return ask_machinecare(question=question, machine=machine)


def get_ask_service() -> AskService:
    """Return the RAG callable; overridden in tests."""

    return ask_machinecare_service


def ask_machinecare_with_documents_service(
    question: str,
    documents: list[dict],
    machine: str | None = None,
) -> dict:
    """Call the document-aware RAG service with a deferred import."""

    from app.services.rag_service import ask_machinecare_with_documents

    return ask_machinecare_with_documents(
        question=question,
        documents=documents,
        machine=machine,
    )


def get_document_ask_service() -> AskService:
    """Return the document-aware callable; overridden in tests."""

    return ask_machinecare_with_documents_service


app = FastAPI(
    title="MachineCare AI RAG API",
    version="0.1.0",
)

# Read-only incident import routes; the router keeps its own path prefix
# and imports the database lazily.
app.include_router(incident_import_router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Report that the process is running.

    No PostgreSQL, ChromaDB or Groq connectivity is checked here, so no
    dependency is claimed to be healthy.
    """

    return HealthResponse(status="ok")


@app.post("/api/v1/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    service: AskService = Depends(get_ask_service),
) -> AskResponse:
    """Answer a maintenance question through the existing RAG service."""

    try:
        result = service(
            question=request.question,
            machine=request.machine,
        )
        return AskResponse.model_validate(result)
    except Exception:
        # Logged server-side; clients only see a generic message.
        logger.exception("Ask request failed.")
        raise HTTPException(
            status_code=500,
            detail="Failed to answer the question.",
        ) from None


def _read_upload(upload: UploadFile) -> UploadedFile:
    """Read one upload into memory, refusing oversized files early."""

    filename = (upload.filename or "attachment").strip()
    chunks = []
    total = 0

    while True:
        chunk = upload.file.read(64 * 1024)

        if not chunk:
            break

        total += len(chunk)

        if total > MAX_FILE_BYTES:
            raise DocumentError(
                f"{filename} is larger than the "
                f"{MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
                413,
                "file_too_large",
            )

        chunks.append(chunk)

    return UploadedFile(
        filename=filename,
        content_type=upload.content_type or "",
        content=b"".join(chunks),
    )


@app.post(
    "/api/v1/ask-with-files",
    response_model=AskWithFilesResponse,
)
def ask_with_files(
    question: Annotated[str, Form(max_length=MAX_QUESTION_LENGTH)],
    machine: Annotated[
        str | None,
        Form(max_length=MAX_MACHINE_LENGTH),
    ] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    service: AskService = Depends(get_document_ask_service),
) -> AskWithFilesResponse:
    """Answer a question using uploaded documents and incident records.

    Files are read and processed in memory only: nothing is written to
    disk, PostgreSQL or ChromaDB.
    """

    uploads = files or []

    if not uploads:
        raise HTTPException(
            status_code=400,
            detail=(
                "Attach at least one document, or use "
                "POST /api/v1/ask for a question without attachments."
            ),
        )

    if len(uploads) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Attach at most {MAX_FILES} documents per question.",
        )

    cleaned_question = (question or "").strip()

    if not cleaned_question:
        raise HTTPException(
            status_code=422,
            detail="question must not be blank.",
        )

    cleaned_machine = (machine or "").strip() or None

    try:
        documents = extract_documents(
            [_read_upload(upload) for upload in uploads]
        )
    except DocumentError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.message,
        ) from None

    payload = [
        {
            "filename": document.filename,
            "text": document.text,
            "characters": document.characters,
            "truncated": document.truncated,
        }
        for document in documents
    ]

    try:
        result = service(
            question=cleaned_question,
            machine=cleaned_machine,
            documents=payload,
        )

        return AskWithFilesResponse.model_validate(
            {
                **result,
                "documents": [
                    DocumentInfo(
                        filename=document.filename,
                        characters=document.characters,
                        truncated=document.truncated,
                    ).model_dump()
                    for document in documents
                ],
            }
        )
    except Exception:
        # Logged server-side; document contents are never included.
        logger.exception("Ask-with-files request failed.")
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to answer the question with the attached "
                "documents."
            ),
        ) from None
