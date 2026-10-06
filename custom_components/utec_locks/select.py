"""Lock-mode select platform — stub."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UtecConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lock-mode select entities (scaffold: none yet)."""
    return None
