"""Command executor: always send, never optimistic, bounded confirmation.

DESIGN.md section 8:

* **Security-command rule.** Lock, unlock and setMode are ALWAYS sent when
  asked. No cached state (already locked, same mode, Passage, stale, unknown,
  cloud offline, push unhealthy, backoff, open circuit) can suppress one.
  A lock command while the cached mode is Passage is still sent, with a
  warning in the log.
* **No optimistic state.** The lock shows ``locking``/``unlocking`` (the select
  a ``pending_mode``) until a report confirms, or the attempt times out.
* **Confirmation.** Ticks at 1/1/1/1/2/3/5/8/13/21 s after the reply
  (absolute 1, 2, 3, 4, 6, 9, 14, 22, 35, 56 s), at most 10 per command,
  batched across pending locks, merged with a background poll due within 3 s.
  No hourly budget or cap. ``not_confirmed`` at about 90 s fires the
  command-result event.
* Nothing is retried after the user's call returns and nothing is queued.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import itertools
import logging
import random
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .api.client import KIND_CONFIRM
from .api.errors import (
    UtecAuthError,
    UtecError,
    UtecRateLimitError,
    UtecServerError,
    UtecTransportError,
)
from .api.models import CommandReceipt, LockMode, LockReport, LockStateValue, Source
from .const import (
    COMMAND_LOOP_LIMIT,
    COMMAND_LOOP_WINDOW,
    COMMAND_RETRY_DELAY_RANGE,
    COMMAND_SHORT_RETRY_AFTER,
    CONF_CONFIRM_COMMANDS,
    CONFIRM_BATCH_WINDOW_SECONDS,
    CONFIRM_MERGE_WINDOW_SECONDS,
    CONFIRMATION_SCHEDULE_SECONDS,
    CONFIRMATION_TIMEOUT_SECONDS,
    CONTRADICTION_WINDOW_SECONDS,
    DOMAIN,
    EVENT_COMMAND_RESULT,
    HONOR_DEFERRED_HINT,
    ISSUE_COMMAND_LOOP,
    PUSH_MISS_WINDOW_SECONDS,
)
from .helpers import async_get_lock_device

if TYPE_CHECKING:
    from .coordinator import UtecAccountCoordinator

_LOGGER = logging.getLogger(__name__)

CMD_LOCK = "lock"
CMD_UNLOCK = "unlock"
CMD_SET_MODE = "set_mode"

RESULT_CONFIRMED = "confirmed"
RESULT_NOT_CONFIRMED = "not_confirmed"
RESULT_REJECTED = "rejected"
RESULT_TYPES = (RESULT_CONFIRMED, RESULT_NOT_CONFIRMED, RESULT_REJECTED)

_API_NAMES = {CMD_LOCK: "lock", CMD_UNLOCK: "unlock", CMD_SET_MODE: "setMode"}


def absolute_ticks(
    schedule: tuple[int, ...] = CONFIRMATION_SCHEDULE_SECONDS,
    deferred: int | None = None,
) -> list[float]:
    """Absolute tick offsets (s); ticks before ``deferred`` are skipped."""
    ticks = list(itertools.accumulate(float(s) for s in schedule))
    if HONOR_DEFERRED_HINT and deferred:
        ticks = [t for t in ticks if t >= deferred]
    return ticks


@dataclass
class PendingCommand:
    """A command waiting for confirmation."""

    seq: int
    device_id: str
    command: str
    expected: LockStateValue | LockMode
    started: datetime
    accepted: bool = False
    reply_at: datetime | None = None
    deferred: int | None = None
    ticks: deque[datetime] = field(default_factory=deque)
    queries: int = 0
    timeout_unsub: CALLBACK_TYPE | None = None

    @property
    def field(self) -> str:
        """Store field that confirms this command."""
        return "lock_mode" if self.command == CMD_SET_MODE else "lock_state"

    @property
    def expected_label(self) -> str:
        """Human/automation-friendly expected value."""
        if isinstance(self.expected, LockMode):
            return self.expected.option
        return str(self.expected.value)


@dataclass
class _RecentConfirm:
    field: str
    value: Any
    at: datetime
    extra_query_done: bool = False


class _OutcomeUnknown(Exception):
    """5xx/timeout twice: the command may still have reached the lock."""


ResultListener = Callable[[str, dict[str, Any]], None]


class CommandExecutor:
    """Sends lock/unlock/setMode and runs the bounded confirmation burst."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: UtecAccountCoordinator,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._mutex: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[str, PendingCommand] = {}
        self._recent_confirm: dict[str, _RecentConfirm] = {}
        self._history: defaultdict[str, deque[datetime]] = defaultdict(deque)
        self._seq = itertools.count(1)
        self._tick_unsub: CALLBACK_TYPE | None = None
        self._result_listeners: defaultdict[str, list[ResultListener]] = defaultdict(list)
        self._state_listeners: list[CALLBACK_TYPE] = []
        self.last_results: dict[str, str] = {}
        self.rng = random.Random()
        self.sleep: Callable[[float], Any] = asyncio.sleep

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @callback
    def async_add_result_listener(self, device_id: str, listener: ResultListener) -> CALLBACK_TYPE:
        """Listen for command results of one lock (event entity)."""
        self._result_listeners[device_id].append(listener)

        def _remove() -> None:
            if listener in self._result_listeners[device_id]:
                self._result_listeners[device_id].remove(listener)

        return _remove

    def pending_for(self, device_id: str) -> PendingCommand | None:
        """The pending command for a lock, if any."""
        return self._pending.get(device_id)

    @callback
    def _notify(self) -> None:
        self._coordinator.async_update_listeners()

    @callback
    def async_shutdown(self) -> None:
        """Cancel every timer (unload)."""
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None
        for pending in self._pending.values():
            if pending.timeout_unsub:
                pending.timeout_unsub()
        self._pending.clear()

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    async def async_lock(self, device_id: str) -> None:
        """Send lock (always)."""
        await self._async_execute(device_id, CMD_LOCK, LockStateValue.LOCKED)

    async def async_unlock(self, device_id: str) -> None:
        """Send unlock (always)."""
        await self._async_execute(device_id, CMD_UNLOCK, LockStateValue.UNLOCKED)

    async def async_set_mode(self, device_id: str, mode: LockMode) -> None:
        """Send setMode (always, even if the cached mode already matches)."""
        await self._async_execute(device_id, CMD_SET_MODE, mode, {"mode": int(mode)})

    async def _async_execute(
        self,
        device_id: str,
        command: str,
        expected: LockStateValue | LockMode,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        lock = self._coordinator.locks.get(device_id)
        if lock is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="lock_not_found")
        async with self._mutex[device_id]:
            superseded = self._pending.pop(device_id, None)
            if superseded is not None:
                self._cancel_timers(superseded)
                _LOGGER.debug(
                    "%s: new command supersedes pending %s", lock.name, superseded.command
                )
            self._check_loop(device_id, lock.name)
            if command == CMD_LOCK:
                cached = self._coordinator.store.get(device_id).lock_mode
                if cached is LockMode.PASSAGE:
                    _LOGGER.warning(
                        "%s is in Passage mode; the lock may ignore lock commands "
                        "until the mode is Normal. Sending the lock command anyway",
                        lock.name,
                    )
            pending = PendingCommand(
                seq=next(self._seq),
                device_id=device_id,
                command=command,
                expected=expected,
                started=dt_util.utcnow(),
            )
            self._pending[device_id] = pending
            self._notify()
            self._coordinator.usage.record_command_sent()
            _LOGGER.info("%s: sending %s", lock.name, _API_NAMES[command])
            try:
                receipt = await self._async_send(lock, command, arguments)
            except _OutcomeUnknown as err:
                _LOGGER.warning(
                    "%s: U-tec did not confirm it received %s; checking the lock",
                    lock.name,
                    _API_NAMES[command],
                )
                self._start_confirmation(pending, None)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_outcome_unknown"
                ) from err
            except UtecAuthError as err:
                self._finish(pending, RESULT_REJECTED, code="AUTH")
                self._entry.async_start_reauth(self._hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="reauth_required"
                ) from err
            except UtecRateLimitError as err:
                self._finish(pending, RESULT_REJECTED, code="RATE_LIMITED")
                seconds = round(err.retry_after) if err.retry_after is not None else 60
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="rate_limited",
                    translation_placeholders={"seconds": str(seconds)},
                ) from err
            except UtecError as err:
                code = getattr(err, "code", type(err).__name__)
                self._finish(pending, RESULT_REJECTED, code=str(code))
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_rejected",
                    translation_placeholders={"code": str(code)},
                ) from err
            if receipt.error_code:
                self._finish(pending, RESULT_REJECTED, code=receipt.error_code)
                key = (
                    "device_offline"
                    if receipt.error_code.upper() == "DEVICE_OFFLINE"
                    else "command_rejected"
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=key,
                    translation_placeholders={"code": receipt.error_code},
                )
            self._start_confirmation(pending, receipt.deferred_seconds)

    async def _async_send(
        self, lock: Any, command: str, arguments: dict[str, Any] | None
    ) -> CommandReceipt:
        """Send once; retry once on 5xx/timeout or a short 429 (DESIGN 8.4)."""
        client = self._coordinator.client
        name = _API_NAMES[command]
        try:
            return await client.async_command(lock, name, arguments)
        except UtecRateLimitError as err:
            if err.retry_after is None or err.retry_after > COMMAND_SHORT_RETRY_AFTER:
                raise
            await self.sleep(err.retry_after)
            return await client.async_command(lock, name, arguments)
        except UtecServerError, UtecTransportError:
            await self.sleep(self.rng.uniform(*COMMAND_RETRY_DELAY_RANGE))
            try:
                return await client.async_command(lock, name, arguments)
            except UtecAuthError:
                raise
            except UtecError as err:
                # The first attempt may already have reached the lock.
                raise _OutcomeUnknown from err

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------

    def _start_confirmation(self, pending: PendingCommand, deferred: int | None) -> None:
        now = dt_util.utcnow()
        pending.accepted = True
        pending.reply_at = now
        pending.deferred = deferred
        if self._entry.options.get(CONF_CONFIRM_COMMANDS, True):
            pending.ticks = deque(
                now + timedelta(seconds=t) for t in absolute_ticks(deferred=deferred)
            )

        @callback
        def _timeout(_now: datetime) -> None:
            pending.timeout_unsub = None
            if self._pending.get(pending.device_id) is pending:
                self._finish(pending, RESULT_NOT_CONFIRMED)

        pending.timeout_unsub = async_call_later(self._hass, CONFIRMATION_TIMEOUT_SECONDS, _timeout)
        self._schedule_tick()
        self._notify()

    def _schedule_tick(self) -> None:
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None
        upcoming = [p.ticks[0] for p in self._pending.values() if p.accepted and p.ticks]
        if not upcoming:
            return
        self._tick_unsub = async_track_point_in_utc_time(
            self._hass, self._async_tick, min(upcoming)
        )

    async def _async_tick(self, _now: datetime) -> None:
        """One batched confirmation query for every pending lock."""
        self._tick_unsub = None
        now = dt_util.utcnow()
        window = timedelta(seconds=CONFIRM_BATCH_WINDOW_SECONDS)
        due = [
            p
            for p in self._pending.values()
            if p.accepted and p.ticks and p.ticks[0] <= now + window
        ]
        for p in due:
            p.ticks.popleft()
        next_poll = self._coordinator.next_poll_due
        if due and (
            next_poll is None or next_poll - now > timedelta(seconds=CONFIRM_MERGE_WINDOW_SECONDS)
        ):
            await self._async_confirm_query(
                [p.device_id for p in self._pending.values() if p.accepted], due
            )
        elif due:
            _LOGGER.debug("Confirmation tick merged into the background poll")
        self._schedule_tick()

    async def _async_confirm_query(self, device_ids: list[str], due: list[PendingCommand]) -> None:
        locks = [self._coordinator.locks[d] for d in device_ids if d in self._coordinator.locks]
        if not locks:
            return
        seq_at_start = {d: p.seq for d, p in self._pending.items() if d in device_ids}
        for p in due:
            p.queries += 1
        try:
            result = await self._coordinator.client.async_query(
                locks, kind=KIND_CONFIRM, source=Source.CONFIRM
            )
        except UtecError as err:
            _LOGGER.debug("Confirmation query failed: %s", type(err).__name__)
            return
        windows: dict[str, float] = {}
        reports: list[LockReport] = []
        for report in result.reports.values():
            current = self._pending.get(report.device_id)
            if current is not None and current.seq != seq_at_start.get(report.device_id):
                # A newer command started while this query was in flight; this
                # observation predates it and must not confirm it.
                continue
            if current is not None:
                windows[report.device_id] = (current.deferred or 0) + PUSH_MISS_WINDOW_SECONDS
            reports.append(report)
        self._coordinator.async_apply_reports(reports, evidence_window=windows)

    @callback
    def on_report(self, report: LockReport) -> None:
        """Called for every applied report: confirm a matching pending command."""
        pending = self._pending.get(report.device_id)
        if pending is None or report.received_at < pending.started:
            return
        value = getattr(report, pending.field)
        if value is not None and value == pending.expected:
            self._recent_confirm[report.device_id] = _RecentConfirm(
                pending.field, value, dt_util.utcnow()
            )
            self._finish(pending, RESULT_CONFIRMED, source=report.source)

    @callback
    def filter_contradiction(self, report: LockReport) -> LockReport:
        """Hold a report that contradicts a confirmation within 10 s.

        The contradicting value is dropped and one extra query decides; the
        newest real observation wins. Nothing is invented.
        """
        recent = self._recent_confirm.get(report.device_id)
        if recent is None:
            return report
        if dt_util.utcnow() - recent.at > timedelta(seconds=CONTRADICTION_WINDOW_SECONDS):
            del self._recent_confirm[report.device_id]
            return report
        value = getattr(report, recent.field)
        if value is None or value == recent.value:
            return report
        if recent.extra_query_done:
            # The deciding query already ran; accept what it says.
            del self._recent_confirm[report.device_id]
            return report
        recent.extra_query_done = True
        _LOGGER.debug("Report contradicts a fresh confirmation; one extra query decides")
        self._entry.async_create_background_task(
            self._hass,
            self._async_decide(report.device_id),
            f"{DOMAIN} contradiction check",
        )
        return replace(report, **{recent.field: None})

    async def _async_decide(self, device_id: str) -> None:
        lock = self._coordinator.locks.get(device_id)
        if lock is None:
            return
        try:
            result = await self._coordinator.client.async_query(
                [lock], kind=KIND_CONFIRM, source=Source.CONFIRM
            )
        except UtecError as err:
            _LOGGER.debug("Contradiction query failed: %s", type(err).__name__)
            return
        self._recent_confirm.pop(device_id, None)
        self._coordinator.async_apply_reports(list(result.reports.values()))

    # ------------------------------------------------------------------
    # Finishing
    # ------------------------------------------------------------------

    def _cancel_timers(self, pending: PendingCommand) -> None:
        if pending.timeout_unsub:
            pending.timeout_unsub()
            pending.timeout_unsub = None
        pending.ticks.clear()

    @callback
    def _finish(
        self,
        pending: PendingCommand,
        result: str,
        *,
        code: str | None = None,
        source: Source | None = None,
    ) -> None:
        if self._pending.get(pending.device_id) is pending:
            del self._pending[pending.device_id]
        self._cancel_timers(pending)
        self._schedule_tick()
        lock = self._coordinator.locks.get(pending.device_id)
        name = lock.name if lock else "lock"
        seconds = round((dt_util.utcnow() - pending.started).total_seconds(), 1)
        label = f"{result}:{code}" if code else result
        self.last_results[pending.device_id] = label
        self._coordinator.usage.record_command_result(result)
        if result == RESULT_CONFIRMED:
            _LOGGER.info(
                "%s: %s confirmed after %.1f s (%d queries, via %s)",
                name,
                _API_NAMES[pending.command],
                seconds,
                pending.queries,
                source.value if source else "report",
            )
        elif result == RESULT_NOT_CONFIRMED:
            _LOGGER.warning(
                "%s: %s was accepted by U-tec but not confirmed after %.0f s "
                "(%d queries); showing the last reported state",
                name,
                _API_NAMES[pending.command],
                seconds,
                pending.queries,
            )
        else:
            _LOGGER.warning("%s: %s rejected (%s)", name, _API_NAMES[pending.command], code)
        data: dict[str, Any] = {
            "command": pending.command,
            "expected": pending.expected_label,
            "seconds": seconds,
            "queries": pending.queries,
        }
        if code:
            data["code"] = code
        for listener in list(self._result_listeners.get(pending.device_id, [])):
            listener(result, data)
        device = async_get_lock_device(self._hass, self._entry.entry_id, pending.device_id)
        self._hass.bus.async_fire(
            EVENT_COMMAND_RESULT,
            {
                "device_id": device.id if device else None,
                "lock_name": name,
                "result": result,
                **data,
            },
        )
        self._notify()

    # ------------------------------------------------------------------
    # Loop detection (guidance, never blocking)
    # ------------------------------------------------------------------

    def _check_loop(self, device_id: str, name: str) -> None:
        now = dt_util.utcnow()
        history = self._history[device_id]
        history.append(now)
        while history and now - history[0] > COMMAND_LOOP_WINDOW:
            history.popleft()
        issue_id = f"{ISSUE_COMMAND_LOOP}_{device_id}"
        if len(history) > COMMAND_LOOP_LIMIT:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_COMMAND_LOOP,
                translation_placeholders={
                    "name": name,
                    "count": str(len(history)),
                },
            )
