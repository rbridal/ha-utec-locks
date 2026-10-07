"""Security-command rule and the bounded confirmation burst (DESIGN 8)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.lock import LockState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.utec_locks.const import (
    CONF_CONFIRM_COMMANDS,
    CONF_POLL_INTERVAL,
    DOMAIN,
    EVENT_COMMAND_RESULT,
    ISSUE_COMMAND_LOOP,
)

from .conftest import LOCK1, LOCK2, FakeUtecCloud, advance, load_fixture, runtime

STATE_LOCKED = LockState.LOCKED
STATE_LOCKING = LockState.LOCKING
STATE_UNLOCKED = LockState.UNLOCKED
STATE_UNLOCKING = LockState.UNLOCKING

FRONT = "lock.front_door"  # LOCK1: locked, normal
SHOP = "lock.shop_door"  # LOCK2: unlocked, passage
FRONT_MODE = "select.front_door_lock_mode"


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Keep the background poll out of the way (3600 s) unless a test wants it."""
    return {CONF_POLL_INTERVAL: 3600}


@pytest.fixture
def events(hass: HomeAssistant) -> list[Event]:
    """Captured command-result bus events."""
    return async_capture_events(hass, EVENT_COMMAND_RESULT)


@pytest.fixture
async def loaded(freezer: FrozenDateTimeFactory, setup_entry, cloud: FakeUtecCloud):
    """Frozen time first, then the loaded entry; no-op sleeps for retries."""
    runtime(setup_entry).commands.sleep = AsyncMock()
    cloud.calls.clear()
    return setup_entry


async def call_lock(hass: HomeAssistant, service: str, entity_id: str) -> None:
    """Call lock.lock / lock.unlock."""
    await hass.services.async_call("lock", service, {"entity_id": entity_id}, blocking=True)


async def call_mode(hass: HomeAssistant, option: str, entity_id: str = FRONT_MODE) -> None:
    """Call select.select_option."""
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
    )


def commands(cloud: FakeUtecCloud) -> list[tuple[str, str, Any]]:
    """(device, name, arguments) of every Command sent."""
    out = []
    for call in cloud.ops("Command"):
        dev = call["payload"]["devices"][0]
        out.append((dev["id"], dev["command"]["name"], dev["command"].get("arguments")))
    return out


def query_offsets(cloud: FakeUtecCloud, start) -> list[float]:
    """Seconds after ``start`` of each Query."""
    return [round((c["at"] - start).total_seconds(), 1) for c in cloud.ops("Query")]


# ---------------------------------------------------------------------------
# Security-command rule: always send
# ---------------------------------------------------------------------------


async def test_lock_sent_when_already_locked(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """Lock on a lock that reports locked: sent, locking until confirmed."""
    assert hass.states.get(FRONT).state == STATE_LOCKED
    await call_lock(hass, "lock", FRONT)
    assert commands(cloud) == [(LOCK1, "lock", None)]
    state = hass.states.get(FRONT)
    assert state.state == STATE_LOCKING  # never optimistic
    assert state.attributes["pending_command"] == "lock"
    await advance(hass, freezer, 1)
    assert hass.states.get(FRONT).state == STATE_LOCKED
    assert len(events) == 1
    data = events[0].data
    assert data["result"] == "confirmed"
    assert data["command"] == "lock"
    assert data["expected"] == "locked"
    assert data["queries"] == 1
    assert data["lock_name"] == "Front Door"
    assert data["device_id"] is not None
    assert hass.states.get(FRONT).attributes["last_command_result"] == "confirmed"
    # The per-lock event entity got it too.
    assert (
        hass.states.get("event.front_door_command_result").attributes["event_type"] == "confirmed"
    )


async def test_unlock_sent_when_already_unlocked(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer
) -> None:
    """Unlock on a lock that reports unlocked: sent."""
    assert hass.states.get(SHOP).state == STATE_UNLOCKED
    await call_lock(hass, "unlock", SHOP)
    assert commands(cloud) == [(LOCK2, "unlock", None)]
    assert hass.states.get(SHOP).state == STATE_UNLOCKING
    await advance(hass, freezer, 1)
    assert hass.states.get(SHOP).state == STATE_UNLOCKED


async def test_lock_in_passage_warns_and_sends(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, caplog
) -> None:
    """Passage mode never blocks a lock command; it only warns."""
    await call_lock(hass, "lock", SHOP)
    assert commands(cloud) == [(LOCK2, "lock", None)]
    assert "Passage mode" in caplog.text


async def test_set_mode_same_mode_is_sent(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """setMode to the cached mode is still sent."""
    assert hass.states.get(FRONT_MODE).state == "normal"
    await call_mode(hass, "normal")
    assert commands(cloud) == [(LOCK1, "setMode", {"mode": 0})]
    assert hass.states.get(FRONT_MODE).attributes["pending_mode"] == "normal"
    await call_mode(hass, "passage")
    await call_mode(hass, "locked")
    assert [c[2] for c in commands(cloud)] == [{"mode": 0}, {"mode": 1}, {"mode": 2}]
    await advance(hass, freezer, 1)
    assert hass.states.get(FRONT_MODE).state == "locked"
    assert hass.states.get(FRONT_MODE).attributes["pending_mode"] is None


async def test_commands_sent_while_offline(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer
) -> None:
    """Cloud offline (state unknown) does not suppress a command."""
    cloud.set_state(LOCK1, "st.healthCheck", "status", "Offline")
    await advance(hass, freezer, 31)
    await runtime(loaded).coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(FRONT).state == STATE_UNKNOWN
    await call_lock(hass, "lock", FRONT)
    await call_lock(hass, "unlock", FRONT)
    await call_mode(hass, "normal")
    assert [c[1] for c in commands(cloud)] == ["lock", "unlock", "setMode"]


async def test_commands_sent_while_stale_and_in_backoff(
    hass: HomeAssistant, freezer, cloud: FakeUtecCloud, credentials
) -> None:
    """Stale (unknown) state and an open circuit never suppress a command."""
    from .conftest import make_entry

    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = runtime(entry).coordinator
    cloud.script("Query", status=503, times=100)
    await advance(hass, freezer, 3600 * 3, step=30)
    assert coordinator._failures >= 5  # circuit open
    assert hass.states.get(FRONT).state == STATE_UNKNOWN
    assert hass.states.get(FRONT).attributes["stale"] is True
    cloud.calls.clear()
    await call_lock(hass, "lock", FRONT)
    await call_lock(hass, "unlock", SHOP)
    assert [c[1] for c in commands(cloud)] == ["lock", "unlock"]


async def test_commands_sent_while_push_unhealthy(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud
) -> None:
    """Push health has no say over commands."""
    from custom_components.utec_locks.push_health import MISS, PushStatus, Registration

    health = runtime(loaded).health
    health.registration = Registration.REGISTERED
    for _ in range(3):
        health._add(MISS, LOCK1, "lock_state")
    assert health.status is PushStatus.UNHEALTHY
    await call_lock(hass, "lock", FRONT)
    assert len(commands(cloud)) == 1


# ---------------------------------------------------------------------------
# Confirmation schedule
# ---------------------------------------------------------------------------


async def test_full_schedule_then_not_confirmed(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events, caplog
) -> None:
    """10 confirmation queries at 1,2,3,4,6,9,14,22,35,56 s; not_confirmed at 90 s."""
    cloud.apply_commands = False  # accepted, never happens (A22)
    from homeassistant.util import dt as dt_util

    start = dt_util.utcnow()
    await call_lock(hass, "unlock", FRONT)
    assert hass.states.get(FRONT).state == STATE_UNLOCKING
    await advance(hass, freezer, 89)
    assert query_offsets(cloud, start) == [1, 2, 3, 4, 6, 9, 14, 22, 35, 56]
    assert all(
        c["payload"]["devices"] == [{"id": LOCK1, "customData": {"token": "secret-custom"}}]
        for c in cloud.ops("Query")
    )
    assert events == []
    assert hass.states.get(FRONT).state == STATE_UNLOCKING
    await advance(hass, freezer, 2)
    assert len(events) == 1
    assert events[0].data["result"] == "not_confirmed"
    assert events[0].data["queries"] == 10
    # Transitional state cleared: back to the last reported state.
    assert hass.states.get(FRONT).state == STATE_LOCKED
    assert "not confirmed" in caplog.text
    await advance(hass, freezer, 300, step=10)
    assert cloud.count("Query") == 10  # nothing after the burst


async def test_no_hourly_cap(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """Every command gets its full burst; there is no hourly budget."""
    cloud.apply_commands = False
    for _ in range(8):
        await call_lock(hass, "unlock", FRONT)
        await advance(hass, freezer, 91, step=1)
    assert cloud.count("Command") == 8
    assert cloud.count("Query") == 80
    assert [e.data["result"] for e in events] == ["not_confirmed"] * 8


async def test_batched_across_locks(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer
) -> None:
    """Two pending locks share one query per tick."""
    cloud.apply_commands = False
    await call_lock(hass, "unlock", FRONT)
    await call_lock(hass, "lock", SHOP)
    await advance(hass, freezer, 60)
    ids = cloud.query_ids()
    assert len(ids) == 10
    assert all(sorted(q) == sorted([LOCK1, LOCK2]) for q in ids)


@pytest.mark.parametrize(
    ("command", "entity", "deferred"),
    [("unlock", FRONT, 20), ("lock", SHOP, 20), ("set_mode", FRONT_MODE, 5)],
)
async def test_deferred_hint_ignored_by_default(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events, command, entity, deferred
) -> None:
    """Default: the full 1/1/1/1/2/3/5/8/13/21 s schedule despite 'result in N s'."""
    from homeassistant.util import dt as dt_util

    cloud.apply_commands = False
    cloud.deferred_seconds = deferred
    start = dt_util.utcnow()
    if command == "set_mode":
        await call_mode(hass, "passage")
    else:
        await call_lock(hass, command, entity)
    await advance(hass, freezer, 89)
    assert query_offsets(cloud, start) == [1, 2, 3, 4, 6, 9, 14, 22, 35, 56]
    assert events == []
    await advance(hass, freezer, 2)
    # The ~90 s unconfirmed timeout still holds.
    assert [e.data["result"] for e in events] == ["not_confirmed"]
    assert events[0].data["queries"] == 10
    assert 89 <= events[0].data["seconds"] <= 91


async def test_deferred_hint_confirms_on_first_tick(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """A deferred 20 s reply no longer delays confirmation to 22 s."""
    cloud.deferred_seconds = 20
    await call_lock(hass, "unlock", FRONT)
    await advance(hass, freezer, 1)
    assert events[0].data["result"] == "confirmed"
    assert events[0].data["seconds"] <= 1.5
    assert hass.states.get(FRONT).state == STATE_UNLOCKED


async def test_deferred_hint_switch_on_skips_early_ticks(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """With the (non-default) switch on, ticks before the hint are skipped."""
    from homeassistant.util import dt as dt_util

    runtime(loaded).commands.honor_deferred = True
    cloud.apply_commands = False
    cloud.deferred_seconds = 20
    start = dt_util.utcnow()
    await call_lock(hass, "unlock", FRONT)
    await advance(hass, freezer, 91)
    assert query_offsets(cloud, start) == [22, 35, 56]
    assert [e.data["result"] for e in events] == ["not_confirmed"]


def test_absolute_ticks() -> None:
    """Pure schedule helper: default ignores the hint."""
    from custom_components.utec_locks.commands import absolute_ticks
    from custom_components.utec_locks.const import HONOR_DEFERRED_HINT

    full = [1, 2, 3, 4, 6, 9, 14, 22, 35, 56]
    assert HONOR_DEFERRED_HINT is False
    assert absolute_ticks() == full
    assert absolute_ticks(deferred=20) == full
    assert absolute_ticks(deferred=5) == full
    assert absolute_ticks(deferred=20, honor_deferred=True) == [22, 35, 56]
    assert absolute_ticks(deferred=5, honor_deferred=True) == [6, 9, 14, 22, 35, 56]


async def test_confirm_disabled_waits_for_push_or_poll(
    hass: HomeAssistant, freezer, cloud: FakeUtecCloud, credentials, events
) -> None:
    """confirm_commands off: no burst; the timeout still fires."""
    from .conftest import make_entry

    entry = make_entry(**{CONF_POLL_INTERVAL: 3600, CONF_CONFIRM_COMMANDS: False})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cloud.calls.clear()
    await call_lock(hass, "lock", FRONT)
    await advance(hass, freezer, 91)
    assert cloud.count("Query") == 0
    assert events[0].data["result"] == "not_confirmed"


async def test_confirmed_by_push(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """A push report confirms before the first tick."""
    from custom_components.utec_locks.api.models import normalize_push

    cloud.apply_commands = False
    await call_lock(hass, "unlock", FRONT)
    push = load_fixture("push_envelope.json")
    runtime(loaded).coordinator.async_handle_push(normalize_push(push))
    await hass.async_block_till_done()
    assert hass.states.get(FRONT).state == STATE_UNLOCKED
    assert events[0].data["result"] == "confirmed"
    assert events[0].data["queries"] == 0
    await advance(hass, freezer, 60)
    assert cloud.count("Query") == 0


async def test_confirmed_by_background_poll(
    hass: HomeAssistant, freezer, setup_entry, cloud: FakeUtecCloud, events
) -> None:
    """With a poll due within 3 s the tick merges into it; the poll confirms."""
    coordinator = runtime(setup_entry).coordinator
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    cloud.apply_commands = False
    # Move to just before the next background poll.
    due = coordinator.next_poll_due
    await advance(hass, freezer, (due - dt_util.utcnow()).total_seconds() - 2)
    cloud.calls.clear()
    await call_lock(hass, "unlock", FRONT)
    cloud.set_state(LOCK1, "st.lock", "lockState", "Unlocked")
    await advance(hass, freezer, 4)
    assert events and events[0].data["result"] == "confirmed"
    assert events[0].data["queries"] == 0  # the confirmation query was merged
    assert cloud.count("Query") == 1
    assert dt_util.utcnow() - due < timedelta(seconds=5)


async def test_battery_or_door_report_never_confirms(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """Only the expected field confirms."""
    from custom_components.utec_locks.api.models import normalize_push

    cloud.apply_commands = False
    await call_lock(hass, "unlock", SHOP)  # SHOP reports unlocked already
    # ...but the pending command needs a report received after it started:
    battery_push = {
        "payload": {
            "devices": [
                {
                    "id": LOCK2,
                    "states": [
                        {"capability": "st.batteryLevel", "name": "level", "value": 3},
                        {"capability": "st.doorSensor", "name": "sensorState", "value": "Open"},
                    ],
                }
            ]
        }
    }
    runtime(loaded).coordinator.async_handle_push(normalize_push(battery_push))
    await hass.async_block_till_done()
    assert events == []
    assert hass.states.get(SHOP).state == STATE_UNLOCKING


async def test_rejected_device_offline(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """Per-device DEVICE_OFFLINE: translated error, no burst."""
    cloud.device_errors[LOCK1] = {"code": "DEVICE_OFFLINE", "message": "offline"}
    with pytest.raises(HomeAssistantError) as err:
        await call_lock(hass, "lock", FRONT)
    assert err.value.translation_key == "device_offline"
    assert events[0].data["result"] == "rejected"
    assert events[0].data["code"] == "DEVICE_OFFLINE"
    assert hass.states.get(FRONT).state == STATE_LOCKED
    await advance(hass, freezer, 60)
    assert cloud.count("Query") == 0


async def test_envelope_rejection(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, events
) -> None:
    """An HTTP-200 error envelope rejects with its code."""
    cloud.script("Command", body=load_fixture("envelope_unknown.json"))
    with pytest.raises(HomeAssistantError) as err:
        await call_lock(hass, "lock", FRONT)
    assert err.value.translation_key == "command_rejected"
    assert events[0].data["code"] == "SOMETHING_NEW"


async def test_invalid_token_refresh_and_retry(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, token_url, events
) -> None:
    """INVALID_TOKEN: refresh once, retry once, success."""
    token_url(load_fixture("token_wrapped.json"))
    cloud.script("Command", body=load_fixture("envelope_invalid_token.json"))
    await call_lock(hass, "unlock", FRONT)
    assert cloud.count("Command") == 2
    assert cloud.ops("Command")[1]["headers"]["Authorization"] == "Bearer wrapped-access"
    await advance(hass, freezer, 1)
    assert events[0].data["result"] == "confirmed"
    assert loaded.data["token"]["access_token"] == "wrapped-access"


async def test_invalid_token_twice_starts_reauth(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, token_url, events
) -> None:
    """INVALID_TOKEN after a refresh: rejected and reauth started."""
    token_url(load_fixture("token_wrapped.json"))
    cloud.script("Command", body=load_fixture("envelope_invalid_token.json"), times=2)
    with pytest.raises(HomeAssistantError) as err:
        await call_lock(hass, "unlock", FRONT)
    assert err.value.translation_key == "reauth_required"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
    assert events[0].data["code"] == "AUTH"


async def test_server_error_retried_once(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """5xx: one retry after 1-2 s, then success."""
    cloud.script("Command", status=502)
    await call_lock(hass, "unlock", FRONT)
    assert cloud.count("Command") == 2
    delay = runtime(loaded).commands.sleep.await_args.args[0]
    assert 1 <= delay <= 2
    await advance(hass, freezer, 1)
    assert events[0].data["result"] == "confirmed"


async def test_outcome_unknown_keeps_confirming(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """5xx twice: command_outcome_unknown, but confirmation still runs."""
    cloud.script("Command", status=500)
    cloud.script("Command", exc=TimeoutError())
    with pytest.raises(HomeAssistantError) as err:
        await call_lock(hass, "unlock", FRONT)
    assert err.value.translation_key == "command_outcome_unknown"
    assert cloud.count("Command") == 2
    assert hass.states.get(FRONT).state == STATE_UNLOCKING
    # The first attempt did reach the lock after all.
    cloud.set_state(LOCK1, "st.lock", "lockState", "Unlocked")
    await advance(hass, freezer, 1)
    assert events[0].data["result"] == "confirmed"
    assert hass.states.get(FRONT).state == STATE_UNLOCKED


async def test_rate_limit_short_retry(hass: HomeAssistant, loaded, cloud: FakeUtecCloud) -> None:
    """429 with Retry-After <= 5 s: wait and retry once."""
    cloud.script("Command", status=429, headers={"Retry-After": "3"})
    await call_lock(hass, "lock", FRONT)
    assert cloud.count("Command") == 2
    assert runtime(loaded).commands.sleep.await_args.args[0] == 3


async def test_rate_limit_long_rejects(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, events
) -> None:
    """429 with a long Retry-After: rejected with the wait, no retry."""
    cloud.script("Command", status=429, headers={"Retry-After": "120"})
    with pytest.raises(HomeAssistantError) as err:
        await call_lock(hass, "lock", FRONT)
    assert err.value.translation_key == "rate_limited"
    assert err.value.translation_placeholders == {"seconds": "120"}
    assert cloud.count("Command") == 1
    assert events[0].data["code"] == "RATE_LIMITED"


async def test_new_command_supersedes(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """A second command replaces the first one's confirmation."""
    cloud.apply_commands = False
    await call_lock(hass, "unlock", FRONT)
    await advance(hass, freezer, 3)
    await call_lock(hass, "lock", FRONT)
    assert hass.states.get(FRONT).attributes["pending_command"] == "lock"
    # The lock already reports locked; the next tick confirms the *lock*.
    await advance(hass, freezer, 1)
    assert [e.data["command"] for e in events] == ["lock"]
    assert events[0].data["result"] == "confirmed"
    await advance(hass, freezer, 120, step=2)
    assert len(events) == 1  # the superseded unlock never reports


async def test_old_report_does_not_confirm_new_command(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """A report that predates the command never confirms it."""
    from homeassistant.util import dt as dt_util

    from custom_components.utec_locks.api.models import LockReport, LockStateValue, Source

    cloud.apply_commands = False
    before = dt_util.utcnow()
    await advance(hass, freezer, 1)
    await call_lock(hass, "lock", FRONT)
    old = LockReport(
        device_id=LOCK1, source=Source.POLL, received_at=before, lock_state=LockStateValue.LOCKED
    )
    runtime(loaded).commands.on_report(old)
    assert events == []


async def test_contradiction_check_one_query(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud, freezer, events
) -> None:
    """A contradicting report within 10 s is held; exactly one query decides."""
    from custom_components.utec_locks.api.models import normalize_push

    await call_lock(hass, "unlock", FRONT)
    await advance(hass, freezer, 1)
    assert events[0].data["result"] == "confirmed"
    assert hass.states.get(FRONT).state == STATE_UNLOCKED
    queries_before = cloud.count("Query")
    stale_push = {
        "payload": {
            "devices": [
                {
                    "id": LOCK1,
                    "states": [{"capability": "st.lock", "name": "lockState", "value": "Locked"}],
                }
            ]
        }
    }
    runtime(loaded).coordinator.async_handle_push(normalize_push(stale_push))
    await hass.async_block_till_done()
    # Held: still unlocked, one deciding query ran, cloud says unlocked.
    assert cloud.count("Query") == queries_before + 1
    assert hass.states.get(FRONT).state == STATE_UNLOCKED
    # A second contradiction in the window is accepted (the decision already ran).
    cloud.set_state(LOCK1, "st.lock", "lockState", "Locked")
    runtime(loaded).coordinator.async_handle_push(normalize_push(stale_push))
    await hass.async_block_till_done()
    assert cloud.count("Query") == queries_before + 1


async def test_command_loop_issue_never_blocks(
    hass: HomeAssistant, loaded, cloud: FakeUtecCloud
) -> None:
    """More than 6 commands in 10 minutes: an issue, every command still sent."""
    for i in range(8):
        await call_lock(hass, "lock" if i % 2 else "unlock", FRONT)
    assert cloud.count("Command") == 8
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_COMMAND_LOOP}_{LOCK1}")
    assert issue is not None


async def test_unknown_lock_raises(hass: HomeAssistant, loaded) -> None:
    """Executor guards against an unknown device id."""
    with pytest.raises(HomeAssistantError):
        await runtime(loaded).commands.async_lock("nope")
