/**
 * Authentication API and the browser-side session around it.
 *
 * The access token is held in memory only — never in localStorage — so
 * nothing session-bearing survives a reload except the HttpOnly refresh
 * cookie, which the server rotates on every refresh. When the server
 * finally rejects that cookie (expired, revoked, or replayed), the
 * session-ended listeners fire and the UI returns to the login screen.
 *
 * The refresh cookie is single-use: two parallel rotations would look
 * like a replay and revoke the whole session family, so refreshes are
 * single-flight — concurrent callers share one rotation.
 */

import {
  ApiError,
  describeDetail,
  fetchWithTimeout,
  toNetworkError,
} from "./http";
import type { MeResponse, TokenResponse } from "../types";

const AUTH_TIMEOUT_MS = 10_000;
const LOGIN_TIMEOUT_MS = 15_000;

export const SESSION_EXPIRED_MESSAGE =
  "Your session has ended. Please sign in again.";

/** The server definitively rejected the refresh session. */
export class SessionExpiredError extends Error {
  constructor() {
    super(SESSION_EXPIRED_MESSAGE);
    this.name = "SessionExpiredError";
  }
}

let accessToken: string | null = null;
let tokenExpiresAt: number | null = null;
let refreshInFlight: Promise<TokenResponse> | null = null;
const sessionEndedListeners = new Set<() => void>();

/** When the current access token expires (epoch ms), or null if none. */
export function getTokenExpiresAt(): number | null {
  return tokenExpiresAt;
}

/**
 * Register a callback for definitive session endings. Returns the
 * unsubscribe function.
 */
export function onSessionEnded(listener: () => void): () => void {
  sessionEndedListeners.add(listener);

  return () => {
    sessionEndedListeners.delete(listener);
  };
}

function applyToken(payload: TokenResponse): TokenResponse {
  accessToken = payload.access_token;
  tokenExpiresAt = Date.now() + payload.expires_in * 1000;
  return payload;
}

function clearToken(): void {
  accessToken = null;
  tokenExpiresAt = null;
}

function notifySessionEnded(): void {
  // Copy: a listener may unsubscribe while the notification runs.
  for (const listener of [...sessionEndedListeners]) {
    listener();
  }
}

function authHeaders(existing?: HeadersInit): Headers {
  const headers = new Headers(existing);

  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  return headers;
}

function toAuthNetworkError(error: unknown): ApiError {
  if (error instanceof ApiError && error.kind === "timeout") {
    return new ApiError("timeout", "The sign-in request timed out. Try again.");
  }

  return toNetworkError(error);
}

async function readDetail(response: Response): Promise<string | null> {
  const payload: unknown = await response.json().catch(() => null);

  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return null;
  }

  return describeDetail((payload as { detail: unknown }).detail);
}

function isTokenResponse(value: unknown): value is TokenResponse {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<TokenResponse>;

  return (
    typeof candidate.access_token === "string" &&
    candidate.access_token.length > 0 &&
    typeof candidate.token_type === "string" &&
    typeof candidate.expires_in === "number" &&
    Number.isFinite(candidate.expires_in) &&
    candidate.expires_in > 0
  );
}

function isMeResponse(value: unknown): value is MeResponse {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<MeResponse>;

  return (
    typeof candidate.user_id === "number" &&
    typeof candidate.username === "string" &&
    typeof candidate.display_name === "string" &&
    Array.isArray(candidate.roles) &&
    candidate.roles.every((role) => typeof role === "string") &&
    Array.isArray(candidate.permissions) &&
    candidate.permissions.every((permission) => typeof permission === "string")
  );
}

/** POST /api/v1/auth/login — exchange credentials for the first tokens. */
export async function login(
  username: string,
  password: string,
): Promise<TokenResponse> {
  let response: Response;

  try {
    response = await fetchWithTimeout(
      "/api/v1/auth/login",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ username, password }),
      },
      LOGIN_TIMEOUT_MS,
    );
  } catch (error) {
    throw toAuthNetworkError(error);
  }

  if (!response.ok) {
    const detail = await readDetail(response);

    throw new ApiError(
      "http",
      detail ?? "Sign-in failed. Try again.",
      response.status,
    );
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!isTokenResponse(payload)) {
    throw new ApiError(
      "invalid-response",
      "The API returned an unexpected response shape.",
    );
  }

  return applyToken(payload);
}

/**
 * POST /api/v1/auth/refresh — rotate the refresh cookie into a fresh
 * access token.
 *
 * Throws SessionExpiredError when the server rejects the session for
 * good; transport failures surface as ordinary ApiErrors so callers can
 * tell "the session is over" from "the API is unreachable".
 */
export function refreshSession(): Promise<TokenResponse> {
  if (!refreshInFlight) {
    refreshInFlight = postRefresh().finally(() => {
      refreshInFlight = null;
    });
  }

  return refreshInFlight;
}

async function postRefresh(): Promise<TokenResponse> {
  let response: Response;

  try {
    response = await fetchWithTimeout(
      "/api/v1/auth/refresh",
      { method: "POST", headers: { Accept: "application/json" } },
      AUTH_TIMEOUT_MS,
    );
  } catch (error) {
    throw toAuthNetworkError(error);
  }

  if (!response.ok) {
    if (response.status === 401) {
      // Definitive: the cookie is missing, expired, revoked or was
      // replayed, so the session family is gone server-side.
      clearToken();
      notifySessionEnded();
      throw new SessionExpiredError();
    }

    throw new ApiError(
      "http",
      "The session could not be refreshed. Try again shortly.",
      response.status,
    );
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!isTokenResponse(payload)) {
    throw new ApiError(
      "invalid-response",
      "The API returned an unexpected response shape.",
    );
  }

  return applyToken(payload);
}

/** GET /api/v1/auth/me — the signed-in user; needs a valid access token. */
export async function fetchMe(signal?: AbortSignal): Promise<MeResponse> {
  let response: Response;

  try {
    response = await fetchWithTimeout(
      "/api/v1/auth/me",
      {
        method: "GET",
        headers: authHeaders({ Accept: "application/json" }),
      },
      AUTH_TIMEOUT_MS,
      signal,
    );
  } catch (error) {
    throw toAuthNetworkError(error);
  }

  if (!response.ok) {
    const detail = await readDetail(response);

    throw new ApiError(
      "http",
      detail ?? "The signed-in user could not be loaded.",
      response.status,
    );
  }

  const payload: unknown = await response.json().catch(() => null);

  if (!isMeResponse(payload)) {
    throw new ApiError(
      "invalid-response",
      "The API returned an unexpected response shape.",
    );
  }

  return payload;
}

/**
 * Restore the session at startup: trust the in-memory token first, then
 * fall back to the refresh cookie. Returns null when no session can be
 * restored; transport failures propagate to the caller.
 */
export async function probeSession(
  signal?: AbortSignal,
): Promise<MeResponse | null> {
  if (accessToken) {
    try {
      return await fetchMe(signal);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        throw error;
      }

      clearToken();
    }
  }

  try {
    await refreshSession();
  } catch (error) {
    if (error instanceof SessionExpiredError) {
      return null;
    }

    throw error;
  }

  return fetchMe(signal);
}

/**
 * POST /api/v1/auth/logout — revoke the session server-side.
 *
 * Best-effort: a network failure must not trap the user in the UI, so
 * the local token is dropped either way. If the logout request never
 * reached the API, the refresh cookie simply lives out its remaining
 * validity server-side.
 */
export async function logout(): Promise<void> {
  try {
    // The endpoint requires a valid access token; rotate once if ours
    // has expired since the last refresh.
    let response = await postLogoutRequest();

    if (response.status === 401) {
      await refreshSession();
      response = await postLogoutRequest();
    }
  } catch {
    // Deliberately swallowed; see the docstring above.
  } finally {
    clearToken();
  }
}

function postLogoutRequest(): Promise<Response> {
  return fetchWithTimeout(
    "/api/v1/auth/logout",
    {
      method: "POST",
      headers: authHeaders({ Accept: "application/json" }),
    },
    AUTH_TIMEOUT_MS,
  );
}

/**
 * fetchWithTimeout plus the bearer token: attaches the access token
 * and, on a single 401, rotates the refresh cookie once and retries.
 * A 401 after that is returned or thrown to the caller — nothing
 * retries a request twice.
 */
export async function fetchWithAuth(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await fetchWithTimeout(
    url,
    { ...init, headers: authHeaders(init.headers) },
    timeoutMs,
    signal,
  );

  if (response.status !== 401) {
    return response;
  }

  try {
    await refreshSession();
  } catch (error) {
    if (error instanceof SessionExpiredError) {
      throw new ApiError("http", SESSION_EXPIRED_MESSAGE, 401);
    }

    // Transient (network, timeout, 5xx): the session may still be
    // alive server-side, so surface the original 401 instead of
    // declaring the session over.
    return response;
  }

  return fetchWithTimeout(
    url,
    { ...init, headers: authHeaders(init.headers) },
    timeoutMs,
    signal,
  );
}
