"""Request and response schemas for the MachineCare ask API."""

from pydantic import BaseModel, Field, field_validator


MAX_QUESTION_LENGTH = 2000
MAX_MACHINE_LENGTH = 50


class HealthResponse(BaseModel):
    """Liveness payload: the API process is running."""

    status: str


class AskRequest(BaseModel):
    """A technician question and an optional machine filter."""

    question: str = Field(
        ...,
        max_length=MAX_QUESTION_LENGTH,
        description="Maintenance question about a downtime incident.",
    )
    machine: str | None = Field(
        default=None,
        max_length=MAX_MACHINE_LENGTH,
        description="Optional machine code, for example AOI-01.",
    )

    @field_validator("question")
    @classmethod
    def _question_must_not_be_blank(cls, value: str) -> str:
        question = value.strip()

        if not question:
            raise ValueError("question must not be blank.")

        return question

    @field_validator("machine")
    @classmethod
    def _blank_machine_means_no_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return value.strip() or None


class Source(BaseModel):
    """One historical incident that grounded the answer."""

    incident_id: str
    machine: str
    distance: float


class AskResponse(BaseModel):
    """The RAG service response, unchanged in meaning."""

    answer: str
    sources: list[Source]
    cited_incident_ids: list[str]
    unsupported_citations: list[str]


class DocumentInfo(BaseModel):
    """One attachment that was read for this answer."""

    filename: str
    characters: int = Field(
        ...,
        description="Characters of extracted text used for the prompt.",
    )
    truncated: bool = Field(
        ...,
        description="True when the extracted text was cut to its limit.",
    )


class AskWithFilesResponse(AskResponse):
    """The ask response plus the documents that grounded it."""

    documents: list[DocumentInfo]
