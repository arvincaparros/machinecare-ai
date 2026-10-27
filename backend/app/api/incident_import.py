"""Endpoints for the incident import.

Four read-only routes prepare and check an import:

- ``GET .../template.xlsx`` returns the Step 1 template, as XLSX or,
  with ``?format=csv``, as CSV.
- ``GET .../machines`` lists the machine codes a row may reference.
- ``POST .../preview`` parses and validates an upload and compares it
  with PostgreSQL without writing anything.
- ``POST .../reindex`` refreshes the search index from PostgreSQL on
  request, so an import whose indexing failed can be retried.

Two routes change data:

- ``POST ...`` imports a previewed file. It re-checks the file against
  the database as it is now, refuses every file with a row-level
  problem, and inserts the eligible incidents and their resolutions in
  one transaction. Indexing runs after the commit and never decides
  whether the rows are kept.
- The rows are inserted with the SQLAlchemy models and resolved by exact
  machine code; no machine is ever created and no existing incident,
  resolution or machine is updated or deleted.

``app.db.database`` reads its PostgreSQL settings at import time and
``app.services.vector_store_service`` opens the persistent collection at
import time. ``app.services.incident_import_commit`` imports the models
and the repository, and therefore the database module as well. All three
are imported inside the functions below, so starting this process or
answering ``/health`` still needs neither database settings nor a vector
store.

Local development only: there is no authentication or authorisation yet,
so the write endpoints are unauthenticated and must not be exposed as
they are, with or without a network in front of them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.incident_import_schemas import (
    ImportCommitResponse,
    ImportIndexingInfo,
    ImportPreviewResponse,
    ImportPreviewRowInfo,
    ImportRejectionResponse,
    ImportRowErrorInfo,
    MachineInfo,
    MachineListResponse,
    ReindexResponse,
)
from app.services.incident_import_digest import (
    digest_matches,
    sha256_hex,
)
from app.services.incident_import_preview import (
    IncidentImportPreview,
    build_import_preview,
    collect_lookup_values,
)
from app.services.incident_import_service import (
    MAX_IMPORT_BYTES,
    ImportRowError,
    IncidentImportError,
    IncidentImportResult,
    parse_incident_import,
)
from app.services.incident_import_template import (
    build_csv_template,
    build_xlsx_template,
)


if TYPE_CHECKING:
    from app.services.incident_import_commit import ImportCommitOutcome


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/incidents/import",
    tags=["incident-import"],
)

TEMPLATE_BASENAME = "machinecare-incident-import-template"

XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

PREVIEW_FAILED_DETAIL = (
    "Failed to check the import file against the incident database."
)
MACHINES_FAILED_DETAIL = "Failed to load the machine codes."

HASH_MISMATCH_DETAIL = (
    "The upload does not match the file that was previewed. Preview it "
    "again and send the file_sha256 from that response."
)
INVALID_ROWS_DETAIL = (
    "The file has rows that cannot be imported. Nothing was written."
)
STALE_DATA_DETAIL = (
    "The database changed after the preview. Nothing was written; "
    "preview the file again before importing it."
)
IMPORT_FAILED_DETAIL = "Failed to import the file. No row was written."
INDEX_FAILED_DETAIL = (
    "The rows were imported and committed, but indexing them for search "
    "failed. The stored incidents are unchanged; retry the reindex "
    "endpoint once the vector store is available."
)
INDEX_COMPLETED_DETAIL = (
    "The search index was refreshed from PostgreSQL. collection_total "
    "is the collection's total document count, not the number of rows "
    "this import added."
)
NOTHING_TO_INDEX_DETAIL = (
    "No row was inserted, so there was nothing to index."
)
REINDEX_COMPLETED_DETAIL = (
    "Every Closed incident was re-indexed from PostgreSQL. "
    "collection_total is the collection's total document count."
)
REINDEX_FAILED_DETAIL = (
    "Failed to refresh the search index. No stored incident was "
    "changed; retry once the vector store is available."
)


def get_index_service() -> Callable[[], int]:
    """Return the indexing callable that the import runs after commit."""

    return index_committed_incidents


def index_committed_incidents() -> int:
    """Refresh the search index from the committed incidents.

    The existing service upserts every Closed incident by its business
    ID and never deletes, which is what makes a retry safe. Its return
    value is the collection's total document count, not the number of
    rows it touched, so it is reported as ``collection_total``.
    """

    from app.services.vector_store_service import index_incidents

    return index_incidents()


def get_db_session() -> Iterator[Session]:
    """Yield a session from the RAG database provider.

    The import is deferred: ``app.db.database`` reads the PostgreSQL
    settings as soon as it is imported, and the ask routes must keep
    working without them.
    """

    from app.db.database import get_db

    yield from get_db()


def _database_machines(db: Session):
    """Read every machine through the repository."""

    from app.repositories import incident_repository

    return incident_repository.get_machines(db)


def _database_import_facts(
    db: Session,
    machine_codes: tuple[str, ...],
    incident_ids: tuple[str, ...],
) -> tuple[set[str], set[str]]:
    """Read machine-code and incident-ID existence, in two SELECTs."""

    from app.repositories import incident_repository

    known = incident_repository.find_known_machine_codes(
        db,
        machine_codes,
    )
    existing = incident_repository.find_existing_incident_ids(
        db,
        incident_ids,
    )

    return known, existing


@router.get("/template.xlsx")
def download_import_template(
    template_format: Annotated[str, Query(alias="format")] = "xlsx",
) -> Response:
    """Download the import template as XLSX or CSV."""

    wanted = template_format.strip().lower()

    if wanted == "xlsx":
        content = build_xlsx_template()
        media_type = XLSX_MEDIA_TYPE
        extension = "xlsx"
    elif wanted == "csv":
        content = build_csv_template()
        media_type = CSV_MEDIA_TYPE
        extension = "csv"
    else:
        raise HTTPException(
            status_code=422,
            detail="format must be xlsx or csv.",
        )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{TEMPLATE_BASENAME}.{extension}"'
            )
        },
    )


@router.get("/machines", response_model=MachineListResponse)
def list_machines(
    db: Session = Depends(get_db_session),
) -> MachineListResponse:
    """List the machine codes an import row may reference."""

    try:
        machines = _database_machines(db)
    except Exception:
        # Logged server-side; no SQL, host or credential reaches the
        # client.
        logger.exception("Machine lookup for the import failed.")
        raise HTTPException(
            status_code=500,
            detail=MACHINES_FAILED_DETAIL,
        ) from None

    return MachineListResponse(
        machines=[
            MachineInfo(
                code=machine.machine_code,
                name=machine.name,
                production_line=machine.production_line,
            )
            for machine in machines
        ]
    )


def _read_import_upload(upload: UploadFile) -> tuple[str, bytes]:
    """Read one upload into memory, refusing oversized files early."""

    filename = (upload.filename or "import").strip()
    chunks = []
    total = 0

    while True:
        chunk = upload.file.read(64 * 1024)

        if not chunk:
            break

        total += len(chunk)

        if total > MAX_IMPORT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{filename} is larger than the "
                    f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB import limit."
                ),
            )

        chunks.append(chunk)

    return filename, b"".join(chunks)


def _parse_upload(
    filename: str,
    content: bytes,
) -> IncidentImportResult:
    """Parse an upload, keeping the parser's own 4xx status."""

    try:
        return parse_incident_import(filename, content)
    except IncidentImportError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.message,
        ) from None


def _preview_against_database(
    db: Session,
    parsed: IncidentImportResult,
    skip_existing: bool,
) -> IncidentImportPreview:
    """Classify a parsed file against the database as it is now."""

    machine_codes, incident_ids = collect_lookup_values(parsed)

    try:
        known, existing = _database_import_facts(
            db,
            machine_codes,
            incident_ids,
        )
    except Exception:
        logger.exception("Import preview database lookup failed.")
        raise HTTPException(
            status_code=500,
            detail=PREVIEW_FAILED_DETAIL,
        ) from None

    return build_import_preview(
        parsed,
        known_machine_codes=known,
        existing_incident_ids=existing,
        skip_existing=skip_existing,
    )


@router.post("/preview", response_model=ImportPreviewResponse)
def preview_import(
    file: Annotated[
        UploadFile,
        File(description="CSV or XLSX file to check."),
    ],
    skip_existing: Annotated[bool, Form()] = False,
    db: Session = Depends(get_db_session),
) -> ImportPreviewResponse:
    """Check an upload against the schema and the database, read-only.

    A file that parses is previewed with 200 even when rows are invalid:
    the row-level errors are the answer. File-level problems (unsupported
    format, bad headers, too large) keep the parser's 4xx status.

    The response's ``file_sha256`` is what a later import has to send
    back, so the commit can prove it is importing this exact file.
    """

    filename, content = _read_import_upload(file)
    parsed = _parse_upload(filename, content)
    preview = _preview_against_database(db, parsed, skip_existing)

    return _preview_response(preview, sha256_hex(content))


def _preview_response(
    preview: IncidentImportPreview,
    file_sha256: str,
) -> ImportPreviewResponse:
    """Convert the preview into its response schema."""

    return ImportPreviewResponse(
        file_name=preview.file_name,
        file_sha256=file_sha256,
        skip_existing=preview.skip_existing,
        total_rows=preview.total_rows,
        valid_rows=preview.valid_row_count,
        invalid_rows=preview.invalid_row_count,
        skipped_existing_rows=preview.skipped_row_count,
        eligible_rows=preview.eligible_row_count,
        rows=[
            ImportPreviewRowInfo(
                row=item.row.row_number,
                incident_id=item.row.incident_id,
                date=item.row.date,
                machine=item.row.machine,
                production_line=item.row.production_line,
                downtime_minutes=item.row.downtime_minutes,
                state=item.state,
            )
            for item in preview.rows
        ],
        errors=[
            ImportRowErrorInfo(
                row=error.row,
                field=error.field,
                code=error.code,
                message=error.message,
            )
            for error in preview.errors
        ],
    )


def _rejection(
    status_code: int,
    detail: str,
    errors: Sequence[ImportRowError],
) -> JSONResponse:
    """Refuse a file and name the rows at fault."""

    body = ImportRejectionResponse(
        detail=detail,
        errors=[
            ImportRowErrorInfo(
                row=error.row,
                field=error.field,
                code=error.code,
                message=error.message,
            )
            for error in errors
        ],
    )

    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )


def _index_after_commit(
    indexer: Callable[[], int],
    outcome: ImportCommitOutcome,
) -> ImportIndexingInfo:
    """Index the committed rows, reporting a failure without undoing it.

    Indexing is started only after the database transaction committed, so
    the rows it reads are the rows this import wrote. A failure here is
    reported as an indexing failure and never as a failed import.
    """

    if outcome.imported_row_count == 0:
        return ImportIndexingInfo(
            status="skipped",
            message=NOTHING_TO_INDEX_DETAIL,
        )

    try:
        collection_total = indexer()
    except Exception:
        logger.exception(
            "Indexing after the incident import failed; the imported "
            "rows stay committed."
        )
        return ImportIndexingInfo(
            status="failed",
            message=INDEX_FAILED_DETAIL,
        )

    return ImportIndexingInfo(
        status="indexed",
        collection_total=collection_total,
        message=INDEX_COMPLETED_DETAIL,
    )


@router.post(
    "",
    response_model=ImportCommitResponse,
    responses={
        409: {"model": ImportRejectionResponse},
        422: {"model": ImportRejectionResponse},
    },
)
def import_incidents(
    file: Annotated[
        UploadFile,
        File(description="CSV or XLSX file to import."),
    ],
    file_sha256: Annotated[str | None, Form()] = None,
    skip_existing: Annotated[bool, Form()] = False,
    db: Session = Depends(get_db_session),
    indexer: Callable[[], int] = Depends(get_index_service),
) -> ImportCommitResponse | Response:
    """Import a previewed file in one transaction, then index it.

    ``file_sha256`` must be the digest the preview returned, so a file
    that changed after the preview is refused with 409 instead of being
    imported. The file is parsed and checked again against the database
    as it is now: any row-level problem refuses the whole file and
    writes nothing, and an existing incident ID is only ever skipped,
    never updated.

    Every eligible incident is inserted with its resolution in a single
    transaction. The response reports the commit and the indexing
    separately, because a failed indexing leaves the rows imported.
    """

    from app.services.incident_import_commit import (
        IncidentImportInsertFailed,
        IncidentImportStaleData,
        commit_incident_import,
        stale_data_errors,
    )

    filename, content = _read_import_upload(file)

    if not digest_matches(file_sha256, content):
        raise HTTPException(
            status_code=409,
            detail=HASH_MISMATCH_DETAIL,
        )

    parsed = _parse_upload(filename, content)
    preview = _preview_against_database(db, parsed, skip_existing)

    if preview.errors:
        return _rejection(422, INVALID_ROWS_DETAIL, preview.errors)

    eligible = [item.row for item in preview.eligible_rows]

    try:
        outcome = commit_incident_import(
            db,
            eligible,
            skip_existing=skip_existing,
        )
    except IncidentImportStaleData as stale:
        return _rejection(
            409,
            STALE_DATA_DETAIL,
            stale_data_errors(stale, eligible),
        )
    except IncidentImportInsertFailed:
        logger.exception("Incident import insert failed.")
        raise HTTPException(
            status_code=500,
            detail=IMPORT_FAILED_DETAIL,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Incident import failed unexpectedly.")
        raise HTTPException(
            status_code=500,
            detail=IMPORT_FAILED_DETAIL,
        ) from None

    return ImportCommitResponse(
        file_name=filename,
        file_sha256=sha256_hex(content),
        skip_existing=skip_existing,
        imported_rows=outcome.imported_row_count,
        imported_incident_ids=list(outcome.imported_incident_ids),
        skipped_existing_rows=(
            preview.skipped_row_count
            + outcome.skipped_existing_rows
        ),
        database_committed=True,
        indexing=_index_after_commit(indexer, outcome),
    )


@router.post(
    "/reindex",
    response_model=ReindexResponse,
    responses={503: {"model": ReindexResponse}},
)
def reindex_incidents(
    indexer: Callable[[], int] = Depends(get_index_service),
) -> ReindexResponse | Response:
    """Rebuild the search index from the committed incidents.

    The retry for an import whose indexing failed. It reads PostgreSQL
    and upserts into the vector store: no incident, resolution or machine
    is inserted, updated or deleted, and the collection is never cleared.

    The existing indexing service re-indexes every Closed incident rather
    than a chosen subset. That is idempotent, needs no write to
    PostgreSQL and covers the rows a failed import committed, so it is
    reused as it is.
    """

    try:
        collection_total = indexer()
    except Exception:
        logger.exception("Incident reindexing failed.")
        return JSONResponse(
            status_code=503,
            content=ReindexResponse(
                status="failed",
                message=REINDEX_FAILED_DETAIL,
            ).model_dump(mode="json"),
        )

    return ReindexResponse(
        status="indexed",
        collection_total=collection_total,
        message=REINDEX_COMPLETED_DETAIL,
    )
