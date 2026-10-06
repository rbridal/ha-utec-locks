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

# Send discovery customData with Query/Command (A11, risk 18.4). Switchable.
SEND_CUSTOM_DATA: Final = True

REPO_URL: Final = "https://github.com/rbridal/ha-utec-locks"
# Formatted with the manifest version at setup (single source of truth).
USER_AGENT_TEMPLATE: Final = "HomeAssistant-utec_locks/{version} (+" + REPO_URL + ")"

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_USE_PUSH: Final = "use_push"
CONF_SLOW_POLL_WHEN_PUSH_HEALTHY: Final = "slow_poll_when_push_healthy"
CONF_PUSH_HEALTHY_POLL_INTERVAL: Final = "push_healthy_poll_interval"
CONF_CONFIRM_COMMANDS: Final = "confirm_commands"

DEFAULT_OPTIONS: Final = {
    CONF_POLL_INTERVAL: int(DEFAULT_POLL_INTERVAL.total_seconds()),
    CONF_USE_PUSH: True,
    CONF_SLOW_POLL_WHEN_PUSH_HEALTHY: False,
    CONF_PUSH_HEALTHY_POLL_INTERVAL: int(DEFAULT_PUSH_HEALTHY_POLL_INTERVAL.total_seconds()),
    CONF_CONFIRM_COMMANDS: True,
}

# entry.data keys
DATA_USER_ID: Final = "user_id"
DATA_USER_ID_SOURCE: Final = "user_id_source"
DATA_WEBHOOK_ID: Final = "webhook_id"
DATA_PUSH_SECRET: Final = "push_secret"
DATA_PUSH_SECRET_PREVIOUS: Final = "push_secret_previous"
DATA_PUSH_SECRET_ROTATED_AT: Final = "push_secret_rotated_at"
DATA_CLOUDHOOK_URL: Final = "cloudhook_url"

# ---------------------------------------------------------------------------
# Timing details
# ---------------------------------------------------------------------------
# Manual refresh debouncer cooldown and the in-update floor guard.
REFRESH_COOLDOWN_SECONDS: Final = 30.0
FLOOR_GUARD_SECONDS: Final = 29.5
# HA schedules refreshes at int(loop.time()) + interval, which can be up to
# ~1 s early. Padding the scheduler by 1 s keeps every scheduled poll at least
# 30 s after the previous one (the floor is a floor, not a target).
SCHEDULER_PAD_SECONDS: Final = 1.0
BACKOFF_MAX_SECONDS: Final = 900.0
BACKOFF_JITTER: Final = 0.2
CIRCUIT_OPEN_AFTER_FAILURES: Final = 5
RATE_LIMIT_MAX_SECONDS: Final = 3600.0
STALE_CHECK_INTERVAL: Final = timedelta(seconds=15)
DISCOVERY_DEBOUNCE: Final = timedelta(minutes=10)
LOCK_REMOVED_AFTER_DISCOVERIES: Final = 2
# Confirmation tick merged into a background poll due within this window.
CONFIRM_MERGE_WINDOW_SECONDS: Final = 3.0
# Pending locks whose tick falls within this window share one query.
CONFIRM_BATCH_WINDOW_SECONDS: Final = 1.0
CONTRADICTION_WINDOW_SECONDS: Final = 10.0
COMMAND_RETRY_DELAY_RANGE: Final = (1.0, 2.0)
COMMAND_SHORT_RETRY_AFTER: Final = 5.0
COMMAND_LOOP_LIMIT: Final = 6
COMMAND_LOOP_WINDOW: Final = timedelta(minutes=10)
# Honor st.deferredResponse: skip ticks before the hint (DESIGN 8.3).
HONOR_DEFERRED_HINT: Final = True

# Push
PUSH_MAX_BODY_BYTES: Final = 64 * 1024
PUSH_PREVIOUS_SECRET_GRACE: Final = timedelta(minutes=10)
PUSH_REGISTER_BUTTON_COOLDOWN: Final = timedelta(minutes=5)
PUSH_AUTO_REREGISTER_MIN_GAP: Final = timedelta(hours=6)
PUSH_RETRY_DELAYS: Final[tuple[timedelta, ...]] = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
)
PUSH_REJECTION_WARN_THRESHOLD: Final = 10
PUSH_MISS_WINDOW_SECONDS: Final = 30.0
PUSH_UNHEALTHY_REPAIR_AFTER: Final = timedelta(hours=24)

# Rate-limit repair
RATE_LIMIT_REPAIR_COUNT: Final = 3
RATE_LIMIT_REPAIR_WINDOW: Final = timedelta(hours=1)
RATE_LIMIT_REPAIR_CLEAR_AFTER: Final = timedelta(hours=24)

# Events / signals
EVENT_COMMAND_RESULT: Final = f"{DOMAIN}_command_result"
SIGNAL_NEW_LOCKS: Final = f"{DOMAIN}_new_locks_{{entry_id}}"

# Repair issue ids
ISSUE_CONFLICT: Final = "conflicting_integration"
ISSUE_PUSH_UNHEALTHY: Final = "push_unhealthy"
ISSUE_PUSH_NO_HTTPS: Final = "push_no_https_url"
ISSUE_RATE_LIMITED: Final = "rate_limited"
ISSUE_COMMAND_LOOP: Final = "command_loop"
ISSUE_LOCK_REMOVED: Final = "lock_removed"

# Usage store
USAGE_STORE_VERSION: Final = 1
USAGE_STORE_MINOR_VERSION: Final = 1
USAGE_SAVE_DELAY: Final = 60

# Platforms forwarded at setup.
PLATFORMS: Final[tuple[str, ...]] = (
    "lock",
    "select",
    "sensor",
    "binary_sensor",
    "event",
    "button",
)
