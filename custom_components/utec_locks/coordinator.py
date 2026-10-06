"""Account-level data update coordinator (DESIGN.md sections 4.3 and 5).

* One coordinator per U-tec account; one batched Query per cycle for all locks.
* Background polling floor of 30 s, hard-coded (``MIN_POLL_INTERVAL``). The
  default equals the floor; options can only lengthen it.
* Nothing (UI, services, manual refresh) polls the full account faster than
  30 s: the scheduler is padded, manual refreshes go through a 30 s debouncer,
  and ``_async_update_data`` has a 29.5 s monotonic guard.
* Backoff with jitter on failures; circuit open after five; 429 honored.
* Push never suppresses polling. There is no faster fallback when push is down.
* Discovery at setup, every 6 h, and debounced (10 min) on sync/unknown ids.
* Stale is ``unknown``, not unavailable: a 15 s timer re-checks freshness and
  writes state only when a lock flips between fresh and stale.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.client import UtecClient
from .api.errors import UtecAuthError, UtecError, UtecRateLimitError
from .api.models import LockInfo, LockReport, PushMessage, Source, parse_lock_report
from .const import (
    BACKOFF_JITTER,
    BACKOFF_MAX_SECONDS,
    CIRCUIT_OPEN_AFTER_FAILURES,
    CONF_POLL_INTERVAL,
    CONF_PUSH_HEALTHY_POLL_INTERVAL,
    CONF_SLOW_POLL_WHEN_PUSH_HEALTHY,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PUSH_HEALTHY_POLL_INTERVAL,
    DISCOVERY_DEBOUNCE,
    DISCOVERY_INTERVAL,
    DOMAIN,
    FLOOR_GUARD_SECONDS,
    ISSUE_LOCK_REMOVED,
    ISSUE_RATE_LIMITED,
    LOCK_REMOVED_AFTER_DISCOVERIES,
    MAX_POLL_INTERVAL,
    MAX_PUSH_HEALTHY_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_PUSH_HEALTHY_POLL_INTERVAL,
    MIN_STALE_AFTER,
    RATE_LIMIT_MAX_SECONDS,
    RATE_LIMIT_REPAIR_CLEAR_AFTER,
    RATE_LIMIT_REPAIR_COUNT,
    RATE_LIMIT_REPAIR_WINDOW,
    REFRESH_COOLDOWN_SECONDS,
    SCHEDULER_PAD_SECONDS,
    SIGNAL_NEW_LOCKS,
    STALE_CHECK_INTERVAL,
)
from .push_health import PushHealth, PushStatus, Registration
from .state import EVIDENCE_FIELDS, LockSnapshot, LockStateStore, is_fresh, stale_after
from .usage import UsageMeter

if TYPE_CHECKING:
    from .commands import CommandExecutor

_LOGGER = logging.getLogger(__name__)

_FLOOR_WARNED: set[str] = set()


def clamp_poll_interval(interval: timedelta, *, entry_id: str | None = None) -> timedelta:
    """Clamp any interval to the firm 30-second floor (and max 1 hour)."""
    if interval < MIN_POLL_INTERVAL:
        key = entry_id or ""
        if key not in _FLOOR_WARNED:
            _FLOOR_WARNED.add(key)
            _LOGGER.warning(
                "Poll interval %s is below the %s floor; using the floor",
                interval,
                MIN_POLL_INTERVAL,
            )
        return MIN_POLL_INTERVAL
    if interval > MAX_POLL_INTERVAL:
        return MAX_POLL_INTERVAL
    return interval


def clamp_relaxed_interval(interval: timedelta) -> timedelta:
    """Clamp the relaxed (push healthy) interval to its 60-1,800 s range."""
    return max(MIN_PUSH_HEALTHY_POLL_INTERVAL, min(MAX_PUSH_HEALTHY_POLL_INTERVAL, interval))


def backoff_delay(base: float, failures: int, rng: random.Random | None = None) -> float:
    """``min(base x 2^n, 900)`` with +/-20 % jitter, never below ``base``.

    ``n = failures - 1`` so the first retry is at about ``base``. From the
    fifth consecutive failure the circuit is open: one attempt per 900 s.
    """
    rng = rng or random
    if failures >= CIRCUIT_OPEN_AFTER_FAILURES:
        return BACKOFF_MAX_SECONDS
    n = max(0, failures - 1)
    raw = min(base * (2**n), BACKOFF_MAX_SECONDS)
    jittered = raw * (1 + rng.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))
    return max(base, min(jittered, BACKOFF_MAX_SECONDS * (1 + BACKOFF_JITTER)))


def rate_limit_delay(base: float, retry_after: float | None, failures: int) -> float:
    """Honor Retry-After clamped to [base, 3,600]; else back off from n=2."""
    if retry_after is not None:
        return max(base, min(RATE_LIMIT_MAX_SECONDS, retry_after))
    return backoff_delay(base, max(failures, 3))


class UtecAccountCoordinator(DataUpdateCoordinator[dict[str, LockSnapshot]]):
    """Polls all locks on one account in a single batched Query."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: UtecClient,
        usage: UsageMeter,
        health: PushHealth,
    ) -> None:
        """Initialize with the floor-respecting interval."""
        self.client = client
        self.usage = usage
        self.health = health
        self.store = LockStateStore()
        self.locks: dict[str, LockInfo] = {}
        self.removed: set[str] = set()
        self.commands: CommandExecutor | None = None
        self.discovery_ignored = 0
        self._missing_count: dict[str, int] = {}
        self._failures = 0
        self._last_query_started: float | None = None
        self._first_success_done = False
        self.next_poll_due: datetime | None = None
        self.last_success: datetime | None = None
        self.last_failure: datetime | None = None
        self.interval_in_use: float = 0.0
        self._last_discovery: datetime | None = None
        self._discovery_unsub: CALLBACK_TYPE | None = None
        self._rate_limits: deque[datetime] = deque(maxlen=10)
        self._rate_limit_clear_unsub: CALLBACK_TYPE | None = None
        self._freshness: dict[str, tuple[bool, bool, bool]] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self.rng = random.Random()
        self.monotonic: Callable[[], float] = time.monotonic

        self.base_interval = clamp_poll_interval(
            timedelta(
                seconds=int(
                    entry.options.get(
                        CONF_POLL_INTERVAL, int(DEFAULT_POLL_INTERVAL.total_seconds())
                    )
                )
            ),
            entry_id=entry.entry_id,
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=self._padded(self.base_interval.total_seconds()),
            always_update=False,
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=REFRESH_COOLDOWN_SECONDS,
                immediate=False,
            ),
        )
        self.interval_in_use = self.base_interval.total_seconds()

    # ------------------------------------------------------------------
    # Intervals
    # ------------------------------------------------------------------

    @staticmethod
    def _padded(seconds: float) -> timedelta:
        return timedelta(seconds=seconds + SCHEDULER_PAD_SECONDS)

    @property
    def relaxed_enabled(self) -> bool:
        """Option: slow down polling while push is healthy."""
        return bool(self.config_entry.options.get(CONF_SLOW_POLL_WHEN_PUSH_HEALTHY, False))

    @property
    def relaxed_interval(self) -> timedelta:
        """Interval used while push is healthy (option on)."""
        return clamp_relaxed_interval(
            timedelta(
                seconds=int(
                    self.config_entry.options.get(
                        CONF_PUSH_HEALTHY_POLL_INTERVAL,
                        int(DEFAULT_PUSH_HEALTHY_POLL_INTERVAL.total_seconds()),
                    )
                )
            )
        )

    @property
    def effective_interval(self) -> timedelta:
        """Base interval, or the relaxed one while push is healthy (if enabled)."""
        if self.relaxed_enabled and self.health.status is PushStatus.HEALTHY:
            return max(self.base_interval, self.relaxed_interval)
        return self.base_interval

    @property
    def stale_after(self) -> timedelta:
        """``max(3 x effective interval, 120 s)``."""
        return stale_after(self.effective_interval, MIN_STALE_AFTER)

    @callback
    def async_apply_options(self) -> None:
        """Re-read options (called by the entry update listener)."""
        self.base_interval = clamp_poll_interval(
            timedelta(
                seconds=int(
                    self.config_entry.options.get(
                        CONF_POLL_INTERVAL, int(DEFAULT_POLL_INTERVAL.total_seconds())
                    )
                )
            ),
            entry_id=self.config_entry.entry_id,
        )
        if self._failures == 0:
            self.interval_in_use = self.effective_interval.total_seconds()
            self.update_interval = self._padded(self.interval_in_use)
        self.async_update_listeners()

    @callback
    def async_push_status_changed(self, old: PushStatus, new: PushStatus) -> None:
        """Adjust the interval when push health changes (relaxed mode)."""
        if not self.relaxed_enabled:
            self.async_update_listeners()
            return
        if self._failures == 0:
            self.interval_in_use = self.effective_interval.total_seconds()
            self.update_interval = self._padded(self.interval_in_use)
        if old is PushStatus.HEALTHY and new is not PushStatus.HEALTHY:
            # Back to the base interval at the first miss: reschedule through
            # the 30 s debouncer (the floor guard still applies).
            self.hass.async_create_task(self.async_request_refresh())
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Initial discovery (raises UtecError on failure)."""
        await self.async_discover(initial=True)

    @callback
    def async_start_timers(self) -> None:
        """Start the 6 h discovery and 15 s freshness timers (after setup)."""
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_discovery,
                DISCOVERY_INTERVAL,
                name=f"{DOMAIN} discovery",
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_check_freshness,
                STALE_CHECK_INTERVAL,
                name=f"{DOMAIN} freshness",
            )
        )

    @callback
    def async_stop(self) -> None:
        """Cancel timers (unload)."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._discovery_unsub:
            self._discovery_unsub()
            self._discovery_unsub = None
        if self._rate_limit_clear_unsub:
            self._rate_limit_clear_unsub()
            self._rate_limit_clear_unsub = None

    @property
    def active_locks(self) -> list[LockInfo]:
        """Locks currently on the account (not removed)."""
        return [lock for lid, lock in self.locks.items() if lid not in self.removed]

    def is_available(self, device_id: str) -> bool:
        """Unavailable only when the lock is gone from the account."""
        return device_id in self.locks and device_id not in self.removed

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, LockSnapshot]:
        """One batched Query for every known lock (floor-guarded)."""
        now_m = self.monotonic()
        if (
            self._last_query_started is not None
            and now_m - self._last_query_started < FLOOR_GUARD_SECONDS
        ):
            self.usage.record_refresh_suppressed()
            _LOGGER.debug("Refresh suppressed by the 30 s floor guard")
            return self.store.snapshot()
        locks = self.active_locks
        if not locks:
            self._schedule_next(success=True)
            return self.store.snapshot()
        self._last_query_started = now_m
        base = self.base_interval.total_seconds()
        try:
            result = await self.client.async_query(locks)
        except UtecAuthError as err:
            self.last_failure = dt_util.utcnow()
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="reauth_required"
            ) from err
        except UtecRateLimitError as err:
            self._failures += 1
            self.last_failure = dt_util.utcnow()
            self._note_rate_limited()
            delay = rate_limit_delay(base, err.retry_after, self._failures)
            self._schedule_after_failure(delay)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                translation_placeholders={"seconds": str(round(delay))},
                retry_after=delay + SCHEDULER_PAD_SECONDS,
            ) from err
        except UtecError as err:
            self._failures += 1
            self.last_failure = dt_util.utcnow()
            delay = backoff_delay(base, self._failures, self.rng)
            self._schedule_after_failure(delay)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_unreachable",
                translation_placeholders={"error": type(err).__name__},
                retry_after=delay + SCHEDULER_PAD_SECONDS,
            ) from err

        self._failures = 0
        self.last_success = dt_util.utcnow()
        for device_id, code in result.device_errors.items():
            self.store.record_error(device_id, code)
        self.async_apply_reports(list(result.reports.values()), notify=False)
        if result.unknown_ids:
            self.async_request_discovery()
        self._schedule_next(success=True)
        return self.store.snapshot()

    def _schedule_after_failure(self, delay: float) -> None:
        self.interval_in_use = delay
        self.next_poll_due = dt_util.utcnow() + timedelta(seconds=delay)

    def _schedule_next(self, *, success: bool) -> None:
        interval = self.effective_interval.total_seconds()
        if success and not self._first_success_done:
            # Startup jitter: spread fleet-wide restarts (DESIGN 5.7).
            self._first_success_done = True
            interval += self.rng.uniform(0, self.base_interval.total_seconds())
        self.interval_in_use = self.effective_interval.total_seconds()
        self.update_interval = self._padded(interval)
        self.next_poll_due = dt_util.utcnow() + timedelta(seconds=interval)

    def _note_rate_limited(self) -> None:
        now = dt_util.utcnow()
        self._rate_limits.append(now)
        recent = [t for t in self._rate_limits if now - t <= RATE_LIMIT_REPAIR_WINDOW]
        if len(recent) >= RATE_LIMIT_REPAIR_COUNT:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_RATE_LIMITED}_{self.config_entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_RATE_LIMITED,
            )
        if self._rate_limit_clear_unsub:
            self._rate_limit_clear_unsub()

        @callback
        def _clear(_now: datetime) -> None:
            self._rate_limit_clear_unsub = None
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{ISSUE_RATE_LIMITED}_{self.config_entry.entry_id}"
            )

        self._rate_limit_clear_unsub = async_call_later(
            self.hass, RATE_LIMIT_REPAIR_CLEAR_AFTER, _clear
        )

    # ------------------------------------------------------------------
    # Reports (poll, push, confirmation)
    # ------------------------------------------------------------------

    @callback
    def async_apply_reports(
        self,
        reports: list[LockReport],
        *,
        notify: bool = True,
        evidence_window: dict[str, float] | None = None,
    ) -> None:
        """Apply reports to the store, score push evidence, confirm commands."""
        for incoming in reports:
            if incoming.device_id not in self.locks:
                continue
            report = (
                self.commands.filter_contradiction(incoming)
                if self.commands is not None
                else incoming
            )
            # A first value (old None) is not a change: nothing could have pushed it.
            changes = {c.field for c in self.store.apply(report) if c.old is not None}
            track_push = self.health.registration is Registration.REGISTERED
            for field in EVIDENCE_FIELDS:
                if not track_push:
                    break
                value = getattr(report, field)
                if value is None:
                    continue
                if report.source is Source.PUSH:
                    self.health.on_push_value(report.device_id, field, value, field in changes)
                else:
                    window = None
                    if evidence_window and report.device_id in evidence_window:
                        window = evidence_window[report.device_id]
                    self.health.on_observed(
                        report.device_id, field, value, field in changes, window
                    )
            if self.commands is not None:
                self.commands.on_report(report)
        self._freshness = {lid: self._fresh_tuple(lid) for lid in self.locks}
        if notify:
            self.data = self.store.snapshot()
            self.async_update_listeners()

    @callback
    def async_handle_push(self, message: PushMessage) -> str:
        """Apply a normalized push. Returns the usage counter name."""
        if message.kind == "devicesync":
            self.async_request_discovery()
            return "sync"
        if message.kind == "devicedelete":
            self.async_request_discovery()
            return "delete"
        now = dt_util.utcnow()
        reports: list[LockReport] = []
        for device in message.devices:
            report = parse_lock_report(device, Source.PUSH, now)
            if report is None:
                continue
            if report.device_id not in self.locks or report.device_id in self.removed:
                self.async_request_discovery()
                continue
            if not report.has_data:
                continue
            reports.append(report)
        if not reports:
            return "malformed"
        self.async_apply_reports(reports)
        return "applied"

    # ------------------------------------------------------------------
    # Freshness
    # ------------------------------------------------------------------

    def _fresh_tuple(self, device_id: str) -> tuple[bool, bool, bool]:
        snap = self.store.get(device_id)
        now = dt_util.utcnow()
        threshold = self.stale_after
        return (
            is_fresh(snap.lock_state_at, now, threshold),
            is_fresh(snap.lock_mode_at, now, threshold),
            is_fresh(snap.door_at, now, threshold),
        )

    @callback
    def _async_check_freshness(self, _now: datetime | None = None) -> None:
        """Write state only when a lock flips between fresh and stale."""
        current = {lid: self._fresh_tuple(lid) for lid in self.locks}
        if current != self._freshness:
            flipped = [lid for lid in current if current[lid] != self._freshness.get(lid)]
            self._freshness = current
            for lid in flipped:
                if not current[lid][0]:
                    _LOGGER.info(
                        "%s: no fresh lock state for %s; showing unknown",
                        self.locks[lid].name,
                        self.stale_after,
                    )
            self.async_update_listeners()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def async_discover(self, *, initial: bool = False) -> None:
        """Run discovery and reconcile the lock list."""
        self._last_discovery = dt_util.utcnow()
        result = await self.client.async_discover()
        self.discovery_ignored = result.ignored_devices
        new_ids = [lid for lid in result.locks if lid not in self.locks]
        for lid, info in result.locks.items():
            self.locks[lid] = info
            self._missing_count[lid] = 0
            if lid in self.removed:
                self.removed.discard(lid)
                ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_LOCK_REMOVED}_{lid}")
                _LOGGER.info("%s is back on the account", info.name)
        for lid in list(self.locks):
            if lid in result.locks:
                continue
            self._missing_count[lid] = self._missing_count.get(lid, 0) + 1
            if (
                self._missing_count[lid] >= LOCK_REMOVED_AFTER_DISCOVERIES
                and lid not in self.removed
            ):
                self.removed.add(lid)
                info = self.locks[lid]
                _LOGGER.warning(
                    "%s is missing from two discoveries; marking unavailable", info.name
                )
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"{ISSUE_LOCK_REMOVED}_{lid}",
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_LOCK_REMOVED,
                    translation_placeholders={"name": info.name},
                    data={"entry_id": self.config_entry.entry_id, "device_id": lid},
                )
        if new_ids and not initial:
            _LOGGER.info("Discovered %d new lock(s)", len(new_ids))
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_LOCKS.format(entry_id=self.config_entry.entry_id),
                new_ids,
            )
        if not initial:
            self.async_update_listeners()

    async def _async_scheduled_discovery(self, _now: datetime | None = None) -> None:
        await self._async_safe_discover()

    async def _async_safe_discover(self) -> None:
        try:
            await self.async_discover()
        except UtecAuthError:
            self.config_entry.async_start_reauth(self.hass)
        except UtecError as err:
            _LOGGER.debug("Discovery failed: %s", type(err).__name__)

    @callback
    def async_request_discovery(self) -> None:
        """Debounced discovery: at most once per 10 minutes."""
        if self._discovery_unsub is not None:
            return
        now = dt_util.utcnow()
        if self._last_discovery is None or now - self._last_discovery >= DISCOVERY_DEBOUNCE:
            self.config_entry.async_create_background_task(
                self.hass, self._async_safe_discover(), f"{DOMAIN} discovery"
            )
            self._last_discovery = now
            return
        delay = self._last_discovery + DISCOVERY_DEBOUNCE - now

        @callback
        def _run(_now: datetime) -> None:
            self._discovery_unsub = None
            self._last_discovery = dt_util.utcnow()
            self.config_entry.async_create_background_task(
                self.hass, self._async_safe_discover(), f"{DOMAIN} discovery"
            )

        self._discovery_unsub = async_call_later(self.hass, delay, _run)

    def diagnostics(self) -> dict[str, Any]:
        """Coordinator timing for diagnostics."""
        return {
            "base_interval_s": self.base_interval.total_seconds(),
            "effective_interval_s": self.effective_interval.total_seconds(),
            "interval_in_use_s": self.interval_in_use,
            "stale_after_s": self.stale_after.total_seconds(),
            "consecutive_failures": self._failures,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "next_poll_due": self.next_poll_due.isoformat() if self.next_poll_due else None,
            "last_discovery": (self._last_discovery.isoformat() if self._last_discovery else None),
            "discovery_ignored_devices": self.discovery_ignored,
            "removed_locks": len(self.removed),
        }
