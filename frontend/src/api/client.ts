/**
 * Thin HTTP layer for the MachineCare RAG API.
 *
 * The browser always calls same-origin paths ("/api/v1/ask", "/health");
 * Vite proxies them to the FastAPI process on port 8001 (see vite.config.ts).
 * UI components never build URLs or touch fetch directly. Ask requests
 * carry the signed-in user's bearer token and, on a single 401, refresh
 * the session once and retry (see src/api/authClient.ts).
 */

import type { AskRequest, AskResponse, HealthResponse } from "../types";
import { fetchWithAuth } from "./authClient";
import {
  ApiError,
  describeDetail,
  fetchWithTimeout,
  toNetworkError,
} from "./http";

// Transport primitives live in ./http; these re-exports keep the rest of
// the app (useAsk, useIncidentImport, importClient) on one import site.
export {
  ApiError,
  describeDetail,
  fetchWithTimeout,
  toNetworkError,
} from "./http";
export type { ApiErrorKind } from "./http";

const ASK_TIMEOUT_MS = 90_000;

// Uploads are larger, so the document-aware call gets a longer budget.
const UPLOAD_TIMEOUT_MS = 120_000;
const HEALTH_TIMEOUT_MS = 5_000;

/** GET /health — liveness of the API process only. */
export async function checkHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const response = await fetchWithTimeout(
      "/health",
      { method: "GET", headers: { Accept: "application/json" } },
      HEALTH_TIMEOUT_MS,
      signal,
    );

    if (!response.ok) {
      return false;
    }

    const payload = (await response.json()) as HealthResponse;
    return payload.status === "ok";
  } catch {
    return false;
  }
}

function isAskResponse(value: unknown): value is AskResponse {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<AskResponse>;

  const documentsAreValid =
    candidate.documents === undefined ||
    (Array.isArray(candidate.documents) &&
      candidate.documents.every(
        (document) =>
          Boolean(document) &&
          typeof document.filename === "string" &&
          typeof document.characters === "number" &&
          typeof document.truncated === "boolean",
      ));

  return (
    typeof candidate.answer === "string" &&
    Array.isArray(candidate.sources) &&
    Array.isArray(candidate.cited_incident_ids) &&
    Array.isArray(candidate.unsupported_citations) &&
    documentsAreValid
  );
}

/** POST /api/v1/ask — grounded answer plus the records that supported it. */
export async function askQuestion(
  input: AskRequest,
  signal?: AbortSignal,
): Promise<AskResponse> {
  let response: Response;

  try {
    response = await fetchWithAuth(
      "/api/v1/ask",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          question: input.question,
          machine: input.machine ?? null,
        }),
      },
      ASK_TIMEOUT_MS,
      signal,
    );
  } catch (error) {
    throw toNetworkError(error);
  }

  return readAskResponse(response, { uploading: false });
}

/**
 * POST /api/v1/ask-with-files — the same answer, grounded in the
 * attached documents as well as the historical incident records.
 *
 * The files are sent as multipart form data: no path and no file
 * content ever travels as JSON.
 */
export async function askQuestionWithFiles(
  input: AskRequest,
  files: File[],
  signal?: AbortSignal,
): Promise<AskResponse> {
  if (files.length === 0) {
    return askQuestion(input, signal);
  }

  const body = new FormData();
  body.append("question", input.question);

  if (input.machine) {
    body.append("machine", input.machine);
  }

  for (const file of files) {
    body.append("files", file, file.name);
  }

  let response: Response;

  try {
    response = await fetchWithAuth(
      "/api/v1/ask-with-files",
      {
        method: "POST",
        // Content-Type is left unset so the browser adds the multipart
        // boundary; setting it by hand would break the upload.
        headers: { Accept: "application/json" },
        body,
      },
      UPLOAD_TIMEOUT_MS,
      signal,
    );
  } catch (error) {
    throw toNetworkError(error);
  }

  return readAskResponse(response, { uploading: true });
}

async function readAskResponse(
  response: Response,
  options: { uploading: boolean },
): Promise<AskResponse> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? describeDetail((payload as { detail: unknown }).detail)
        : null;

    throw new ApiError(
      "http",
      detail ?? fallbackMessage(response.status, options.uploading),
      response.status,
    );
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!isAskResponse(payload)) {
    throw new ApiError(
      "invalid-response",
      "The API returned an unexpected response shape.",
    );
  }

  return payload;
}

function fallbackMessage(status: number, uploading: boolean): string {
  if (status === 413) {
    return "One of the attached documents is larger than the 10 MB limit.";
  }

  if (status === 415) {
    return "One of the attached documents has an unsupported or invalid format.";
  }

  if (status === 400) {
    return "The attachments were rejected. Attach one to three documents.";
  }

  if (status === 422) {
    return uploading
      ? "The question or one of the documents was rejected by the API."
      : "The question was rejected by the API. Check the wording and length.";
  }

  return uploading
    ? "The API could not analyze the attached documents right now."
    : "The API could not answer this question right now.";
}
