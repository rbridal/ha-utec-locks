"""Small Home Assistant compatibility helpers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


def async_get_lock_device(
    hass: HomeAssistant, entry_id: str, device_id: str
) -> dr.DeviceEntry | None:
    """Device registry entry of one lock owned by ``entry_id``.

    HA 2026.9 deprecates ``async_get_device`` in favour of
    ``async_get_device_by_identifier``; HA 2026.3 only has the former.
    """
    registry = dr.async_get(hass)
    identifier = (DOMAIN, device_id)
    by_identifier = getattr(registry, "async_get_device_by_identifier", None)
    if by_identifier is not None:
        return by_identifier(identifier, entry_id)
    device = registry.async_get_device(identifiers={identifier})
    if device is None or entry_id not in device.config_entries:
        return None
    return device
