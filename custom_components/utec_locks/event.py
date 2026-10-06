"""Command-result event entity: confirmed / not_confirmed / rejected."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UtecConfigEntry
from .commands import RESULT_TYPES
from .const import SIGNAL_NEW_LOCKS
from .coordinator import UtecAccountCoordinator
from .entity import UtecLockEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up command-result event entities."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(UtecCommandResultEvent(coordinator, lid) for lid in coordinator.locks)

    @callback
    def _new(ids: list[str]) -> None:
        async_add_entities(UtecCommandResultEvent(coordinator, lid) for lid in ids)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_LOCKS.format(entry_id=entry.entry_id), _new)
    )


class UtecCommandResultEvent(UtecLockEntity, EventEntity):
    """Fires once per lock command outcome."""

    _attr_translation_key = "command_result"
    _attr_event_types = list(RESULT_TYPES)

    def __init__(self, coordinator: UtecAccountCoordinator, device_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator, device_id, "command_result")

    async def async_added_to_hass(self) -> None:
        """Subscribe to results for this lock."""
        await super().async_added_to_hass()
        assert self.coordinator.commands is not None
        self.async_on_remove(
            self.coordinator.commands.async_add_result_listener(self.device_id, self._handle_result)
        )

    @callback
    def _handle_result(self, result: str, data: dict[str, Any]) -> None:
        self._trigger_event(result, data)
        self.async_write_ha_state()
