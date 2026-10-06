"""Lock platform: lock and unlock, always sent, never optimistic."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import UtecConfigEntry
from .commands import CMD_LOCK, CMD_UNLOCK
from .const import SIGNAL_NEW_LOCKS
from .coordinator import UtecAccountCoordinator
from .entity import UtecLockEntity
from .state import LockView, derive_lock_view

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lock entities (and add new locks later)."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(UtecLock(coordinator, lid) for lid in coordinator.locks)

    @callback
    def _new(ids: list[str]) -> None:
        async_add_entities(UtecLock(coordinator, lid) for lid in ids)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_LOCKS.format(entry_id=entry.entry_id), _new)
    )


class UtecLock(UtecLockEntity, LockEntity):
    """A U-tec lock."""

    _attr_name = None
    _unrecorded_attributes = frozenset({"last_reported", "last_report_source"})

    def __init__(self, coordinator: UtecAccountCoordinator, device_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator, device_id)

    def _view(self) -> LockView:
        return derive_lock_view(self.snapshot, dt_util.utcnow(), self.coordinator.stale_after)

    @property
    def _pending_command(self) -> str | None:
        commands = self.coordinator.commands
        pending = commands.pending_for(self.device_id) if commands else None
        return pending.command if pending else None

    @property
    def is_locked(self) -> bool | None:
        """Reported state; None (unknown) when stale or cloud offline."""
        return self._view().is_locked

    @property
    def is_jammed(self) -> bool:
        """Fresh Jammed report."""
        return self._view().is_jammed

    @property
    def is_locking(self) -> bool:
        """A lock command is waiting for confirmation."""
        return self._pending_command == CMD_LOCK

    @property
    def is_unlocking(self) -> bool:
        """An unlock command is waiting for confirmation."""
        return self._pending_command == CMD_UNLOCK

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Staleness and command details (DESIGN.md 7.1)."""
        snap = self.snapshot
        view = self._view()
        commands = self.coordinator.commands
        cloud = None if snap.online is None else ("online" if snap.online else "offline")
        return {
            "last_reported": snap.lock_state_at.isoformat() if snap.lock_state_at else None,
            "last_report_source": snap.lock_state_source.value if snap.lock_state_source else None,
            "stale": view.stale,
            "last_known_state": view.last_known_state.value if view.last_known_state else None,
            "lock_mode": snap.lock_mode.option if snap.lock_mode is not None else None,
            "cloud_status": cloud,
            "pending_command": self._pending_command,
            "last_command_result": commands.last_results.get(self.device_id) if commands else None,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        """Send lock. Always sent, whatever the cached state."""
        assert self.coordinator.commands is not None
        await self.coordinator.commands.async_lock(self.device_id)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Send unlock. Always sent, whatever the cached state."""
        assert self.coordinator.commands is not None
        await self.coordinator.commands.async_unlock(self.device_id)
