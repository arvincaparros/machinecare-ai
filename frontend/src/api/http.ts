/**
 * Transport primitives shared by every API client module.
 *
 * Every request in the app goes through fetchWithTimeout, so it gets the
 * same timeout, abort and error-mapping behaviour; ApiError is the one
 * failure type the UI layers know about. The auth session lives one
 * layer up in src/api/authClient.ts, which builds on these primitives.
 */

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

export async function fetchWithTimeout(
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

export function describeDetail(detail: unknown): string | null {
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

export function toNetworkError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }

  return new ApiError(
    "network",
    "The RAG API could not be reached. Is it running on port 8001?",
  );
}
