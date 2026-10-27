/**
 * HTTP layer for the incident import endpoints.
 *
 * Paths and payloads mirror backend/app/api/incident_import.py and
 * backend/app/api/incident_import_schemas.py:
 *
 *   GET  /api/v1/incidents/import/template.xlsx?format=xlsx|csv
 *   POST /api/v1/incidents/import/preview   multipart: file, skip_existing
 *   POST /api/v1/incidents/import           multipart: file, file_sha256, skip_existing
 *   POST /api/v1/incidents/import/reindex
 *
 * The browser calls these same-origin paths and Vite proxies them to port
 * 8001, exactly like the ask endpoints in client.ts. Nothing here retries a
 * request: the import can commit, so a caller must decide what a failure
 * means before sending it again.
 */

import {
  IMPORT_ACCEPTED_FORMATS_LABEL,
  type ImportCommitResponse,
  type ImportIndexingInfo,
  type ImportPreviewResponse,
  type ImportRowError,
  type ReindexResponse,
} from "../types";
import {
  ApiError,
  describeDetail,
  fetchWithTimeout,
  toNetworkError,
  type ApiErrorKind,
} from "./client";

const PREVIEW_TIMEOUT_MS = 60_000;
const COMMIT_TIMEOUT_MS = 120_000;
// Reindexing upserts every Closed incident, and the first run may also
// download the embedding model, so it gets the longest budget.
const REINDEX_TIMEOUT_MS = 300_000;
const TEMPLATE_TIMEOUT_MS = 30_000;

const PREVIEW_PATH = "/api/v1/incidents/import/preview";
const COMMIT_PATH = "/api/v1/incidents/import";
const REINDEX_PATH = "/api/v1/incidents/import/reindex";
const TEMPLATE_PATH = "/api/v1/incidents/import/template.xlsx";

const TEMPLATE_BASENAME = "machinecare-incident-import-template";
const UNEXPECTED_SHAPE = "The API returned an unexpected response shape.";
const UNREACHABLE = "The RAG API could not be reached. Is it running on port 8001?";

const PREVIEW_TIMEOUT_MESSAGE =
  "The preview request timed out. The API may be busy; try again.";

// Deliberately explicit: the request may have committed before the browser
// gave up, so the user has to look before sending the file again.
const COMMIT_TIMEOUT_MESSAGE =
  "The import request timed out before the API answered. The rows may " +
  "already have been committed, so check the incidents and the search " +
  "index before importing the file again.";

const REINDEX_TIMEOUT_MESSAGE =
  "The reindex request timed out. Reindexing is idempotent, so it is safe " +
  "to try again.";

const TEMPLATE_TIMEOUT_MESSAGE = "The template download timed out. Try again.";

export type ImportTemplateFormat = "xlsx" | "csv";

/**
 * A failed import request.
 *
 * `rowErrors` holds the backend's row-level reasons for a 409 or 422
 * refusal; it stays empty for file-level problems such as 413, 415 or 500.
 */
export class ImportRequestError extends ApiError {
  readonly rowErrors: ImportRowError[];

  constructor(
    kind: ApiErrorKind,
    message: string,
    status?: number,
    rowErrors: ImportRowError[] = [],
  ) {
    super(kind, message, status);
    this.name = "ImportRequestError";
    this.rowErrors = rowErrors;
  }
}

async function request(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  timeoutMessage: string,
  signal?: AbortSignal,
): Promise<Response> {
  try {
    return await fetchWithTimeout(url, init, timeoutMs, signal);
  } catch (error) {
    // No response headers arrived: either the timeout fired or the request
    // never reached the API. fetchWithTimeout's message is retrieval-specific,
    // so a timeout is re-worded for the import flow.
    const failure = toNetworkError(error);

    if (failure.kind === "timeout") {
      throw new ImportRequestError("timeout", timeoutMessage);
    }

    throw new ImportRequestError("network", UNREACHABLE);
  }
}

async function readFailure(
  response: Response,
  fallback: string,
): Promise<ImportRequestError> {
  const payload: unknown = await response.json().catch(() => null);

  return new ImportRequestError(
    "http",
    readDetail(payload) ?? fallback,
    response.status,
    readRowErrors(payload),
  );
}

function readDetail(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return null;
  }

  return describeDetail((payload as { detail: unknown }).detail);
}

/** Row-level reasons from a `{ detail, errors }` refusal body. */
function readRowErrors(payload: unknown): ImportRowError[] {
  if (!payload || typeof payload !== "object" || !("errors" in payload)) {
    return [];
  }

  const candidate = (payload as { errors: unknown }).errors;
  return Array.isArray(candidate) ? candidate.filter(isRowError) : [];
}

function isRowError(value: unknown): value is ImportRowError {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<ImportRowError>;

  return (
    isCount(candidate.row) &&
    typeof candidate.field === "string" &&
    typeof candidate.code === "string" &&
    typeof candidate.message === "string"
  );
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isPreviewResponse(value: unknown): value is ImportPreviewResponse {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<ImportPreviewResponse>;

  return (
    typeof candidate.file_name === "string" &&
    typeof candidate.file_sha256 === "string" &&
    typeof candidate.skip_existing === "boolean" &&
    isCount(candidate.total_rows) &&
    isCount(candidate.valid_rows) &&
    isCount(candidate.invalid_rows) &&
    isCount(candidate.skipped_existing_rows) &&
    isCount(candidate.eligible_rows) &&
    Array.isArray(candidate.rows) &&
    Array.isArray(candidate.errors) &&
    candidate.errors.every(isRowError)
  );
}

function isIndexingInfo(value: unknown): value is ImportIndexingInfo {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<ImportIndexingInfo>;

  return (
    (candidate.status === "indexed" ||
      candidate.status === "failed" ||
      candidate.status === "skipped") &&
    (candidate.collection_total === null ||
      isCount(candidate.collection_total)) &&
    (candidate.message === null || typeof candidate.message === "string")
  );
}

function isCommitResponse(value: unknown): value is ImportCommitResponse {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<ImportCommitResponse>;

  return (
    typeof candidate.file_name === "string" &&
    typeof candidate.file_sha256 === "string" &&
    typeof candidate.skip_existing === "boolean" &&
    isCount(candidate.imported_rows) &&
    Array.isArray(candidate.imported_incident_ids) &&
    candidate.imported_incident_ids.every(
      (incidentId) => typeof incidentId === "string",
    ) &&
    isCount(candidate.skipped_existing_rows) &&
    typeof candidate.database_committed === "boolean" &&
    isIndexingInfo(candidate.indexing)
  );
}

function isReindexResponse(value: unknown): value is ReindexResponse {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<ReindexResponse>;

  return (
    (candidate.status === "indexed" || candidate.status === "failed") &&
    (candidate.collection_total === null ||
      isCount(candidate.collection_total)) &&
    typeof candidate.message === "string"
  );
}

function previewFallback(status: number): string {
  if (status === 413) {
    return "The file is larger than the 5 MB import limit.";
  }

  if (status === 415) {
    return `The file type is not supported. Choose a ${IMPORT_ACCEPTED_FORMATS_LABEL} file.`;
  }

  if (status === 400) {
    return "The file could not be read. Start from the import template.";
  }

  if (status === 422) {
    return "The file does not match the import template, so no row could be read.";
  }

  if (status === 500) {
    return "The file could not be checked because the incident database could not be read.";
  }

  return "The file could not be checked right now.";
}

function commitFallback(status: number): string {
  if (status === 409) {
    return "The file or the database changed since the preview. Nothing was written; preview the file again.";
  }

  if (status === 422) {
    return "The file has rows that cannot be imported. Nothing was written.";
  }

  if (status === 413) {
    return "The file is larger than the 5 MB import limit.";
  }

  if (status === 500) {
    return "The import failed. No row was written.";
  }

  return "The import request failed.";
}

/**
 * POST /api/v1/incidents/import/preview — check a file, read-only.
 *
 * A 200 response can still report invalid rows; those are the answer, not a
 * failure. File-level problems keep the backend's 4xx status.
 */
export async function previewIncidentImport(
  file: File,
  skipExisting: boolean,
  signal?: AbortSignal,
): Promise<ImportPreviewResponse> {
  const body = new FormData();
  body.append("file", file, file.name);
  body.append("skip_existing", skipExisting ? "true" : "false");

  const response = await request(
    PREVIEW_PATH,
    {
      method: "POST",
      // Content-Type is left unset so the browser adds the multipart boundary.
      headers: { Accept: "application/json" },
      body,
    },
    PREVIEW_TIMEOUT_MS,
    PREVIEW_TIMEOUT_MESSAGE,
    signal,
  );

  if (!response.ok) {
    throw await readFailure(response, previewFallback(response.status));
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!isPreviewResponse(payload)) {
    throw new ImportRequestError("invalid-response", UNEXPECTED_SHAPE);
  }

  return payload;
}

export interface ImportCommitInput {
  file: File;
  /** The digest the preview returned, proving this is the previewed file. */
  fileSha256: string;
  skipExisting: boolean;
}

/**
 * POST /api/v1/incidents/import — commit one previewed file.
 *
 * The original File object is sent again, so the bytes the digest was
 * computed from are the bytes the API hashes and inserts.
 */
export async function commitIncidentImport(
  input: ImportCommitInput,
  signal?: AbortSignal,
): Promise<ImportCommitResponse> {
  const body = new FormData();
  body.append("file", input.file, input.file.name);
  body.append("file_sha256", input.fileSha256);
  body.append("skip_existing", input.skipExisting ? "true" : "false");

  const response = await request(
    COMMIT_PATH,
    {
      method: "POST",
      headers: { Accept: "application/json" },
      body,
    },
    COMMIT_TIMEOUT_MS,
    COMMIT_TIMEOUT_MESSAGE,
    signal,
  );

  if (!response.ok) {
    throw await readFailure(response, commitFallback(response.status));
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!isCommitResponse(payload)) {
    // The status was 200, so the transaction may have committed even though
    // the body was unusable; the caller has to treat this as unknown.
    throw new ImportRequestError(
      "invalid-response",
      UNEXPECTED_SHAPE,
      response.status,
    );
  }

  return payload;
}

/**
 * POST /api/v1/incidents/import/reindex — rebuild the search index.
 *
 * A failure is documented as 503 with a ReindexResponse body, so its message
 * is used as the error text.
 */
export async function reindexIncidents(
  signal?: AbortSignal,
): Promise<ReindexResponse> {
  const response = await request(
    REINDEX_PATH,
    { method: "POST", headers: { Accept: "application/json" } },
    REINDEX_TIMEOUT_MS,
    REINDEX_TIMEOUT_MESSAGE,
    signal,
  );

  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    throw new ImportRequestError(
      "http",
      isReindexResponse(payload)
        ? payload.message
        : "The search index could not be refreshed.",
      response.status,
    );
  }

  if (!isReindexResponse(payload)) {
    throw new ImportRequestError("invalid-response", UNEXPECTED_SHAPE);
  }

  return payload;
}

export interface ImportTemplateFile {
  blob: Blob;
  filename: string;
}

/** GET /api/v1/incidents/import/template.xlsx — the blank import file. */
export async function downloadImportTemplate(
  format: ImportTemplateFormat,
  signal?: AbortSignal,
): Promise<ImportTemplateFile> {
  const response = await request(
    `${TEMPLATE_PATH}?format=${format}`,
    { method: "GET", headers: { Accept: "*/*" } },
    TEMPLATE_TIMEOUT_MS,
    TEMPLATE_TIMEOUT_MESSAGE,
    signal,
  );

  if (!response.ok) {
    throw await readFailure(
      response,
      `The ${format.toUpperCase()} template could not be downloaded.`,
    );
  }

  return {
    blob: await response.blob(),
    filename:
      filenameFromDisposition(response.headers.get("Content-Disposition")) ??
      `${TEMPLATE_BASENAME}.${format}`,
  };
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) {
    return null;
  }

  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
  const name = match?.[1]?.split(/[\\/]/).pop()?.trim();

  return name ? name : null;
}
