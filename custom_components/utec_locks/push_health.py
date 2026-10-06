"""Push health: score push on evidence, not on silence (DESIGN.md 6.5).

Evidence events:

* **hit**  - a change that a push delivered is later seen by polling or a
  confirmation query;
* **miss** - polling or a confirmation observes a change and no push delivers
  it within the window (30 s; ``deferred + 30 s`` for confirmations);
* **late** - a push delivers a value after polling already saw it. Recorded
  with its latency, never counted as a hit.

Silence is never evidence: locks nobody touches send nothing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class PushStatus(StrEnum):
    """Push status states (also the push status sensor's enum options)."""

    DISABLED = "disabled"
    NO_URL = "no_url"
    REGISTRATION_FAILED = "registration_failed"
    UNVERIFIED = "unverified"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class Registration(StrEnum):
    """Registration side of push status (set by the push manager)."""

    PENDING = "pending"
    REGISTERED = "registered"
    DISABLED = "disabled"
    NO_URL = "no_url"
    FAILED = "registration_failed"


HIT = "hit"
MISS = "miss"
LATE = "late"
MIN_EVIDENCE = 3


@dataclass(frozen=True)
class Evidence:
    """One evidence event (device ids are hashed by diagnostics)."""

    kind: str
    device_id: str
    field: str
    at: datetime
    latency: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics view."""
        return {
            "kind": self.kind,
            "device_id": self.device_id,
            "field": self.field,
            "at": self.at.isoformat(),
            "latency_s": None if self.latency is None else round(self.latency, 1),
        }


def compute_status(registration: Registration, events: list[str]) -> PushStatus:
    """Pure state machine: registration state + evidence kinds (oldest first)."""
    if registration is Registration.DISABLED:
        return PushStatus.DISABLED
    if registration is Registration.NO_URL:
        return PushStatus.NO_URL
    if registration is Registration.FAILED:
        return PushStatus.REGISTRATION_FAILED
    if len(events) < MIN_EVIDENCE:
        return PushStatus.UNVERIFIED
    last5 = events[-5:]
    if events[-1] == HIT and last5.count(HIT) >= 4:
        return PushStatus.HEALTHY
    if events[-1] == MISS and events[-2] == MISS:
        return PushStatus.UNHEALTHY
    return PushStatus.DEGRADED


class PushHealth:
    """Evidence model and status for one account."""

    def __init__(self, hass: HomeAssistant, miss_window: float = 30.0) -> None:
        """Initialize."""
        self._hass = hass
        self._miss_window = miss_window
        self.registration = Registration.PENDING
        self.evidence: deque[Evidence] = deque(maxlen=20)
        # (device, field) -> value delivered by push, awaiting a poll to see it
        self._push_delivered: dict[tuple[str, str], Any] = {}
        # (device, field) -> (value, observed_at, cancel) seen by poll, awaiting push
        self._awaiting_push: dict[tuple[str, str], tuple[Any, datetime, CALLBACK_TYPE]] = {}
        self._listeners: list[Callable[[PushStatus, PushStatus], None]] = []
        self.status = compute_status(self.registration, [])
        self.status_since: datetime = dt_util.utcnow()

    @callback
    def async_add_listener(
        self, listener: Callable[[PushStatus, PushStatus], None]
    ) -> CALLBACK_TYPE:
        """Listen for status changes (old, new)."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def async_shutdown(self) -> None:
        """Cancel timers."""
        for _, _, cancel in self._awaiting_push.values():
            cancel()
        self._awaiting_push.clear()

    @callback
    def set_registration(self, registration: Registration) -> None:
        """Update the registration side and recompute."""
        self.registration = registration
        self._recompute()

    @callback
    def _recompute(self) -> None:
        new = compute_status(self.registration, [e.kind for e in self.evidence])
        old = self.status
        if new is old:
            return
        self.status = new
        self.status_since = dt_util.utcnow()
        log = _LOGGER.warning if new is PushStatus.UNHEALTHY else _LOGGER.info
        log("U-tec push status changed: %s -> %s", old, new)
        for listener in list(self._listeners):
            listener(old, new)

    @callback
    def _add(self, kind: str, device_id: str, field: str, latency: float | None = None) -> None:
        self.evidence.append(Evidence(kind, device_id, field, dt_util.utcnow(), latency))
        _LOGGER.debug("Push evidence: %s (%s)", kind, field)
        self._recompute()

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    @callback
    def on_push_value(self, device_id: str, field: str, value: Any, changed: bool) -> None:
        """A push carried ``value`` for ``field`` (``changed``: it changed the store)."""
        key = (device_id, field)
        waiting = self._awaiting_push.get(key)
        if waiting is not None and waiting[0] == value:
            _, observed_at, cancel = waiting
            cancel()
            del self._awaiting_push[key]
            latency = (dt_util.utcnow() - observed_at).total_seconds()
            self._add(LATE, device_id, field, latency)
            return
        if changed:
            self._push_delivered[key] = value

    @callback
    def on_observed(
        self,
        device_id: str,
        field: str,
        value: Any,
        changed: bool,
        window: float | None = None,
    ) -> None:
        """Polling or a confirmation query saw ``value`` for ``field``."""
        key = (device_id, field)
        if key in self._push_delivered:
            delivered = self._push_delivered.pop(key)
            if delivered == value:
                self._add(HIT, device_id, field)
            return
        if not changed or key in self._awaiting_push:
            return
        observed_at = dt_util.utcnow()
        delay = self._miss_window if window is None else window

        @callback
        def _miss(_now: datetime) -> None:
            if self._awaiting_push.pop(key, None) is not None:
                self._add(MISS, device_id, field)

        cancel = async_call_later(self._hass, timedelta(seconds=delay), _miss)
        self._awaiting_push[key] = (value, observed_at, cancel)

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics view."""
        return {
            "status": self.status.value,
            "status_since": self.status_since.isoformat(),
            "registration": self.registration.value,
            "evidence": [e.as_dict() for e in self.evidence],
        }
