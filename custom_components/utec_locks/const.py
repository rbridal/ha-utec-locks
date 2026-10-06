"""Constants for the U-tec Locks integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "utec_locks"
MANUFACTURER: Final = "U-tec"
ATTRIBUTION: Final = "Data provided by U-tec OpenAPI"

# Config entry versioning (migrate from day one).
CONFIG_ENTRY_VERSION: Final = 1
CONFIG_ENTRY_MINOR_VERSION: Final = 1

# ---------------------------------------------------------------------------
# Polling floor (firm)
# ---------------------------------------------------------------------------
# Background polling floor: nothing in the UI, services, or manual refresh
# may poll the full account faster than this. There is no faster fallback
# when push is down.
MIN_POLL_INTERVAL: Final = timedelta(seconds=30)
DEFAULT_POLL_INTERVAL: Final = timedelta(seconds=30)
MAX_POLL_INTERVAL: Final = timedelta(seconds=3600)

# Optional: slow down while push is healthy (off by default).
DEFAULT_PUSH_HEALTHY_POLL_INTERVAL: Final = timedelta(seconds=120)
MIN_PUSH_HEALTHY_POLL_INTERVAL: Final = timedelta(seconds=60)
MAX_PUSH_HEALTHY_POLL_INTERVAL: Final = timedelta(seconds=1800)

# Staleness: max(3 * effective poll interval, 120 s) at runtime.
MIN_STALE_AFTER: Final = timedelta(seconds=120)

# ---------------------------------------------------------------------------
# Command confirmation schedule
# ---------------------------------------------------------------------------
# Inter-tick delays (seconds) after an accepted command. Batched across locks
# that are pending at the same time. This is the only polling allowed faster
# than the 30 s background floor, and only because the user just acted.
#
# Locked decision: Fibonacci-ish burst; NO hourly confirmation budget.
# Commands are still confirmed via this schedule + push + background polls.
CONFIRMATION_SCHEDULE_SECONDS: Final[tuple[int, ...]] = (
    1,
    1,
    1,
    1,
    2,
    3,
    5,
    8,
    13,
    21,
)
# Absolute tick times: 1, 2, 3, 4, 6, 9, 14, 22, 35, 56 s (max 10 queries/command).
CONFIRMATION_MAX_QUERIES: Final = len(CONFIRMATION_SCHEDULE_SECONDS)
# not_confirmed after ~90 s (56 s burst plus at least one background poll).
CONFIRMATION_TIMEOUT_SECONDS: Final = 90

# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------
# HTTPS-only push URL. Nabu Casa / Home Assistant Cloud cloudhooks qualify
# (they are HTTPS). Plain HTTP local webhooks do NOT — the push secret travels
# in an Authorization header.
PUSH_HTTPS_ONLY: Final = True
PUSH_SECRET_ROTATION_INTERVAL: Final = timedelta(days=1)

# ---------------------------------------------------------------------------
# Discovery / API
# ---------------------------------------------------------------------------
API_ACTION_URL: Final = "https://api.u-tec.com/action"
OAUTH_AUTHORIZE_URL: Final = "https://oauth.u-tec.com/authorize"
OAUTH_TOKEN_URL: Final = "https://oauth.u-tec.com/token"
OAUTH_SCOPE: Final = "openapi"
DISCOVERY_INTERVAL: Final = timedelta(hours=6)

# Legacy integration domain. Setup is BLOCKED while a loaded entry exists
# (push-slot conflict + doubled API load). We never touch its entries.
CONFLICTING_DOMAIN: Final = "u_tec"

# Platforms forwarded at setup.
PLATFORMS: Final[tuple[str, ...]] = (
    "lock",
    "select",
    "sensor",
    "binary_sensor",
    "event",
    "button",
)
