/**
 * Types mirroring backend/app/api/schemas.py.
 *
 * Keep these in sync with the FastAPI models: AskResponse is returned by
 * POST /api/v1/ask, AskResponse with `documents` by
 * POST /api/v1/ask-with-files, and HealthResponse by GET /health.
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
