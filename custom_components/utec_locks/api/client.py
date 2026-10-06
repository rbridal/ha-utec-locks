"""U-tec OpenAPI client — stub.

POST https://api.u-tec.com/action with envelope {header, payload}.
Classify HTTP 200 error envelopes; case-insensitive parse; User-Agent.
"""

from __future__ import annotations

from .errors import UtecApiError

__all__ = ["UtecClient", "UtecApiError"]


class UtecClient:
    """Minimal client placeholder (not wired)."""

    def __init__(self) -> None:
        """Scaffold only."""
        raise NotImplementedError("UtecClient is not implemented in this scaffold pass")
