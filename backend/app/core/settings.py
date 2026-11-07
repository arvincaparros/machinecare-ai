"""Typed settings for the authentication layer.

The settings are read **lazily** through :func:`get_settings`, never at
import time: ``app.db.database`` already reads the database settings when
it is imported, which is why the API layer imports it inside the request
handlers. Keeping the same discipline here preserves the guarantee that
starting the process — and answering ``/health`` — needs neither the
database nor the JWT configuration. A missing ``JWT_SECRET`` therefore
surfaces as a clear error on the first authenticated call, not as a
startup failure.

Values come from the environment (the root ``.env`` is loaded by
``load_dotenv`` exactly as ``app.db.database`` does) and follow the same
``UPPER_SNAKE`` names used there.
"""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings


class MissingSecretError(RuntimeError):
    """Raised when JWT_SECRET is not configured."""


class AuthSettings(BaseSettings):
    """Authentication configuration.

    Attribute names map to environment variables case-insensitively, so
    ``jwt_secret`` is read from ``JWT_SECRET``.
    """

    jwt_secret: str
    jwt_issuer: str = "machinecare-api"
    jwt_audience: str = "machinecare-web"

    # Access tokens are short-lived by design; the refresh session is
    # the revocable unit and lives in the cookie.
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 14

    # Secure cookies require HTTPS. Local development runs over plain
    # HTTP on localhost, so ``.env`` sets COOKIE_SECURE=false there.
    cookie_secure: bool = True
    cookie_name: str = "machinecare_refresh"

    # Login lockout: threshold failures, then exponential backoff from
    # the base duration, capped at the maximum.
    lockout_threshold: int = 5
    lockout_base_seconds: int = 60
    lockout_max_seconds: int = 900

    # One-time admin-issued password reset tokens.
    reset_token_ttl_minutes: int = 30

    # Password policy: minimum length; the composition and common-password
    # rules live in app.core.password_policy.
    password_min_length: int = 12


@lru_cache(maxsize=1)
def get_settings() -> AuthSettings:
    """Build the settings on first use and cache them for the process."""

    load_dotenv()

    try:
        return AuthSettings()  # type: ignore[call-arg]
    except Exception as error:
        raise MissingSecretError(
            "JWT_SECRET is not configured. Set it in the environment "
            "or the root .env file before using the authenticated "
            "endpoints."
        ) from error
