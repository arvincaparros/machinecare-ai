"""Pure-ASGI middleware that tags every request with a correlation id.

The id is generated per request (an incoming ``X-Request-ID`` is kept
when present), exposed to handlers as ``request.state.request_id`` so
audit rows can carry it, and echoed back as a response header.
"""

from __future__ import annotations

import uuid

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware:
    """Minimal ASGI wrapper: no BaseHTTPMiddleware quirks."""

    def __init__(self, app) -> None:  # noqa: ANN001 (ASGI callable)
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex

        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        async def send_with_request_id(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append(
                    (REQUEST_ID_HEADER.encode("latin-1"),
                     request_id.encode("latin-1"))
                )

            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _incoming_request_id(scope) -> str | None:  # noqa: ANN001
    """Return the caller-supplied id, truncated to a sane length."""

    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode("latin-1"):
            candidate = value.decode("latin-1", errors="replace").strip()

            if candidate:
                return candidate[:64]

    return None


__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware"]
