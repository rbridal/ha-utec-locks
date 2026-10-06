"""Base entity classes and device info."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import LockInfo
from .const import DOMAIN, MANUFACTURER
from .coordinator import UtecAccountCoordinator
from .state import LockSnapshot


class UtecLockEntity(CoordinatorEntity[UtecAccountCoordinator]):
    """Entity belonging to one lock device.

    Availability deliberately ignores poll failures: an entity is unavailable
    only when its lock is gone from the account (DESIGN.md 7.2). Home
    Assistant silently skips service calls to unavailable entities, and a
    lock you cannot command is worse than one whose state is uncertain.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: UtecAccountCoordinator, device_id: str, key: str | None = None
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.device_id = device_id
        self._attr_unique_id = device_id if key is None else f"{device_id}_{key}"
        info = coordinator.locks[device_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=info.name,
            manufacturer=info.manufacturer or MANUFACTURER,
            model=info.model or None,
            hw_version=info.hw_version or None,
        )

    @property
    def available(self) -> bool:
        """Unavailable only when the lock is gone from the account."""
        return self.coordinator.is_available(self.device_id)

    @property
    def lock_info(self) -> LockInfo:
        """Discovery record."""
        return self.coordinator.locks[self.device_id]

    @property
    def snapshot(self) -> LockSnapshot:
        """Last known values."""
        return self.coordinator.store.get(self.device_id)


class UtecAccountEntity(CoordinatorEntity[UtecAccountCoordinator]):
    """Entity on the account service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UtecAccountCoordinator, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"account_{entry_id}")},
            name="U-tec account",
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Account entities stay available while the entry is loaded."""
        return True

    async def async_added_to_hass(self) -> None:
        """Also update on usage changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.usage.async_add_listener(self.async_write_ha_state))
