"""API usage meter with persisted totals and rolling windows (DESIGN.md 10).

Every request to ``/action`` is counted at one choke point (the client's
observer). Totals and the rolling 24 x hourly / 60 x per-minute buckets are
persisted with HA's ``Store`` so they survive restarts. Response times are
tracked alongside in memory only (:mod:`.latency`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api.client import KIND_COMMAND, KIND_CONFIRM, REQUEST_KINDS, RequestRecord
from .const import (
    DOMAIN,
    USAGE_SAVE_DELAY,
    USAGE_STORE_MINOR_VERSION,
    USAGE_STORE_VERSION,
)
from .latency import LatencyTracker

_LOGGER = logging.getLogger(__name__)

ERROR_CLASSES = ("http_5xx", "http_429", "timeout", "connection", "envelope", "auth")
COMMAND_RESULTS = ("confirmed", "not_confirmed", "rejected")
PUSH_COUNTERS = ("applied", "rejected_auth", "malformed", "sync", "delete")
DISCOVERIES_PER_DAY = 4
PUSH_REGISTRATIONS_PER_DAY = 1


def store_key(entry_id: str) -> str:
    """Storage key for an entry's usage totals."""
    return f"{DOMAIN}.usage.{entry_id}"


class _UsageStore(Store[dict[str, Any]]):
    """Store with a forgiving migration (keep whatever we recognize)."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        return old_data if isinstance(old_data, dict) else {}


def _empty() -> dict[str, Any]:
    return {
        "counting_since": dt_util.utcnow().isoformat(),
        "requests_total": 0,
        "requests_by_kind": dict.fromkeys(REQUEST_KINDS, 0),
        "errors_total": 0,
        "errors_by_class": dict.fromkeys(ERROR_CLASSES, 0),
        "envelope_codes": {},
        "commands_sent": 0,
        "command_results": dict.fromkeys(COMMAND_RESULTS, 0),
        "confirm_queries": 0,
        "pushes_total": 0,
        "push_counters": dict.fromkeys(PUSH_COUNTERS, 0),
        "token_refreshes": 0,
        "refresh_suppressed": 0,
        "last_push": None,
        "hourly": {},
        "minutely": {},
        "hourly_command_overhead": {},
        # Per lock: every "Offline" report ({"count": n, "last": iso}); local only,
        # never in diagnostics (keyed by device id).
        "offline_reports": {},
    }


class UsageMeter:
    """Counts requests and outcomes; persisted via HA Store."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize (call async_load before use)."""
        self._hass = hass
        self._store = _UsageStore(
            hass,
            USAGE_STORE_VERSION,
            store_key(entry_id),
            minor_version=USAGE_STORE_MINOR_VERSION,
        )
        self.data: dict[str, Any] = _empty()
        self._listeners: list[CALLBACK_TYPE] = []
        self._clock: Callable[[], float] = time.time
        self.latency = LatencyTracker()

    async def async_load(self) -> None:
        """Load persisted totals; a corrupt file restarts counting from zero."""
        try:
            stored = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Usage totals file was unreadable; counting restarts from zero")
            stored = None
        base = _empty()
        if isinstance(stored, dict):
            for key, default in base.items():
                value = stored.get(key, default)
                if isinstance(default, dict) and isinstance(value, dict):
                    merged = dict(default)
                    merged.update(value)
                    base[key] = merged
                elif (isinstance(default, int) and isinstance(value, int)) or (
                    key in ("counting_since", "last_push") and isinstance(value, str)
                ):
                    base[key] = value
        self.data = base
        self._prune()

    async def async_flush(self) -> None:
        """Save immediately (unload / HA stop)."""
        await self._store.async_save(self.data)

    async def async_remove(self) -> None:
        """Delete persisted totals (entry removal)."""
        await self._store.async_remove()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Listen for changes."""
        self._listeners.append(update)

        def _remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return _remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _changed(self) -> None:
        self._store.async_delay_save(lambda: self.data, USAGE_SAVE_DELAY)
        self._notify()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _bump_bucket(self, name: str, key: int, amount: int = 1) -> None:
        buckets: dict[str, int] = self.data[name]
        skey = str(key)
        buckets[skey] = buckets.get(skey, 0) + amount

    def _prune(self) -> None:
        now = self._clock()
        hour = int(now // 3600)
        minute = int(now // 60)
        for name, current, keep in (
            ("hourly", hour, 24),
            ("hourly_command_overhead", hour, 24),
            ("minutely", minute, 60),
        ):
            buckets: dict[str, int] = self.data[name]
            for key in list(buckets):
                try:
                    k = int(key)
                except ValueError:
                    del buckets[key]
                    continue
                if k <= current - keep or k > current:
                    del buckets[key]

    @callback
    def record_request(self, record: RequestRecord) -> None:
        """Observer for the API client: one call per request."""
        self.latency.add(record)
        now = self._clock()
        self.data["requests_total"] += 1
        by_kind = self.data["requests_by_kind"]
        by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        self._bump_bucket("hourly", int(now // 3600))
        self._bump_bucket("minutely", int(now // 60))
        if record.kind in (KIND_COMMAND, KIND_CONFIRM):
            self._bump_bucket("hourly_command_overhead", int(now // 3600))
        if record.kind == KIND_CONFIRM:
            self.data["confirm_queries"] += 1
        if record.outcome != "ok":
            self.data["errors_total"] += 1
            classes = self.data["errors_by_class"]
            classes[record.outcome] = classes.get(record.outcome, 0) + 1
            if record.outcome == "envelope" and record.code:
                codes = self.data["envelope_codes"]
                codes[record.code] = codes.get(record.code, 0) + 1
        self._prune()
        self._changed()

    @callback
    def record_offline_report(self, device_id: str) -> int:
        """A report said the lock is offline (counted even while debounced)."""
        reports: dict[str, Any] = self.data["offline_reports"]
        entry = reports.get(device_id)
        count = entry.get("count", 0) if isinstance(entry, dict) else 0
        if not isinstance(count, int):
            count = 0
        reports[device_id] = {"count": count + 1, "last": dt_util.utcnow().isoformat()}
        self._changed()
        return count + 1

    @callback
    def forget_lock(self, device_id: str) -> None:
        """Drop a removed lock's offline-report history."""
        if self.data["offline_reports"].pop(device_id, None) is not None:
            self._changed()

    def offline_report_count(self, device_id: str) -> int:
        """Offline reports seen for one lock (persisted)."""
        entry = self.data["offline_reports"].get(device_id)
        count = entry.get("count") if isinstance(entry, dict) else None
        return count if isinstance(count, int) else 0

    def last_offline_report(self, device_id: str) -> datetime | None:
        """When the last offline report for one lock arrived (persisted)."""
        entry = self.data["offline_reports"].get(device_id)
        raw = entry.get("last") if isinstance(entry, dict) else None
        return dt_util.parse_datetime(raw) if isinstance(raw, str) else None

    @callback
    def record_token_request(self, record: RequestRecord) -> None:
        """A token refresh was timed (response-time sensors only, not counted)."""
        self.latency.add(record)
        self._notify()

    @callback
    def record_command_sent(self) -> None:
        """A lock/unlock/setMode was sent."""
        self.data["commands_sent"] += 1
        self._changed()

    @callback
    def record_command_result(self, result: str) -> None:
        """confirmed / not_confirmed / rejected."""
        results = self.data["command_results"]
        results[result] = results.get(result, 0) + 1
        self._changed()

    @callback
    def record_push(self, counter: str, authenticated: bool = True) -> None:
        """A push arrived (counter is one of PUSH_COUNTERS)."""
        self.data["pushes_total"] += 1
        counters = self.data["push_counters"]
        counters[counter] = counters.get(counter, 0) + 1
        if authenticated:
            self.data["last_push"] = dt_util.utcnow().isoformat()
        self._changed()

    @callback
    def record_token_refresh(self) -> None:
        """OAuth token refreshed (token endpoint, not /action)."""
        self.data["token_refreshes"] += 1
        self._changed()

    @callback
    def record_refresh_suppressed(self) -> None:
        """A refresh was suppressed by the 30 s floor guard."""
        self.data["refresh_suppressed"] += 1
        self._changed()

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    def _window_sum(self, name: str) -> int:
        self._prune()
        return sum(self.data[name].values())

    @property
    def requests_last_24h(self) -> int:
        """Requests in the last 24 hourly buckets."""
        return self._window_sum("hourly")

    @property
    def requests_last_hour(self) -> int:
        """Requests in the last 60 minute buckets."""
        return self._window_sum("minutely")

    @property
    def command_overhead_24h(self) -> int:
        """Command + confirmation requests in the last 24 h."""
        return self._window_sum("hourly_command_overhead")

    def projected_per_day(self, effective_interval_seconds: float) -> int:
        """86,400 / interval + discovery + registrations + command overhead."""
        background = 86400 / max(effective_interval_seconds, 1.0)
        return round(
            background
            + DISCOVERIES_PER_DAY
            + PUSH_REGISTRATIONS_PER_DAY
            + self.command_overhead_24h
        )

    def snapshot(self) -> dict[str, Any]:
        """Diagnostics view."""
        data = dict(self.data)
        data.pop("offline_reports", None)  # keyed by device id; shown per lock instead
        data["requests_last_24h"] = self.requests_last_24h
        data["requests_last_hour"] = self.requests_last_hour
        return data
