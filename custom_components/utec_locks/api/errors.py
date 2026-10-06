"""Exception hierarchy for the U-tec OpenAPI client.

Every failure the client can raise inherits from :class:`UtecError` so callers
can catch one type. The classes map one-to-one onto the response
classification in DESIGN.md section 12.2.
"""

from __future__ import annotations

import aiohttp


class UtecError(Exception):
    """Base error for U-tec OpenAPI failures."""


# Backwards-compatible alias used by the scaffold.
UtecApiError = UtecError


class UtecAuthError(UtecError):
    """HTTP 401/403 or an ``INVALID_TOKEN`` envelope (after one refresh retry)."""


class UtecRateLimitError(UtecError):
    """HTTP 429. ``retry_after`` is seconds, or None when no header was sent."""

    def __init__(self, retry_after: float | None = None) -> None:
        """Store the server's Retry-After hint."""
        super().__init__(f"Rate limited (retry_after={retry_after})")
        self.retry_after = retry_after


class UtecServerError(UtecError):
    """HTTP 5xx."""

    def __init__(self, status: int) -> None:
        """Store the HTTP status."""
        super().__init__(f"HTTP {status}")
        self.status = status


class UtecEnvelopeError(UtecError):
    """HTTP 2xx with a top-level ``payload.error`` (or an unexpected 4xx)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        """Store the vendor error code (message is never logged by default)."""
        super().__init__(code)
        self.code = code
        self.message = message


class UtecTransportError(UtecError):
    """Timeout or connection error: the request may or may not have arrived."""


class UtecDeviceError(UtecError):
    """A per-device error (``payload.devices[].error``) on a command."""

    def __init__(self, device_id: str, code: str, message: str | None = None) -> None:
        """Store the per-device code."""
        super().__init__(code)
        self.device_id = device_id
        self.code = code
        self.message = message


class UtecTokenError(UtecError, aiohttp.ClientError):
    """The OAuth token endpoint returned an error envelope instead of a token.

    Also an ``aiohttp.ClientError`` so Home Assistant's OAuth config flow
    aborts cleanly (``oauth_failed``) instead of crashing.
    """

    def __init__(self, code: str, reauth: bool) -> None:
        """Store the code and whether it should trigger reauthentication."""
        super().__init__(code)
        self.code = code
        self.reauth = reauth
