/**
 * Types mirroring backend/app/api/schemas.py and
 * backend/app/api/incident_import_schemas.py.
 *
 * Keep these in sync with the FastAPI models: AskResponse is returned by
 * POST /api/v1/ask, AskResponse with `documents` by
 * POST /api/v1/ask-with-files, and HealthResponse by GET /health. The
 * Import* and ReindexResponse types belong to the incident import routes
 * (see src/api/importClient.ts).
 */

export interface Source {
  incident_id: string;
  machine: string;
  distance: number;
}

/** One attachment the backend read for an answer. */
export interface DocumentInfo {
  filename: string;
  characters: number;
  truncated: boolean;
}

export interface AskResponse {
  answer: string;
  sources: Source[];
  cited_incident_ids: string[];
  unsupported_citations: string[];
  /** Present only on responses from the document-aware endpoint. */
  documents?: DocumentInfo[];
}

export interface HealthResponse {
  status: string;
}

export interface AskRequest {
  question: string;
  machine?: string | null;
}

/** Maximum question length enforced by the backend validator. */
export const MAX_QUESTION_LENGTH = 2000;

/** Maximum machine code length enforced by the backend validator. */
export const MAX_MACHINE_LENGTH = 50;

/** Server-side attachment limits, mirrored for client-side validation. */
export const MAX_ATTACHMENTS = 3;
export const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
export const ACCEPTED_EXTENSIONS = [
  ".pdf",
  ".txt",
  ".csv",
  ".docx",
  ".xlsx",
  ".xls",
] as const;

/** Supported formats as shown to the user. */
export const ACCEPTED_FORMATS_LABEL = "PDF, TXT, CSV, DOCX, XLSX or XLS";

/** One file selected in the composer, before upload. */
export interface AttachmentMeta {
  name: string;
  size: number;
}

export interface UserTurn {
  id: string;
  role: "user";
  content: string;
  machine: string | null;
  attachments: AttachmentMeta[];
}

export interface AssistantTurn {
  id: string;
  role: "assistant";
  state: "pending" | "done" | "error";
  question: string;
  machine: string | null;
  /** Kept so a failed upload can be retried without re-selecting files. */
  files: File[];
  response?: AskResponse;
  message?: string;
}

export type ChatTurn = UserTurn | AssistantTurn;

/* ------------------------------------------------------------------
   Incident import (backend/app/api/incident_import_schemas.py)
   ------------------------------------------------------------------ */

/** One field-level problem on one spreadsheet row. */
export interface ImportRowError {
  /** Spreadsheet row, 1-based, with the header on row 1. */
  row: number;
  field: string;
  code: string;
  message: string;
}

export type ImportRowState = "new" | "skipped_existing";

/** One row that passed every check. */
export interface ImportPreviewRow {
  row: number;
  incident_id: string;
  date: string;
  machine: string;
  production_line: string | null;
  downtime_minutes: number;
  state: ImportRowState;
}

/**
 * POST /api/v1/incidents/import/preview.
 *
 * Every data row is in exactly one of `invalid_rows`,
 * `skipped_existing_rows` and `eligible_rows`, and `valid_rows` always
 * equals `skipped_existing_rows + eligible_rows`.
 */
export interface ImportPreviewResponse {
  file_name: string;
  /** Sent back to the commit endpoint to prove it is the previewed file. */
  file_sha256: string;
  skip_existing: boolean;
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  skipped_existing_rows: number;
  eligible_rows: number;
  rows: ImportPreviewRow[];
  errors: ImportRowError[];
}

export type IndexingStatus = "indexed" | "failed" | "skipped";

/** Whether the search index reflects what was just committed. */
export interface ImportIndexingInfo {
  status: IndexingStatus;
  /**
   * Size of the whole vector-store collection after indexing, not the
   * number of rows this import added.
   */
  collection_total: number | null;
  message: string | null;
}

/** POST /api/v1/incidents/import — a completed import, split by stage. */
export interface ImportCommitResponse {
  file_name: string;
  file_sha256: string;
  skip_existing: boolean;
  imported_rows: number;
  imported_incident_ids: string[];
  skipped_existing_rows: number;
  database_committed: boolean;
  indexing: ImportIndexingInfo;
}

/** Body of a 409 or 422 refusal from the commit endpoint. */
export interface ImportRejectionResponse {
  detail: string;
  errors: ImportRowError[];
}

/** POST /api/v1/incidents/import/reindex. */
export interface ReindexResponse {
  status: "indexed" | "failed";
  collection_total: number | null;
  message: string;
}

/** Server-side import limits, mirrored for client-side validation. */
export const MAX_IMPORT_BYTES = 5 * 1024 * 1024;
export const MAX_IMPORT_ROWS = 5000;
export const IMPORT_ACCEPTED_EXTENSIONS = [".csv", ".xlsx"] as const;
export const IMPORT_ACCEPTED_FORMATS_LABEL = "CSV or XLSX";
