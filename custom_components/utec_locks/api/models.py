"""Dataclasses, enums and lenient parsers for U-tec OpenAPI payloads.

Vendor payloads are converted here, at the edge, into small immutable
dataclasses. Nothing above ``api/`` touches raw vendor dicts.

Parsing is deliberately lenient (DESIGN.md A15-A17, P5):

* capability, attribute and enum names are compared case-insensitively;
* ``states`` may be a list of ``{capability, name, value}`` or a mapping;
* push payloads may be the documented envelope, a bare list, or have
  ``payload`` itself as a list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

CATEGORY_SMART_LOCK = "smartlock"
HANDLE_LOCK_SENSOR = "utec-lock-sensor"

CAP_LOCK = "st.lock"
CAP_BATTERY = "st.batterylevel"
CAP_DOOR = "st.doorsensor"
CAP_HEALTH = "st.healthcheck"
CAP_DEFERRED = "st.deferredresponse"


class LockStateValue(StrEnum):
    """st.lock.lockState values."""

    LOCKED = "locked"
    UNLOCKED = "unlocked"
    JAMMED = "jammed"
    UNKNOWN = "unknown"


class LockMode(IntEnum):
    """st.lock.lockMode values."""

    NORMAL = 0
    PASSAGE = 1
    LOCKED = 2

    @property
    def option(self) -> str:
        """Select option key for this mode."""
        return self.name.lower()


class DoorState(StrEnum):
    """st.doorSensor.sensorState values."""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Source(StrEnum):
    """Where a LockReport came from."""

    POLL = "poll"
    PUSH = "push"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class LockInfo:
    """One lock from discovery."""

    device_id: str
    name: str
    handle_type: str
    model: str
    hw_version: str
    manufacturer: str
    has_door_sensor: bool
    custom_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """Parsed discovery reply."""

    locks: dict[str, LockInfo]
    ignored_devices: int = 0


@dataclass(frozen=True)
class LockReport:
    """One observation of a lock from Query, push or confirmation."""

    device_id: str
    received_at: datetime
    source: Source
    lock_state: LockStateValue | None = None
    lock_mode: LockMode | None = None
    door: DoorState | None = None
    battery_level: int | None = None
    online: bool | None = None
    error_code: str | None = None

    @property
    def has_data(self) -> bool:
        """True when the report carries at least one state value."""
        return any(
            v is not None
            for v in (
                self.lock_state,
                self.lock_mode,
                self.door,
                self.battery_level,
                self.online,
            )
        )


@dataclass(frozen=True)
class QueryResult:
    """Parsed Query reply: reports for returned devices, errors per device."""

    reports: dict[str, LockReport] = field(default_factory=dict)
    device_errors: dict[str, str] = field(default_factory=dict)
    unknown_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CommandReceipt:
    """Parsed command reply for one device."""

    device_id: str
    deferred_seconds: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class UserInfo:
    """Uhome.User / Get result (only what we keep)."""

    user_id: str | None
    first_name: str | None


@dataclass(frozen=True)
class PushMessage:
    """Normalized push notification."""

    kind: str  # "devicestate", "devicesync", "devicedelete" or "" (no header)
    devices: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Case-insensitive helpers
# ---------------------------------------------------------------------------


def ci_get(data: Any, key: str, default: Any = None) -> Any:
    """Return ``data[key]`` matching the key case-insensitively."""
    if not isinstance(data, Mapping):
        return default
    if key in data:
        return data[key]
    lowered = key.lower()
    for k, v in data.items():
        if isinstance(k, str) and k.lower() == lowered:
            return v
    return default


def _norm(text: Any) -> str:
    return str(text).strip().lower() if text is not None else ""


def flatten_states(states: Any) -> dict[tuple[str, str], Any]:
    """Flatten ``states`` into ``{(capability, name): value}`` (lowercased keys).

    Accepts a list of ``{capability, name, value}`` items, or a mapping of
    ``{capability: {name: value}}``. Malformed items are skipped.
    """
    out: dict[tuple[str, str], Any] = {}
    if isinstance(states, list):
        for item in states:
            if not isinstance(item, Mapping):
                continue
            cap = _norm(ci_get(item, "capability"))
            name = _norm(ci_get(item, "name"))
            if not name:
                continue
            out[(cap, name)] = ci_get(item, "value")
    elif isinstance(states, Mapping):
        for cap, attrs in states.items():
            if isinstance(attrs, Mapping):
                for name, value in attrs.items():
                    out[(_norm(cap), _norm(name))] = value
            else:
                # Flat mapping {name: value} with no capability.
                out[("", _norm(cap))] = attrs
    return out


def _find(flat: Mapping[tuple[str, str], Any], cap: str, name: str) -> Any:
    """Look up an attribute, falling back to a capability-less match."""
    if (cap, name) in flat:
        return flat[(cap, name)]
    if ("", name) in flat:
        return flat[("", name)]
    return None


def parse_lock_state(value: Any) -> LockStateValue | None:
    """Parse lockState leniently. Returns None when absent or unparseable."""
    if value is None or isinstance(value, bool):
        return None
    text = _norm(value)
    for member in LockStateValue:
        if text == member.value:
            return member
    if text in ("lock",):
        return LockStateValue.LOCKED
    if text in ("unlock", "open"):
        return LockStateValue.UNLOCKED
    if text in ("jam",):
        return LockStateValue.JAMMED
    return LockStateValue.UNKNOWN if text else None


def parse_lock_mode(value: Any) -> LockMode | None:
    """Parse lockMode (0/1/2, '0', 'normal', 'passage', 'locked')."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            return LockMode(value)
        except ValueError:
            return None
    text = _norm(value)
    if text.isdigit():
        return parse_lock_mode(int(text))
    for member in LockMode:
        if text == member.name.lower():
            return member
    return None


def parse_door(value: Any) -> DoorState | None:
    """Parse sensorState."""
    if value is None or isinstance(value, bool):
        return None
    text = _norm(value)
    for member in DoorState:
        if text == member.value:
            return member
    if text in ("opened",):
        return DoorState.OPEN
    if text in ("close",):
        return DoorState.CLOSED
    return DoorState.UNKNOWN if text else None


def parse_battery(value: Any) -> int | None:
    """Parse st.batteryLevel.level (1..5)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        level = int(float(value))
    except TypeError, ValueError:
        return None
    if 1 <= level <= 5:
        return level
    return None


def parse_online(value: Any) -> bool | None:
    """Parse st.healthCheck.status (Online / Offline)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = _norm(value)
    if text == "online":
        return True
    if text == "offline":
        return False
    return None


def _error_code(device: Mapping[str, Any]) -> tuple[str | None, str | None]:
    err = ci_get(device, "error")
    if isinstance(err, Mapping):
        code = ci_get(err, "code")
        return (str(code) if code is not None else "UNKNOWN_ERROR"), (
            str(ci_get(err, "message")) if ci_get(err, "message") is not None else None
        )
    if isinstance(err, str) and err:
        return err, None
    return None, None


def parse_lock_report(
    device: Mapping[str, Any], source: Source, received_at: datetime
) -> LockReport | None:
    """Convert one device dict (Query or push) into a LockReport."""
    device_id = ci_get(device, "id")
    if not device_id:
        return None
    flat = flatten_states(ci_get(device, "states"))
    code, _ = _error_code(device)
    return LockReport(
        device_id=str(device_id),
        received_at=received_at,
        source=source,
        lock_state=parse_lock_state(_find(flat, CAP_LOCK, "lockstate")),
        lock_mode=parse_lock_mode(_find(flat, CAP_LOCK, "lockmode")),
        door=parse_door(_find(flat, CAP_DOOR, "sensorstate")),
        battery_level=parse_battery(_find(flat, CAP_BATTERY, "level")),
        online=parse_online(_find(flat, CAP_HEALTH, "status")),
        error_code=code,
    )


def _devices_list(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        devices: Any = payload
    else:
        devices = ci_get(payload, "devices", [])
    if not isinstance(devices, list):
        return []
    return [d for d in devices if isinstance(d, Mapping)]


def _is_lock(device: Mapping[str, Any]) -> bool:
    category = _norm(ci_get(device, "category"))
    handle = _norm(ci_get(device, "handleType"))
    return category == CATEGORY_SMART_LOCK or handle.startswith("utec-lock")


def parse_discovery(payload: Any) -> DiscoveryResult:
    """Parse a Discovery payload. Non-lock devices are only counted."""
    locks: dict[str, LockInfo] = {}
    ignored = 0
    for device in _devices_list(payload):
        device_id = ci_get(device, "id")
        if not device_id:
            ignored += 1
            continue
        if not _is_lock(device):
            ignored += 1
            continue
        info = ci_get(device, "deviceInfo") or {}
        handle = str(ci_get(device, "handleType") or "")
        custom = ci_get(device, "customData")
        locks[str(device_id)] = LockInfo(
            device_id=str(device_id),
            name=str(ci_get(device, "name") or device_id),
            handle_type=handle,
            model=str(ci_get(info, "model") or ""),
            hw_version=str(ci_get(info, "hwVersion") or ""),
            manufacturer=str(ci_get(info, "manufacturer") or "U-tec"),
            has_door_sensor=_norm(handle) == HANDLE_LOCK_SENSOR,
            custom_data=dict(custom) if isinstance(custom, Mapping) else None,
        )
    return DiscoveryResult(locks=locks, ignored_devices=ignored)


def parse_query(
    payload: Any, requested: Iterable[str], source: Source, received_at: datetime
) -> QueryResult:
    """Parse a Query payload into reports for the requested devices."""
    wanted = set(requested)
    reports: dict[str, LockReport] = {}
    errors: dict[str, str] = {}
    unknown: set[str] = set()
    for device in _devices_list(payload):
        report = parse_lock_report(device, source, received_at)
        if report is None:
            continue
        if report.device_id not in wanted:
            unknown.add(report.device_id)
            continue
        if report.error_code:
            errors[report.device_id] = report.error_code
            continue
        reports[report.device_id] = report
    return QueryResult(reports=reports, device_errors=errors, unknown_ids=frozenset(unknown))


def parse_command_receipt(payload: Any, device_id: str) -> CommandReceipt:
    """Parse a Command reply for ``device_id``."""
    for device in _devices_list(payload):
        if str(ci_get(device, "id")) != device_id:
            continue
        code, message = _error_code(device)
        if code:
            return CommandReceipt(device_id, error_code=code, error_message=message)
        flat = flatten_states(ci_get(device, "states"))
        raw = _find(flat, CAP_DEFERRED, "seconds")
        seconds: int | None
        try:
            seconds = int(raw) if raw is not None and not isinstance(raw, bool) else None
        except TypeError, ValueError:
            seconds = None
        if seconds is not None:
            seconds = max(1, min(20, seconds))
        return CommandReceipt(device_id, deferred_seconds=seconds)
    # Device missing from an otherwise successful reply: accepted, no hint.
    return CommandReceipt(device_id)


def parse_user(payload: Any) -> UserInfo:
    """Parse Uhome.User / Get."""
    user = ci_get(payload, "user")
    if not isinstance(user, Mapping):
        return UserInfo(None, None)
    uid = ci_get(user, "id")
    first = ci_get(user, "first_name")
    if first is None:
        first = ci_get(user, "firstName")
    return UserInfo(
        str(uid) if uid not in (None, "") else None,
        str(first) if first not in (None, "") else None,
    )


def normalize_push(data: Any) -> PushMessage:
    """Normalize every known push shape into a PushMessage."""
    kind = ""
    payload: Any = data
    if isinstance(data, Mapping):
        header = ci_get(data, "header")
        if isinstance(header, Mapping):
            kind = _norm(ci_get(header, "name"))
        payload = ci_get(data, "payload", data)
    devices = [dict(d) for d in _devices_list(payload)] if payload is not None else []
    return PushMessage(kind=kind, devices=devices)
