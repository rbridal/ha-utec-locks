"""Diagnostics with redaction (DESIGN.md 11.4).

Tokens, secrets, client id, webhook id and URLs, user id and names, serial
numbers and ``customData`` are redacted. Device ids are replaced with a
stable short hash (``lock_3f9a``) so a user can point at "this lock" in a bug
report without publishing a MAC address.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.loader import async_get_integration

from . import UtecConfigEntry
from .const import DOMAIN

TO_REDACT = {
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "push_secret",
    "push_secret_previous",
    "webhook_id",
    "cloudhook_url",
    "url",
    "user_id",
    "first_name",
    "last_name",
    "serial",
    "serial_number",
    "customData",
    "custom_data",
}


def hash_id(device_id: str) -> str:
    """Stable short hash for a device id."""
    return "lock_" + hashlib.sha256(device_id.encode()).hexdigest()[:4]


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _enum(value: Any) -> Any:
    if value is None:
        return None
    return getattr(value, "value", value)


def _lock_diag(entry: UtecConfigEntry, device_id: str) -> dict[str, Any]:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    info = coordinator.locks[device_id]
    snap = coordinator.store.get(device_id)
    report = coordinator.store.last_reports.get(device_id)
    pending = runtime.commands.pending_for(device_id)
    return {
        "id": hash_id(device_id),
        "handle_type": info.handle_type,
        "manufacturer": info.manufacturer,
        "model": info.model,
        "hw_version": info.hw_version,
        "has_door_sensor": info.has_door_sensor,
        "has_custom_data": info.custom_data is not None,
        "removed": device_id in coordinator.removed,
        "state": {
            "lock_state": _enum(snap.lock_state),
            "lock_state_at": _iso(snap.lock_state_at),
            "lock_state_source": _enum(snap.lock_state_source),
            "lock_mode": _enum(snap.lock_mode),
            "lock_mode_at": _iso(snap.lock_mode_at),
            "door": _enum(snap.door),
            "door_at": _iso(snap.door_at),
            "battery_level": snap.battery_level,
            "battery_at": _iso(snap.battery_at),
            "online": snap.online,
            "last_error": snap.last_error,
        },
        "last_report": None
        if report is None
        else {
            "source": _enum(report.source),
            "received_at": _iso(report.received_at),
            "lock_state": _enum(report.lock_state),
            "lock_mode": _enum(report.lock_mode),
            "door": _enum(report.door),
            "battery_level": report.battery_level,
            "online": report.online,
            "error_code": report.error_code,
        },
        "pending_command": None
        if pending is None
        else {
            "command": pending.command,
            "expected": pending.expected_label,
            "queries": pending.queries,
            "deferred": pending.deferred,
        },
        "last_command_result": runtime.commands.last_results.get(device_id),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UtecConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    health = runtime.health.as_dict()
    for event in health["evidence"]:
        event["device_id"] = hash_id(event["device_id"])
    return {
        "integration_version": str(integration.version),
        "ha_version": HA_VERSION,
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "user_id_source": entry.data.get("user_id_source"),
        },
        "coordinator": runtime.coordinator.diagnostics(),
        "push": {**health, **runtime.push.diagnostics()},
        "exchanges": [record.as_dict() for record in runtime.client.exchanges],
        "usage": runtime.usage.snapshot(),
        "locks": [_lock_diag(entry, lid) for lid in runtime.coordinator.locks],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: UtecConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one lock device."""
    data = await async_get_config_entry_diagnostics(hass, entry)
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier in entry.runtime_data.coordinator.locks:
            data["locks"] = [_lock_diag(entry, identifier)]
            break
    return data
