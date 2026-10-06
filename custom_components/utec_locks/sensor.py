"""Sensors: battery level/percent and last report per lock; account usage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import UtecConfigEntry
from .const import SIGNAL_NEW_LOCKS
from .coordinator import UtecAccountCoordinator
from .entity import UtecAccountEntity, UtecLockEntity
from .push_health import PushStatus

PARALLEL_UPDATES = 0

BATTERY_LEVELS = ["critically_low", "low", "medium", "high", "full"]


@dataclass(frozen=True, kw_only=True)
class LockSensorDescription(SensorEntityDescription):
    """Per-lock sensor description."""

    value_fn: Callable[[UtecLockEntity], Any]
    attrs_fn: Callable[[UtecLockEntity], dict[str, Any]] | None = None


def _battery_attrs(entity: UtecLockEntity) -> dict[str, Any]:
    at = entity.snapshot.battery_at
    return {"last_reported": at.isoformat() if at else None}


LOCK_SENSORS: tuple[LockSensorDescription, ...] = (
    LockSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.ENUM,
        options=BATTERY_LEVELS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda e: (
            BATTERY_LEVELS[e.snapshot.battery_level - 1] if e.snapshot.battery_level else None
        ),
        attrs_fn=_battery_attrs,
    ),
    LockSensorDescription(
        key="battery_pct",
        translation_key="battery_pct",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e.snapshot.battery_level * 20 if e.snapshot.battery_level else None,
        attrs_fn=_battery_attrs,
    ),
    LockSensorDescription(
        key="last_report",
        translation_key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda e: e.snapshot.lock_state_at,
    ),
)


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    """Account sensor description."""

    value_fn: Callable[[UtecAccountCoordinator], Any]
    attrs_fn: Callable[[UtecAccountCoordinator], dict[str, Any]] | None = None


def _last_push(c: UtecAccountCoordinator) -> datetime | None:
    raw = c.usage.data.get("last_push")
    return dt_util.parse_datetime(raw) if isinstance(raw, str) else None


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="api_requests",
        translation_key="api_requests",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.usage.data["requests_total"],
        attrs_fn=lambda c: {
            **c.usage.data["requests_by_kind"],
            "counting_since": c.usage.data["counting_since"],
        },
    ),
    AccountSensorDescription(
        key="api_requests_24h",
        translation_key="api_requests_24h",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.usage.requests_last_24h,
    ),
    AccountSensorDescription(
        key="api_requests_hour",
        translation_key="api_requests_hour",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.usage.requests_last_hour,
    ),
    AccountSensorDescription(
        key="projected_requests",
        translation_key="projected_requests",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.usage.projected_per_day(c.effective_interval.total_seconds()),
    ),
    AccountSensorDescription(
        key="api_errors",
        translation_key="api_errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.usage.data["errors_total"],
        attrs_fn=lambda c: {
            **c.usage.data["errors_by_class"],
            "envelope_codes": dict(c.usage.data["envelope_codes"]),
        },
    ),
    AccountSensorDescription(
        key="commands_sent",
        translation_key="commands_sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.usage.data["commands_sent"],
        attrs_fn=lambda c: dict(c.usage.data["command_results"]),
    ),
    AccountSensorDescription(
        key="confirm_queries",
        translation_key="confirm_queries",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.usage.data["confirm_queries"],
    ),
    AccountSensorDescription(
        key="pushes_received",
        translation_key="pushes_received",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.usage.data["pushes_total"],
        attrs_fn=lambda c: dict(c.usage.data["push_counters"]),
    ),
    AccountSensorDescription(
        key="push_status",
        translation_key="push_status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in PushStatus],
        value_fn=lambda c: c.health.status.value,
    ),
    AccountSensorDescription(
        key="last_push",
        translation_key="last_push",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_push,
    ),
    AccountSensorDescription(
        key="poll_interval",
        translation_key="poll_interval",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: round(c.interval_in_use),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UtecConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        UtecLockSensor(coordinator, lid, d) for lid in coordinator.locks for d in LOCK_SENSORS
    ]
    entities.extend(UtecAccountSensor(coordinator, d) for d in ACCOUNT_SENSORS)
    async_add_entities(entities)

    @callback
    def _new(ids: list[str]) -> None:
        async_add_entities(UtecLockSensor(coordinator, lid, d) for lid in ids for d in LOCK_SENSORS)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_LOCKS.format(entry_id=entry.entry_id), _new)
    )


class UtecLockSensor(UtecLockEntity, SensorEntity):
    """Per-lock sensor."""

    entity_description: LockSensorDescription

    def __init__(
        self,
        coordinator: UtecAccountCoordinator,
        device_id: str,
        description: LockSensorDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Value from the description."""
        return self.entity_description.value_fn(self)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Optional attributes."""
        fn = self.entity_description.attrs_fn
        return fn(self) if fn else None


class UtecAccountSensor(UtecAccountEntity, SensorEntity):
    """Account-level sensor."""

    entity_description: AccountSensorDescription

    def __init__(
        self, coordinator: UtecAccountCoordinator, description: AccountSensorDescription
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Value from the description."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Per-kind counts etc."""
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator) if fn else None
