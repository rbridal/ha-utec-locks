"""API exception hierarchy — stub."""

from __future__ import annotations


class UtecApiError(Exception):
    """Base error for U-tec OpenAPI failures."""


class UtecAuthError(UtecApiError):
    """Authentication / token errors."""


class UtecRateLimitError(UtecApiError):
    """Rate limiting (429 or envelope)."""


class UtecDeviceError(UtecApiError):
    """Per-device error from payload.devices[].error."""
