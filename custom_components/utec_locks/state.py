"""LockStateStore and freshness rules (DESIGN.md 4.2, 5.5, 7.1).

Each field keeps its own timestamp and source, so a battery-only push updates
battery and never refreshes lock-state freshness. Only a report that actually
carries ``lockState`` makes the lock state fresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .api.models import DoorState, LockMode, LockReport, LockStateValue, Source

FIELDS = ("lock_state", "lock_mode", "door", "battery_level", "online")
# Fields whose changes count as push-health evidence.
EVIDENCE_FIELDS = ("lock_state", "lock_mode", "door")


@dataclass(frozen=True)
class LockSnapshot:
    """Immutable view of one lock's last known values."""

    lock_state: LockStateValue | None = None
    lock_state_at: datetime | None = None
    lock_state_source: Source | None = None
    lock_mode: LockMode | None = None
    lock_mode_at: datetime | None = None
    door: DoorState | None = None
    door_at: datetime | None = None
    battery_level: int | None = None
    battery_at: datetime | None = None
    online: bool | None = None
    online_at: datetime | None = None
    last_error: str | None = None
    door_seen: bool = False


@dataclass(frozen=True)
class FieldChange:
    """A value that changed in the store."""

    field: str
    old: Any
    new: Any


@dataclass
class LockStateStore:
    """Per-lock values with per-field timestamps."""

    _locks: dict[str, LockSnapshot] = field(default_factory=dict)
    last_reports: dict[str, LockReport] = field(default_factory=dict)

    def get(self, device_id: str) -> LockSnapshot:
        """Return the snapshot (empty when nothing is known)."""
        return self._locks.get(device_id, LockSnapshot())

    def snapshot(self) -> dict[str, LockSnapshot]:
        """Return a shallow copy of all snapshots (for coordinator data)."""
        return dict(self._locks)

    def remove(self, device_id: str) -> None:
        """Forget a lock."""
        self._locks.pop(device_id, None)
        self.last_reports.pop(device_id, None)

    def record_error(self, device_id: str, code: str | None) -> None:
        """Record a per-device error (does not refresh any timestamp)."""
        snap = self.get(device_id)
        if snap.last_error != code:
            self._locks[device_id] = replace(snap, last_error=code)

    def apply(self, report: LockReport) -> list[FieldChange]:
        """Apply a report; return the fields whose values changed."""
        snap = self.get(report.device_id)
        changes: list[FieldChange] = []
        updates: dict[str, Any] = {"last_error": report.error_code}
        at = report.received_at
        if report.lock_state is not None:
            if report.lock_state != snap.lock_state:
                changes.append(FieldChange("lock_state", snap.lock_state, report.lock_state))
            updates.update(
                lock_state=report.lock_state,
                lock_state_at=at,
                lock_state_source=report.source,
            )
        if report.lock_mode is not None:
            if report.lock_mode != snap.lock_mode:
                changes.append(FieldChange("lock_mode", snap.lock_mode, report.lock_mode))
            updates.update(lock_mode=report.lock_mode, lock_mode_at=at)
        if report.door is not None:
            if report.door != snap.door:
                changes.append(FieldChange("door", snap.door, report.door))
            updates.update(door=report.door, door_at=at, door_seen=True)
        if report.battery_level is not None:
            if report.battery_level != snap.battery_level:
                changes.append(
                    FieldChange("battery_level", snap.battery_level, report.battery_level)
                )
            updates.update(battery_level=report.battery_level, battery_at=at)
        if report.online is not None:
            if report.online != snap.online:
                changes.append(FieldChange("online", snap.online, report.online))
            updates.update(online=report.online, online_at=at)
        self._locks[report.device_id] = replace(snap, **updates)
        self.last_reports[report.device_id] = report
        return changes


def stale_after(effective_interval: timedelta, minimum: timedelta) -> timedelta:
    """``max(3 x effective poll interval, minimum)``."""
    return max(effective_interval * 3, minimum)


def is_fresh(at: datetime | None, now: datetime, threshold: timedelta) -> bool:
    """True when ``at`` is within ``threshold`` of ``now``."""
    return at is not None and (now - at) <= threshold


@dataclass(frozen=True)
class LockView:
    """What the lock entity should show (DESIGN.md 7.1)."""

    is_locked: bool | None
    is_jammed: bool
    stale: bool
    cloud_offline: bool
    last_known_state: LockStateValue | None


def derive_lock_view(snap: LockSnapshot, now: datetime, threshold: timedelta) -> LockView:
    """Map a snapshot to entity state: stale or offline means unknown."""
    stale = not is_fresh(snap.lock_state_at, now, threshold)
    offline = snap.online is False
    is_locked: bool | None = None
    jammed = False
    if not stale and not offline:
        if snap.lock_state is LockStateValue.LOCKED:
            is_locked = True
        elif snap.lock_state is LockStateValue.UNLOCKED:
            is_locked = False
        elif snap.lock_state is LockStateValue.JAMMED:
            jammed = True
    return LockView(
        is_locked=is_locked,
        is_jammed=jammed,
        stale=stale,
        cloud_offline=offline,
        last_known_state=snap.lock_state,
    )


def current_mode(snap: LockSnapshot, now: datetime, threshold: timedelta) -> LockMode | None:
    """Reported mode, or None when unknown, stale, or cloud offline."""
    if snap.online is False or not is_fresh(snap.lock_mode_at, now, threshold):
        return None
    return snap.lock_mode


def current_door(snap: LockSnapshot, now: datetime, threshold: timedelta) -> DoorState | None:
    """Door state, or None when stale or cloud offline."""
    if snap.online is False or not is_fresh(snap.door_at, now, threshold):
        return None
    if snap.door is DoorState.UNKNOWN:
        return None
    return snap.door
