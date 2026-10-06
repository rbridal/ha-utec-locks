"""Re-register push button (disabled by default, 5-minute cooldown)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import UtecConfigEntry
from .coordinator import UtecAccountCoordinator
from .entity import UtecAccountEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the re-register push button."""
    async_add_entities([UtecReregisterPushButton(entry.runtime_data.coordinator, entry)])


class UtecReregisterPushButton(UtecAccountEntity, ButtonEntity):
    """Re-register the push URL with a fresh secret."""

    _attr_translation_key = "reregister_push"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: UtecAccountCoordinator, entry: UtecConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator, "reregister_push")
        self._entry = entry

    async def async_press(self) -> None:
        """Re-register now (raises during the cooldown)."""
        await self._entry.runtime_data.push.async_press_button()
