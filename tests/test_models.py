"""Lenient, case-insensitive parsing of every known payload shape."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.utec_locks.api.models import (
    DoorState,
    LockMode,
    LockStateValue,
    Source,
    flatten_states,
    normalize_push,
    parse_battery,
    parse_command_receipt,
    parse_discovery,
    parse_door,
    parse_lock_mode,
    parse_lock_report,
    parse_lock_state,
    parse_online,
    parse_query,
    parse_user,
)

from .conftest import LOCK1, LOCK2, load_fixture

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def test_discovery_keeps_locks_only() -> None:
    """Non-lock devices are ignored and counted; door sensor from handleType."""
    result = parse_discovery(load_fixture("discovery.json")["payload"])
    assert set(result.locks) == {LOCK1, LOCK2}
    assert result.ignored_devices == 2
    front = result.locks[LOCK1]
    assert front.name == "Front Door"
    assert front.model == "U-Bolt Pro WiFi"
    assert front.custom_data == {"token": "secret-custom"}
    assert not front.has_door_sensor
    assert result.locks[LOCK2].has_door_sensor
    assert result.locks[LOCK2].custom_data is None


def test_discovery_handles_garbage() -> None:
    """Missing ids and odd shapes do not crash."""
    result = parse_discovery(
        {"devices": [{"name": "x"}, "nope", {"id": "1", "handleType": "utec-lock"}]}
    )
    assert set(result.locks) == {"1"}
    assert parse_discovery({"devices": "bad"}).locks == {}
    assert parse_discovery(None).locks == {}


def test_query_normal() -> None:
    """Documented casing."""
    result = parse_query(
        load_fixture("query_normal.json")["payload"], [LOCK1, LOCK2], Source.POLL, NOW
    )
    r1 = result.reports[LOCK1]
    assert r1.lock_state is LockStateValue.LOCKED
    assert r1.lock_mode is LockMode.NORMAL
    assert r1.battery_level == 4
    assert r1.online is True
    assert r1.door is None
    r2 = result.reports[LOCK2]
    assert r2.door is DoorState.CLOSED
    assert r2.lock_mode is LockMode.PASSAGE


def test_query_mixed_case() -> None:
    """Capability, attribute, key and value casing all vary."""
    result = parse_query(
        load_fixture("query_mixed_case.json")["payload"], [LOCK1, LOCK2], Source.POLL, NOW
    )
    r1 = result.reports[LOCK1]
    assert r1.lock_state is LockStateValue.LOCKED
    assert r1.lock_mode is LockMode.LOCKED
    assert r1.battery_level == 5
    assert r1.online is True
    r2 = result.reports[LOCK2]
    assert r2.door is DoorState.OPEN
    assert r2.lock_state is LockStateValue.JAMMED


def test_query_missing_caps_and_errors() -> None:
    """Missing capabilities are None; per-device errors are separated."""
    result = parse_query(
        load_fixture("query_missing_caps.json")["payload"], [LOCK1, LOCK2], Source.POLL, NOW
    )
    assert result.reports[LOCK1].lock_state is None
    assert result.reports[LOCK1].battery_level == 3
    assert not result.reports[LOCK2].has_data
    result = parse_query(
        load_fixture("query_device_error.json")["payload"], [LOCK1, LOCK2], Source.POLL, NOW
    )
    assert result.device_errors == {LOCK1: "DEVICE_OFFLINE"}
    assert LOCK1 not in result.reports
    assert result.reports[LOCK2].lock_state is LockStateValue.LOCKED


def test_query_unknown_ids() -> None:
    """Ids we did not ask for are reported as unknown."""
    payload = {"devices": [{"id": "ZZ", "states": []}]}
    assert parse_query(payload, [LOCK1], Source.POLL, NOW).unknown_ids == {"ZZ"}


def test_offline() -> None:
    """healthCheck Offline."""
    result = parse_query(load_fixture("query_offline.json")["payload"], [LOCK1], Source.POLL, NOW)
    assert result.reports[LOCK1].online is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Locked", LockStateValue.LOCKED),
        ("UNLOCKED", LockStateValue.UNLOCKED),
        ("jammed", LockStateValue.JAMMED),
        ("Unknown", LockStateValue.UNKNOWN),
        ("lock", LockStateValue.LOCKED),
        ("unlock", LockStateValue.UNLOCKED),
        ("open", LockStateValue.UNLOCKED),
        ("jam", LockStateValue.JAMMED),
        ("weird", LockStateValue.UNKNOWN),
        ("", None),
        (None, None),
        (True, None),
    ],
)
def test_parse_lock_state(value, expected) -> None:
    """lockState parsing."""
    assert parse_lock_state(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, LockMode.NORMAL),
        (1, LockMode.PASSAGE),
        (2, LockMode.LOCKED),
        ("1", LockMode.PASSAGE),
        ("Passage", LockMode.PASSAGE),
        ("normal", LockMode.NORMAL),
        (3, None),
        ("x", None),
        (None, None),
        (False, None),
    ],
)
def test_parse_lock_mode(value, expected) -> None:
    """lockMode parsing."""
    assert parse_lock_mode(value) is expected


def test_small_parsers() -> None:
    """Door, battery, online."""
    assert parse_door("Open") is DoorState.OPEN
    assert parse_door("opened") is DoorState.OPEN
    assert parse_door("close") is DoorState.CLOSED
    assert parse_door("?") is DoorState.UNKNOWN
    assert parse_door(None) is None
    assert parse_battery("3") == 3
    assert parse_battery(3.0) == 3
    assert parse_battery(0) is None
    assert parse_battery(9) is None
    assert parse_battery("x") is None
    assert parse_battery(True) is None
    assert parse_online("ONLINE") is True
    assert parse_online("offline") is False
    assert parse_online(True) is True
    assert parse_online("maybe") is None
    assert parse_online(None) is None


def test_flatten_mapping_states() -> None:
    """Mapping-style states, with and without capability."""
    flat = flatten_states({"st.Lock": {"LockState": "locked"}, "level": 3})
    assert flat[("st.lock", "lockstate")] == "locked"
    assert flat[("", "level")] == 3
    assert flatten_states([{"capability": "x"}, "bad"]) == {}


def test_report_without_id() -> None:
    """No id, no report."""
    assert parse_lock_report({"states": []}, Source.PUSH, NOW) is None


def test_command_receipts() -> None:
    """Deferred hint (clamped), device error, missing device."""
    receipt = parse_command_receipt(load_fixture("command_deferred_20.json")["payload"], LOCK1)
    assert receipt.deferred_seconds == 20
    assert receipt.error_code is None
    assert (
        parse_command_receipt(
            load_fixture("command_deferred_5.json")["payload"], LOCK1
        ).deferred_seconds
        == 5
    )
    err = parse_command_receipt(load_fixture("command_device_error.json")["payload"], LOCK1)
    assert err.error_code == "DEVICE_OFFLINE"
    big = {
        "devices": [
            {
                "id": LOCK1,
                "states": [{"capability": "st.deferredResponse", "name": "seconds", "value": 99}],
            }
        ]
    }
    assert parse_command_receipt(big, LOCK1).deferred_seconds == 20
    bad = {
        "devices": [
            {
                "id": LOCK1,
                "states": [{"capability": "st.deferredResponse", "name": "seconds", "value": "x"}],
            }
        ]
    }
    assert parse_command_receipt(bad, LOCK1).deferred_seconds is None
    assert parse_command_receipt({"devices": []}, LOCK1).deferred_seconds is None
    assert (
        parse_command_receipt({"devices": [{"id": LOCK1, "error": "OOPS"}]}, LOCK1).error_code
        == "OOPS"
    )


def test_parse_user() -> None:
    """User Get."""
    user = parse_user(load_fixture("user.json")["payload"])
    assert user.user_id == "02c7badf2b3d44d953b48b579eb9eeb5"
    assert user.first_name == "Rob"
    assert parse_user({}).user_id is None
    assert parse_user({"user": {"id": "", "firstName": "A"}}).first_name == "A"


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("push_envelope.json", "devicestate"),
        ("push_bare_list.json", ""),
        ("push_payload_list.json", ""),
        ("push_mapping_states.json", "devicestate"),
        ("push_lowercase.json", "devicestate"),
    ],
)
def test_push_shapes(fixture: str, kind: str) -> None:
    """Every known push shape normalizes to one unlocked report."""
    message = normalize_push(load_fixture(fixture))
    assert message.kind == kind
    assert len(message.devices) == 1
    report = parse_lock_report(message.devices[0], Source.PUSH, NOW)
    assert report is not None
    assert report.lock_state is LockStateValue.UNLOCKED


def test_push_special_shapes() -> None:
    """Id-only entries, sync, delete, garbage."""
    msg = normalize_push(load_fixture("push_id_only.json"))
    report = parse_lock_report(msg.devices[0], Source.PUSH, NOW)
    assert report is not None and not report.has_data
    assert normalize_push(load_fixture("push_sync.json")).kind == "devicesync"
    assert normalize_push(load_fixture("push_delete.json")).kind == "devicedelete"
    assert normalize_push("garbage").devices == []
    assert normalize_push({"devices": [{"id": "x"}]}).devices == [{"id": "x"}]
    mapping = parse_lock_report(
        normalize_push(load_fixture("push_mapping_states.json")).devices[0], Source.PUSH, NOW
    )
    assert mapping.lock_mode is LockMode.PASSAGE
    assert mapping.battery_level == 1
