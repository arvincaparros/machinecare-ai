/**
 * Thin HTTP layer for the MachineCare RAG API.
 *
 * The browser always calls same-origin paths ("/api/v1/ask", "/health");
 * Vite proxies them to the FastAPI process on port 8001 (see vite.config.ts).
 * UI components never build URLs or touch fetch directly.
 */

import type { AskRequest, AskResponse, HealthResponse } from "../types";

const ASK_TIMEOUT_MS = 90_000;

// Uploads are larger, so the document-aware call gets a longer budget.
const UPLOAD_TIMEOUT_MS = 120_000;
const HEALTH_TIMEOUT_MS = 5_000;

export type ApiErrorKind = "network" | "timeout" | "http" | "invalid-response";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;

  constructor(kind: ApiErrorKind, message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;

  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const forwardAbort = () => controller.abort();
  signal?.addEventListener("abort", forwardAbort);

  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if (timedOut) {
      throw new ApiError(
        "timeout",
        "The request timed out. The retrieval service may be busy; try again.",
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }

  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String((item as { msg: unknown }).msg);
        }
        return null;
      })
      .filter((value): value is string => Boolean(value));

    if (messages.length > 0) {
      return messages.join(" ");
    }
  }

  return null;
}

function toNetworkError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }

  return new ApiError(
    "network",
    "The RAG API could not be reached. Is it running on port 8001?",
  );
}

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
    response = await fetchWithTimeout(
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
    response = await fetchWithTimeout(
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
