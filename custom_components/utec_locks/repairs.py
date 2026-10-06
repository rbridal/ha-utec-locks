"""Repairs: the lock_removed fix flow (other issues are guidance only)."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import ISSUE_LOCK_REMOVED
from .helpers import async_get_lock_device


class LockRemovedRepairFlow(RepairsFlow):
    """Offer to delete the device of a lock that left the account."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        """Initialize."""
        self._data = data or {}

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """First step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Remove the device on confirmation."""
        if user_input is not None:
            entry_id = self._data.get("entry_id")
            device_id = self._data.get("device_id")
            registry = dr.async_get(self.hass)
            device = (
                async_get_lock_device(self.hass, str(entry_id), str(device_id))
                if entry_id
                else None
            )
            if device is not None:
                registry.async_update_device(device.id, remove_config_entry_id=entry_id)
            entry = self.hass.config_entries.async_get_entry(str(entry_id))
            if entry is not None and hasattr(entry, "runtime_data"):
                coordinator = entry.runtime_data.coordinator
                coordinator.locks.pop(str(device_id), None)
                coordinator.removed.discard(str(device_id))
                coordinator.store.remove(str(device_id))
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create a fix flow for a fixable issue."""
    if issue_id.startswith(ISSUE_LOCK_REMOVED):
        return LockRemovedRepairFlow(data)
    return ConfirmRepairFlow()
