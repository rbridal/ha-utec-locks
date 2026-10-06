"""Diagnostics with redaction — stub."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import UtecConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UtecConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (scaffold)."""
    return {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "version": entry.version,
        "note": "Scaffold: no live API data; tokens/secrets never included.",
    }
