"""U-tec OpenAPI client package (in-integration; split to PyPI later if needed)."""

from __future__ import annotations

from .client import RequestRecord, TokenProvider, UtecClient
from .errors import (
    UtecAuthError,
    UtecDeviceError,
    UtecEnvelopeError,
    UtecError,
    UtecRateLimitError,
    UtecServerError,
    UtecTokenError,
    UtecTransportError,
)

__all__ = [
    "RequestRecord",
    "TokenProvider",
    "UtecAuthError",
    "UtecClient",
    "UtecDeviceError",
    "UtecEnvelopeError",
    "UtecError",
    "UtecRateLimitError",
    "UtecServerError",
    "UtecTokenError",
    "UtecTransportError",
]
