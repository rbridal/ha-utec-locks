"""Binary sensor platform — stub."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UtecConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors (scaffold: none yet)."""
    return None
