"""Binary sensors: door, battery low, status stale, cloud connection, push healthy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import UtecConfigEntry
from .api.models import DoorState
from .const import SIGNAL_NEW_LOCKS
from .coordinator import UtecAccountCoordinator
from .entity import UtecAccountEntity, UtecLockEntity
from .push_health import PushStatus
from .state import current_door, is_fresh

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class LockBinaryDescription(BinarySensorEntityDescription):
    """Per-lock binary sensor description."""

    value_fn: Callable[[UtecLockEntity], bool | None]
    attrs_fn: Callable[[UtecLockEntity], dict[str, Any]] | None = None


def _door(entity: UtecLockEntity) -> bool | None:
    door = current_door(entity.snapshot, dt_util.utcnow(), entity.coordinator.stale_after)
    if door is None:
        return None
    return door is DoorState.OPEN


def _battery_low(entity: UtecLockEntity) -> bool | None:
    level = entity.snapshot.battery_level
    return None if level is None else level <= 2


def _stale(entity: UtecLockEntity) -> bool:
    return not is_fresh(
        entity.snapshot.lock_state_at, dt_util.utcnow(), entity.coordinator.stale_after
    )


def _cloud(entity: UtecLockEntity) -> bool | None:
    return entity.snapshot.online


DOOR = LockBinaryDescription(
    key="door",
    translation_key="door",
    device_class=BinarySensorDeviceClass.DOOR,
    value_fn=_door,
)
LOCK_DESCRIPTIONS: tuple[LockBinaryDescription, ...] = (
    LockBinaryDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_battery_low,
    ),
    LockBinaryDescription(
        key="stale",
        translation_key="stale",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_stale,
    ),
    LockBinaryDescription(
        key="cloud",
        translation_key="cloud",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_cloud,
        attrs_fn=lambda e: {"offline_reports": e.snapshot.offline_reports},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data.coordinator
    doors: set[str] = set()

    def _entities(ids: list[str]) -> list[BinarySensorEntity]:
        out: list[BinarySensorEntity] = []
        for lid in ids:
            out.extend(UtecLockBinarySensor(coordinator, lid, d) for d in LOCK_DESCRIPTIONS)
            info = coordinator.locks[lid]
            if info.has_door_sensor or coordinator.store.get(lid).door_seen:
                doors.add(lid)
                out.append(UtecLockBinarySensor(coordinator, lid, DOOR))
        return out

    async_add_entities([*_entities(list(coordinator.locks)), UtecPushHealthy(coordinator)])

    @callback
    def _new(ids: list[str]) -> None:
        async_add_entities(_entities(ids))

    @callback
    def _door_appeared() -> None:
        # A door sensor that shows up in states later gets its entity then.
        new = [
            lid
            for lid in coordinator.locks
            if lid not in doors and coordinator.store.get(lid).door_seen
        ]
        if new:
            doors.update(new)
            async_add_entities(UtecLockBinarySensor(coordinator, lid, DOOR) for lid in new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_LOCKS.format(entry_id=entry.entry_id), _new)
    )
    entry.async_on_unload(coordinator.async_add_listener(_door_appeared))


class UtecLockBinarySensor(UtecLockEntity, BinarySensorEntity):
    """Per-lock binary sensor."""

    entity_description: LockBinaryDescription

    def __init__(
        self,
        coordinator: UtecAccountCoordinator,
        device_id: str,
        description: LockBinaryDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Value from the description."""
        return self.entity_description.value_fn(self)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Optional attributes (cloud connection: pending offline reports)."""
        fn = self.entity_description.attrs_fn
        return fn(self) if fn else None


class UtecPushHealthy(UtecAccountEntity, BinarySensorEntity):
    """On only while push is healthy; unknown while unverified."""

    _attr_translation_key = "push_healthy"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: UtecAccountCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "push_healthy")

    @property
    def is_on(self) -> bool | None:
        """Healthy -> on; unverified -> unknown; otherwise off."""
        status = self.coordinator.health.status
        if status is PushStatus.UNVERIFIED:
            return None
        return status is PushStatus.HEALTHY
