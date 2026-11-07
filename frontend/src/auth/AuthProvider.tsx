/**
 * React context for the signed-in user.
 *
 * The access token never enters React state — it lives in
 * src/api/authClient.ts where the fetch helpers read it directly. This
 * provider owns the transitions: restoring the session at startup,
 * signing in and out, refreshing the token before it expires, and
 * stepping back to the login screen when the server ends the session.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  fetchMe,
  getTokenExpiresAt,
  login as apiLogin,
  logout as apiLogout,
  onSessionEnded,
  probeSession,
  refreshSession,
} from "../api/authClient";
import type { MeResponse } from "../types";

export type AuthStatus = "loading" | "anonymous" | "authenticated";

interface AuthContextValue {
  status: AuthStatus;
  /** The signed-in user; null until authentication has completed. */
  user: MeResponse | null;
  /** True only when a session the user was using ended on its own. */
  sessionExpired: boolean;
  /** Whether the signed-in user holds a permission ("incidents:import"). */
  can: (permission: string) => boolean;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Refresh shortly before the access token expires instead of after.
const REFRESH_MARGIN_MS = 60_000;
// After the tab has been hidden, refresh on return if the token would
// not outlive another minute of use.
const VISIBILITY_MARGIN_MS = 120_000;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<MeResponse | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const refreshTimer = useRef<number | null>(null);
  // Distinguishes "the session I was using ended" from "there was never
  // a session", so the login screen only shows the expiry notice after
  // a real session was lost.
  const hadSession = useRef(false);

  const clearRefreshTimer = useCallback(() => {
    if (refreshTimer.current !== null) {
      window.clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }, []);

  const scheduleRefresh = useCallback(() => {
    clearRefreshTimer();

    const expiresAt = getTokenExpiresAt();

    if (expiresAt === null) {
      return;
    }

    const delay = Math.max(expiresAt - Date.now() - REFRESH_MARGIN_MS, 5_000);

    refreshTimer.current = window.setTimeout(() => {
      void refreshSession()
        .then(scheduleRefresh)
        .catch(() => {
          // Definitive endings arrive through onSessionEnded below;
          // transient failures simply wait for the next opportunity.
        });
    }, delay);
  }, [clearRefreshTimer]);

  // The API layer fires this when the server definitively rejects the
  // refresh session: expired, revoked, disabled user, or a replay.
  useEffect(
    () =>
      onSessionEnded(() => {
        clearRefreshTimer();
        setUser(null);
        setStatus("anonymous");
        setSessionExpired(hadSession.current);
      }),
    [clearRefreshTimer],
  );

  // Startup: restore the session from the refresh cookie if there is one.
  useEffect(() => {
    let cancelled = false;

    void probeSession()
      .then((me) => {
        if (cancelled) {
          return;
        }

        if (me) {
          hadSession.current = true;
          setUser(me);
          setStatus("authenticated");
          scheduleRefresh();
        } else {
          setStatus("anonymous");
        }
      })
      .catch(() => {
        // The API is unreachable right now; the login screen explains
        // itself when a sign-in is attempted.
        if (!cancelled) {
          setStatus("anonymous");
        }
      });

    return () => {
      cancelled = true;
      clearRefreshTimer();
    };
  }, [scheduleRefresh, clearRefreshTimer]);

  // Laptops sleep; on return, refresh a token that is about to expire.
  useEffect(() => {
    if (status !== "authenticated") {
      return;
    }

    const handleVisibility = () => {
      if (document.visibilityState !== "visible") {
        return;
      }

      const expiresAt = getTokenExpiresAt();

      if (expiresAt === null || expiresAt - Date.now() < VISIBILITY_MARGIN_MS) {
        void refreshSession()
          .then(scheduleRefresh)
          .catch(() => {
            // Same reasoning as the scheduled refresh above.
          });
      }
    };

    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [status, scheduleRefresh]);

  const signIn = useCallback(
    async (username: string, password: string) => {
      await apiLogin(username, password);

      const me = await fetchMe();

      hadSession.current = true;
      setSessionExpired(false);
      setUser(me);
      setStatus("authenticated");
      scheduleRefresh();
    },
    [scheduleRefresh],
  );

  const signOut = useCallback(async () => {
    // Revoke the server-side session first; whatever happens to that
    // request, the UI ends up signed out.
    await apiLogout();
    clearRefreshTimer();
    setSessionExpired(false);
    setUser(null);
    setStatus("anonymous");
  }, [clearRefreshTimer]);

  const can = useCallback(
    (permission: string) => user?.permissions.includes(permission) ?? false,
    [user],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, sessionExpired, can, signIn, signOut }),
    [status, user, sessionExpired, can, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);

  if (!value) {
    throw new Error("useAuth must be used inside <AuthProvider>.");
  }

  return value;
}
