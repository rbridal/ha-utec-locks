"""Lock-mode select: Normal / Passage / Locked (mode 2). Always sends setMode."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import UtecConfigEntry
from .api.models import LockMode
from .commands import CMD_SET_MODE
from .const import DOMAIN, SIGNAL_NEW_LOCKS
from .coordinator import UtecAccountCoordinator
from .entity import UtecLockEntity
from .state import current_mode

PARALLEL_UPDATES = 0

OPTIONS = [mode.option for mode in LockMode]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lock-mode selects."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(UtecLockModeSelect(coordinator, lid) for lid in coordinator.locks)

    @callback
    def _new(ids: list[str]) -> None:
        async_add_entities(UtecLockModeSelect(coordinator, lid) for lid in ids)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_LOCKS.format(entry_id=entry.entry_id), _new)
    )


class UtecLockModeSelect(UtecLockEntity, SelectEntity):
    """Lock working mode (st.lock lockMode / setMode)."""

    _attr_translation_key = "lock_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = OPTIONS

    def __init__(self, coordinator: UtecAccountCoordinator, device_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator, device_id, "lock_mode")

    @property
    def current_option(self) -> str | None:
        """Only the reported mode; None when unknown, stale or offline."""
        mode = current_mode(self.snapshot, dt_util.utcnow(), self.coordinator.stale_after)
        return mode.option if mode is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Pending mode while a setMode is being confirmed."""
        commands = self.coordinator.commands
        pending = commands.pending_for(self.device_id) if commands else None
        pending_mode = None
        if pending is not None and pending.command == CMD_SET_MODE:
            pending_mode = pending.expected_label
        return {"pending_mode": pending_mode}

    async def async_select_option(self, option: str) -> None:
        """Always send setMode, even if the cached mode already matches."""
        if option not in OPTIONS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_mode",
                translation_placeholders={"option": option},
            )
        assert self.coordinator.commands is not None
        await self.coordinator.commands.async_set_mode(self.device_id, LockMode[option.upper()])
