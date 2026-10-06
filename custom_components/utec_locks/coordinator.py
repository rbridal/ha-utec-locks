"""Account-level data update coordinator — stub.

Design (locked):
- One coordinator per U-tec account; one batched Query per cycle for all locks.
- Background polling floor of 30 seconds, hard-coded (MIN_POLL_INTERVAL).
- Default interval equals the floor. Options may only lengthen it.
- Nothing in the UI, services, or manual refresh may poll the full account
  faster than 30 s. There is no faster fallback when push is down.
- Confirmation queries after user commands use CONFIRMATION_SCHEDULE_SECONDS
  (1/1/1/1/2/3/5/8/13/21 s, max 10 per command, ~56 s) and are the only
  faster-than-floor polling. There is no hourly confirmation budget or cap.
- HTTPS-only push registration (see const.PUSH_HTTPS_ONLY).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def clamp_poll_interval(interval: timedelta) -> timedelta:
    """Clamp any interval to the firm 30-second floor (and max 1 hour)."""
    from .const import MAX_POLL_INTERVAL

    if interval < MIN_POLL_INTERVAL:
        _LOGGER.warning(
            "Poll interval %s below floor %s; clamping to floor",
            interval,
            MIN_POLL_INTERVAL,
        )
        return MIN_POLL_INTERVAL
    if interval > MAX_POLL_INTERVAL:
        return MAX_POLL_INTERVAL
    return interval


class UtecAccountCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls all locks on one account in a single batched Query.

    Scaffold: does not call the live API. ``_async_update_data`` returns {}.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize with the firm floor as update_interval."""
        options = entry.options or {}
        raw_seconds = options.get(
            "poll_interval", int(DEFAULT_POLL_INTERVAL.total_seconds())
        )
        interval = clamp_poll_interval(timedelta(seconds=int(raw_seconds)))

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=interval,
            always_update=False,
        )
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from U-tec (scaffold: empty)."""
        # Production: one batched Query for all known lock device ids.
        # Floor guard: Debouncer cooldown >= MIN_POLL_INTERVAL; never schedule
        # closer than the floor regardless of trigger source.
        return {}
