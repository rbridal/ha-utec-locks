"""Push (webhook) manager — stub.

HTTPS-only: register Nabu Casa cloudhook or external HTTPS URL with a trusted
CA certificate. Plain HTTP local webhooks are not used (push secret in header).
"""

from __future__ import annotations

from .const import PUSH_HTTPS_ONLY, PUSH_SECRET_ROTATION_INTERVAL


class PushManager:
    """Register, rotate secret, verify, normalize push payloads (scaffold)."""

    https_only = PUSH_HTTPS_ONLY
    rotation_interval = PUSH_SECRET_ROTATION_INTERVAL
