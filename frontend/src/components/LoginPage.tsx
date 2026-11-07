/**
 * The sign-in screen, shown whenever no session is available.
 *
 * Accounts exist only by administrator provisioning (Phase 1 plan):
 * there is no self-service registration, so the screen says where to
 * get an account rather than offering one.
 */

import { useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";

export function LoginPage() {
  const { signIn, sessionExpired } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const cleanedUsername = username.trim();

    if (!cleanedUsername || !password || submitting) {
      return;
    }

    setError(null);
    setSubmitting(true);

    try {
      await signIn(cleanedUsername, password);
    } catch (submitError) {
      // The password is dropped either way; keeping the username saves
      // a retyping mistake, not a credential.
      setPassword("");

      setError(
        submitError instanceof ApiError
          ? submitError.message
          : "Sign-in failed unexpectedly. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login">
      <div className="shell__glow" aria-hidden="true" />

      <div className="login__card">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path
                d="M12 2.6 19.8 7v10L12 21.4 4.2 17V7L12 2.6Z"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinejoin="round"
              />
              <path
                d="M12 8.4 15.2 10v4L12 15.6 8.8 14v-4L12 8.4Z"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <span className="brand__text">
            <span className="brand__name">
              MachineCare <em>AI</em>
            </span>
            <span className="brand__tag">Downtime intelligence</span>
          </span>
        </div>

        <h1 className="login__title">Sign in</h1>
        <p className="login__subtitle">
          Ask about past downtime; every answer cites its evidence.
        </p>

        {sessionExpired && (
          <p className="login__banner login__banner--notice" role="alert">
            Your session has ended. Sign in to continue.
          </p>
        )}

        {error && (
          <p className="login__banner login__banner--error" role="alert">
            {error}
          </p>
        )}

        <form className="login__form" onSubmit={handleSubmit}>
          <div className="login__field">
            <label htmlFor="login-username">Username</label>
            <input
              id="login-username"
              name="username"
              autoComplete="username"
              autoFocus
              maxLength={64}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="login__field">
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              name="password"
              type="password"
              autoComplete="current-password"
              maxLength={256}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={submitting}
            />
          </div>

          <button type="submit" className="login__submit" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="login__foot">
          Accounts are provisioned by an administrator. Forgot your
          password? Ask one to issue a reset token.
        </p>
      </div>
    </main>
  );
}
