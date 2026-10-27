"""Response schemas for the incident import endpoints.

Count semantics for :class:`ImportPreviewResponse`. Every non-blank data
row below the header appears in exactly one of ``invalid_rows``,
``skipped_existing_rows`` and ``eligible_rows``, so::

    total_rows == invalid_rows + skipped_existing_rows + eligible_rows

- ``total_rows``: non-blank data rows in the file. The header row and
  blank rows are not counted.
- ``valid_rows``: rows that passed both the file parser and the database
  checks. It always equals ``skipped_existing_rows + eligible_rows``.
- ``invalid_rows``: rows with at least one row-level error, whatever the
  stage that produced it.
- ``skipped_existing_rows``: rows whose incident ID already exists and
  which ``skip_existing=true`` left out of the import. They are never
  updated or overwritten.
- ``eligible_rows``: rows a later commit step would insert.

``rows`` lists only the rows that passed every check, each flagged
``new`` or ``skipped_existing``; a rejected row appears only in
``errors``. Nothing here exposes a database primary key.

:class:`ImportCommitResponse` reports a completed import. It keeps the
database outcome and the indexing outcome in separate fields, because
the rows stay committed when indexing fails afterwards.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


ImportRowState = Literal["new", "skipped_existing"]

IndexingStatus = Literal["indexed", "failed", "skipped"]


class ImportRowErrorInfo(BaseModel):
    """One field-level problem on one spreadsheet row."""

    row: int = Field(
        ...,
        description=(
            "Spreadsheet row number, 1-based, with the header on row 1."
        ),
    )
    field: str = Field(
        ...,
        description=(
            "Column name, or 'column N' when a value has no header."
        ),
    )
    code: str = Field(
        ...,
        description=(
            "Stable error code, for example required, invalid_date, "
            "unknown_machine or existing_incident_id."
        ),
    )
    message: str = Field(
        ...,
        description="Readable reason a maintenance engineer can act on.",
    )


class ImportPreviewRowInfo(BaseModel):
    """One row that passed every check."""

    row: int = Field(
        ...,
        description="Spreadsheet row number, 1-based.",
    )
    incident_id: str
    date: date
    machine: str = Field(
        ...,
        description="Machine code exactly as supplied in the file.",
    )
    production_line: str | None = Field(
        default=None,
        description=(
            "The incident's own production line from the file. It is "
            "never derived from the machine record."
        ),
    )
    downtime_minutes: int
    state: ImportRowState = Field(
        ...,
        description=(
            "'new' rows are eligible to import. 'skipped_existing' rows "
            "already exist and would be left untouched."
        ),
    )


class ImportPreviewResponse(BaseModel):
    """What a preview found, without changing any stored data."""

    file_name: str
    file_sha256: str = Field(
        ...,
        description=(
            "SHA-256 of the previewed bytes. Send it back to the import "
            "endpoint as file_sha256 so the commit can prove it is "
            "importing the file that was previewed."
        ),
    )
    skip_existing: bool = Field(
        ...,
        description="The option the preview was run with.",
    )
    total_rows: int = Field(
        ...,
        description=(
            "Non-blank data rows below the header, excluding blank rows."
        ),
    )
    valid_rows: int = Field(
        ...,
        description=(
            "Rows that passed both the file parser and the database "
            "checks; equals skipped_existing_rows + eligible_rows."
        ),
    )
    invalid_rows: int = Field(
        ...,
        description="Rows with at least one row-level error.",
    )
    skipped_existing_rows: int = Field(
        ...,
        description=(
            "Rows whose incident ID already exists and were skipped "
            "because skip_existing was true. Nothing is updated."
        ),
    )
    eligible_rows: int = Field(
        ...,
        description=(
            "Rows a later commit step would insert; always "
            "total_rows - invalid_rows - skipped_existing_rows."
        ),
    )
    rows: list[ImportPreviewRowInfo] = Field(
        ...,
        description=(
            "Only rows that passed every check, each flagged 'new' or "
            "'skipped_existing'. Rejected rows appear in errors only."
        ),
    )
    errors: list[ImportRowErrorInfo] = Field(
        ...,
        description=(
            "Every row-level problem, ordered by spreadsheet row."
        ),
    )


class MachineInfo(BaseModel):
    """One machine an import row may reference."""

    code: str = Field(..., description="Machine code, for example AOI-01.")
    name: str
    production_line: str | None = Field(
        default=None,
        description=(
            "Informational only: an import keeps the production line "
            "written in the file."
        ),
    )


class MachineListResponse(BaseModel):
    """The machine codes available to the import UI."""

    machines: list[MachineInfo]


class ImportIndexingInfo(BaseModel):
    """Whether the search index reflects what was just committed."""

    status: IndexingStatus = Field(
        ...,
        description=(
            "'indexed' when the search index was refreshed after the "
            "commit, 'failed' when that refresh failed (the committed "
            "rows are unaffected), 'skipped' when there was nothing to "
            "index because no row was inserted."
        ),
    )
    collection_total: int | None = Field(
        default=None,
        description=(
            "Total number of documents in the vector store collection "
            "after indexing. This is the collection's size, not the "
            "number of rows indexed by this request."
        ),
    )
    message: str | None = Field(
        default=None,
        description=(
            "Readable outcome. On failure it explains that the rows "
            "were imported but not indexed, and never includes an "
            "internal exception message."
        ),
    )


class ImportRejectionResponse(BaseModel):
    """A file-level refusal that also names the rows at fault.

    ``detail`` is the same human-readable string every other refusal
    uses, so a client can keep reading ``detail``. ``errors`` carries
    the row-level reasons, which is empty only when no single row is to
    blame.
    """

    detail: str
    errors: list[ImportRowErrorInfo] = Field(
        default_factory=list,
        description="Row-level reasons, ordered by spreadsheet row.",
    )


class ImportCommitResponse(BaseModel):
    """The result of one import, split by stage.

    On a 200 response every row of the file was either imported or
    skipped, so::

        imported_rows + skipped_existing_rows == the file's data rows

    ``database_committed`` reports the PostgreSQL transaction alone.
    ``indexing`` is reported separately because indexing runs after the
    commit: a failure there leaves the rows imported.
    """

    file_name: str
    file_sha256: str = Field(
        ...,
        description="SHA-256 of the imported bytes.",
    )
    skip_existing: bool = Field(
        ...,
        description="The option the import ran with.",
    )
    imported_rows: int = Field(
        ...,
        description=(
            "Rows inserted, each with its resolution, in this "
            "transaction."
        ),
    )
    imported_incident_ids: list[str] = Field(
        ...,
        description="Business incident IDs of the inserted rows.",
    )
    skipped_existing_rows: int = Field(
        ...,
        description=(
            "Rows whose incident ID already existed and were left "
            "untouched. Counts the rows skipped by the preview and any "
            "row that appeared in the database before the commit."
        ),
    )
    database_committed: bool = Field(
        ...,
        description=(
            "True when the import's database stage finished: either "
            "the transaction committed or no row needed inserting. A "
            "failed import never returns this body."
        ),
    )
    indexing: ImportIndexingInfo


class ReindexResponse(BaseModel):
    """The result of refreshing the search index from PostgreSQL."""

    status: Literal["indexed", "failed"] = Field(
        ...,
        description=("Whether the refresh completed."),
    )
    collection_total: int | None = Field(
        default=None,
        description=(
            "Total number of documents in the vector store collection "
            "after the refresh. It is the collection's size, not the "
            "number of rows re-indexed."
        ),
    )
    message: str = Field(
        ...,
        description=(
            "Readable outcome; on failure, a safe message without an "
            "internal exception."
        ),
    )
