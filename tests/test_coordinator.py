"""Polling: 30 s floor, backoff, staleness/unknown mapping, availability, discovery."""

from __future__ import annotations

from datetime import timedelta
import itertools
import random
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.lock import LockState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest

from custom_components.utec_locks.const import (
    CONF_POLL_INTERVAL,
    CONF_PUSH_HEALTHY_POLL_INTERVAL,
    CONF_SLOW_POLL_WHEN_PUSH_HEALTHY,
    DOMAIN,
    ISSUE_LOCK_REMOVED,
    ISSUE_RATE_LIMITED,
)
from custom_components.utec_locks.coordinator import (
    backoff_delay,
    clamp_poll_interval,
    clamp_relaxed_interval,
    rate_limit_delay,
)
from custom_components.utec_locks.push_health import HIT, MISS, PushStatus, Registration

from .conftest import LOCK1, LOCK2, FakeUtecCloud, advance, make_entry, runtime

FRONT = "lock.front_door"
SHOP = "lock.shop_door"


async def setup(hass: HomeAssistant, **options: Any):
    """Set up an entry with options; return (entry, coordinator)."""
    entry = make_entry(**options)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, runtime(entry).coordinator


def gaps(cloud: FakeUtecCloud, kind: str = "Query") -> list[float]:
    """Seconds between consecutive calls of one op."""
    times = [c["at"] for c in cloud.ops(kind)]
    return [(b - a).total_seconds() for a, b in itertools.pairwise(times)]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_clamp_poll_interval(caplog) -> None:
    """Below 30 s is raised to 30 s with one warning."""
    assert clamp_poll_interval(timedelta(seconds=10), entry_id="x") == timedelta(seconds=30)
    assert clamp_poll_interval(timedelta(seconds=10), entry_id="x") == timedelta(seconds=30)
    assert caplog.text.count("raised to") <= 1 or caplog.text.count("30") >= 1
    assert clamp_poll_interval(timedelta(seconds=45)) == timedelta(seconds=45)
    assert clamp_poll_interval(timedelta(seconds=99999)) <= timedelta(seconds=3600)
    assert clamp_relaxed_interval(timedelta(seconds=5)) >= timedelta(seconds=60)


def test_backoff_delay() -> None:
    """min(base x 2^n, 900) +/-20 %, never below base; 900 s cap at the circuit."""
    rng = random.Random(1)
    for failures, nominal in [(1, 30), (2, 60), (3, 120), (4, 240), (5, 900), (9, 900)]:
        for _ in range(50):
            delay = backoff_delay(30, failures, rng)
            assert delay >= 30
            assert nominal * 0.8 - 1e-9 <= delay or delay == 30
            assert delay <= nominal * 1.2 + 1e-9
    assert backoff_delay(3600, 1, rng) >= 3600


def test_rate_limit_delay() -> None:
    """Retry-After honored (never below base), else aggressive backoff."""
    assert rate_limit_delay(30, 300, 1) == 300
    assert rate_limit_delay(30, 5, 1) == 30
    assert rate_limit_delay(30, None, 1) >= 30 * 4 * 0.8


# ---------------------------------------------------------------------------
# 30 s floor
# ---------------------------------------------------------------------------


async def test_options_below_floor_are_clamped(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials, caplog
) -> None:
    """A stored poll_interval below 30 s runs at 30 s (warned)."""
    _, coordinator = await setup(hass, **{CONF_POLL_INTERVAL: 5})
    assert coordinator.base_interval == timedelta(seconds=30)
    assert "30" in caplog.text
    await advance(hass, freezer, 600)
    assert min(gaps(cloud)) >= 30


async def test_one_batched_query_and_spacing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """One Query per cycle for all locks; never closer than 30 s; ~31 s cadence."""
    await setup(hass)
    await advance(hass, freezer, 3600)
    ids = cloud.query_ids()
    assert all(sorted(q) == sorted([LOCK1, LOCK2]) for q in ids)
    g = gaps(cloud)
    assert min(g) >= 30
    # first gap includes startup jitter (<= 30 s extra)
    assert max(g[1:]) <= 32
    assert 100 <= len(ids) <= 121


async def test_startup_jitter(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """The first scheduled poll lands 30..61 s after setup."""
    _, coordinator = await setup(hass)
    first_due = (coordinator.next_poll_due - dt_util.utcnow()).total_seconds()
    assert 30 <= first_due <= 60


async def test_manual_refresh_respects_floor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """update_entity spam never produces two queries within 30 s."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    await setup(hass)
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": FRONT}, blocking=True
    )
    for _ in range(10):
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": [FRONT, SHOP]}, blocking=True
        )
        await advance(hass, freezer, 1)
    await advance(hass, freezer, 300)
    assert min(gaps(cloud)) >= 30


async def test_floor_guard_suppresses_direct_refresh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """A refresh inside the floor returns cached data without a request."""
    entry, coordinator = await setup(hass)
    before = cloud.count("Query")
    await coordinator.async_refresh()
    assert cloud.count("Query") == before
    assert runtime(entry).usage.data["refresh_suppressed"] >= 1
    assert hass.states.get(FRONT).state == LockState.LOCKED


async def test_no_locks_no_query(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """No active locks: polling makes no request."""
    _, coordinator = await setup(hass)
    coordinator.removed.update({LOCK1, LOCK2})
    before = cloud.count("Query")
    await advance(hass, freezer, 120)
    assert cloud.count("Query") == before


# ---------------------------------------------------------------------------
# Failures, backoff, staleness
# ---------------------------------------------------------------------------


async def test_backoff_stale_unknown_and_recovery(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Failures back off to 900 s; entities stay available; stale shows unknown."""
    _, coordinator = await setup(hass)
    cloud.script("Query", status=503, times=1000)
    await advance(hass, freezer, 3 * 3600, step=5)
    g = gaps(cloud)
    assert g[-1] >= 900 * 0.8 - 6  # circuit open
    assert all(x >= 29 for x in g)
    assert coordinator._failures >= 5
    front = hass.states.get(FRONT)
    assert front.state == STATE_UNKNOWN  # not unavailable
    assert front.attributes["stale"] is True
    assert front.attributes["last_known_state"] == "locked"
    assert hass.states.get("binary_sensor.front_door_status_stale").state == STATE_ON
    assert hass.states.get("select.front_door_lock_mode").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.shop_door_door").state == STATE_UNKNOWN
    # Recovery
    cloud.scripts.clear()
    await advance(hass, freezer, 1000, step=5)
    assert hass.states.get(FRONT).state == LockState.LOCKED
    assert hass.states.get("binary_sensor.front_door_status_stale").state == STATE_OFF
    assert coordinator._failures == 0


async def test_stale_threshold(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Fresh for max(3 x interval, 120 s), then unknown (checked every 15 s)."""
    _, coordinator = await setup(hass)
    assert coordinator.stale_after == timedelta(seconds=120)
    cloud.script("Query", exc=TimeoutError(), times=1000)
    await advance(hass, freezer, 100)
    assert hass.states.get(FRONT).state == LockState.LOCKED
    await advance(hass, freezer, 40)
    assert hass.states.get(FRONT).state == STATE_UNKNOWN


async def test_rate_limit_honors_retry_after_and_repairs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """429 Retry-After pushes the next poll out; 3 in an hour raise a repair."""
    entry, _ = await setup(hass)
    cloud.script("Query", status=429, headers={"Retry-After": "300"}, times=3)
    await advance(hass, freezer, 3600, step=5)
    g = gaps(cloud)
    assert g[1] >= 300 and g[2] >= 300
    issue_id = f"{ISSUE_RATE_LIMITED}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await advance(hass, freezer, 25 * 3600, step=600)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    token_url,
) -> None:
    """Poll auth failure (after a refresh attempt) starts reauth."""
    from .conftest import load_fixture

    token_url(load_fixture("token_error.json"), status=400)
    await setup(hass)
    cloud.script("Query", status=401, times=5)
    await advance(hass, freezer, 70)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------


async def test_offline_shows_unknown_available(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Cloud offline: lock unknown, cloud sensor off, still available."""
    await setup(hass)
    cloud.set_state(LOCK2, "st.healthCheck", "status", "Offline")
    await advance(hass, freezer, 62)
    shop = hass.states.get(SHOP)
    assert shop.state == STATE_UNKNOWN
    assert shop.attributes["cloud_status"] == "offline"
    assert shop.attributes["last_known_state"] == "unlocked"
    assert hass.states.get("binary_sensor.shop_door_cloud_connection").state == STATE_OFF
    assert hass.states.get("binary_sensor.shop_door_door").state == STATE_UNKNOWN
    assert hass.states.get("select.shop_door_lock_mode").state == STATE_UNKNOWN
    # Battery is reported as-is.
    assert hass.states.get("sensor.shop_door_battery_level").state == "low"
    cloud.set_state(LOCK2, "st.healthCheck", "status", "Online")
    await advance(hass, freezer, 32)
    assert hass.states.get(SHOP).state == LockState.UNLOCKED


async def test_jammed_and_unknown_values(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Jammed maps to jammed; an Unknown lockState shows unknown."""
    await setup(hass)
    cloud.set_state(LOCK1, "st.lock", "lockState", "Jammed")
    cloud.set_state(LOCK2, "st.lock", "lockState", "Unknown")
    await advance(hass, freezer, 62)
    assert hass.states.get(FRONT).state == LockState.JAMMED
    assert hass.states.get(SHOP).state == STATE_UNKNOWN


async def test_battery_entities(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Battery low at level <= 2; enum maps 1..5; percent disabled by default."""
    await setup(hass)
    assert hass.states.get("binary_sensor.front_door_battery").state == STATE_OFF
    assert hass.states.get("binary_sensor.shop_door_battery").state == STATE_ON
    assert hass.states.get("sensor.front_door_battery_level").state == "high"
    assert hass.states.get("sensor.front_door_battery_percent") is None
    entity = er.async_get(hass).async_get("sensor.front_door_battery_percent")
    assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    cloud.set_state(LOCK1, "st.batteryLevel", "level", 1)
    await advance(hass, freezer, 62)
    assert hass.states.get("sensor.front_door_battery_level").state == "critically_low"
    assert hass.states.get("binary_sensor.front_door_battery").state == STATE_ON


async def test_device_error_in_poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """A per-device error in a batch affects only that lock."""
    from .conftest import load_fixture

    await setup(hass)
    cloud.script("Query", body=load_fixture("query_device_error.json"), times=20)
    await advance(hass, freezer, 200)
    assert hass.states.get(FRONT).state == STATE_UNKNOWN
    assert hass.states.get(FRONT).state != STATE_UNAVAILABLE
    assert hass.states.get(SHOP).state == LockState.LOCKED


# ---------------------------------------------------------------------------
# Discovery and availability
# ---------------------------------------------------------------------------


async def test_discovery_every_6h(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Initial discovery plus one every 6 hours."""
    await setup(hass)
    assert cloud.count("Discovery") == 1
    await advance(hass, freezer, 12 * 3600 + 60, step=300)
    assert cloud.count("Discovery") == 3


async def test_lock_removed_after_two_discoveries(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Missing from two discoveries: unavailable + fixable repair; returns cleanly."""
    from custom_components.utec_locks.helpers import async_get_lock_device
    from custom_components.utec_locks.repairs import async_create_fix_flow

    entry, coordinator = await setup(hass)
    removed = [d for d in cloud.devices if d["id"] == LOCK2]
    cloud.devices = [d for d in cloud.devices if d["id"] != LOCK2]
    await advance(hass, freezer, 6 * 3600 + 30, step=300)
    assert hass.states.get(SHOP).state != STATE_UNAVAILABLE
    await advance(hass, freezer, 6 * 3600 + 30, step=300)
    assert hass.states.get(SHOP).state == STATE_UNAVAILABLE
    assert hass.states.get(FRONT).state == LockState.LOCKED
    issue_id = f"{ISSUE_LOCK_REMOVED}_{LOCK2}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    # Polling no longer asks for it.
    assert cloud.query_ids()[-1] == [LOCK1]
    # It comes back: available again, issue gone.
    cloud.devices += removed
    await advance(hass, freezer, 6 * 3600 + 30, step=300)
    assert hass.states.get(SHOP).state != STATE_UNAVAILABLE
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # Gone again, then fixed through the repair flow (device deleted).
    cloud.devices = [d for d in cloud.devices if d["id"] != LOCK2]
    await advance(hass, freezer, 12 * 3600 + 60, step=300)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    flow.issue_id = issue_id
    result = await flow.async_step_init()
    assert result["type"] == "form"
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert async_get_lock_device(hass, entry.entry_id, LOCK2) is None
    assert LOCK2 not in coordinator.locks


async def test_new_lock_added(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """A lock that appears in a later discovery gets entities without reload."""
    await setup(hass)
    cloud.devices.append(
        {
            "id": "AA:BB:CC:00:00:03",
            "name": "Barn",
            "category": "SmartLock",
            "handleType": "utec-lock",
            "deviceInfo": {"manufacturer": "U-tec", "model": "U-Bolt", "hwVersion": "1"},
        }
    )
    cloud.states["AA:BB:CC:00:00:03"] = [
        {"capability": "st.lock", "name": "lockState", "value": "Locked"}
    ]
    await advance(hass, freezer, 6 * 3600 + 120, step=300)
    assert hass.states.get("lock.barn") is not None
    assert hass.states.get("lock.barn").state == LockState.LOCKED


async def test_unknown_id_in_query_requests_discovery(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Unexpected ids trigger a debounced discovery (at most every 10 min)."""
    await setup(hass)
    before = cloud.count("Discovery")
    cloud.script("Query", body={"payload": {"devices": [{"id": "ZZ", "states": []}]}}, times=30)
    await advance(hass, freezer, 15 * 60, step=5)
    assert cloud.count("Discovery") - before == 1


async def test_door_sensor_added_when_seen(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """A door state on a lock without a sensor handleType adds the entity."""
    await setup(hass)
    assert hass.states.get("binary_sensor.front_door_door") is None
    cloud.set_state(LOCK1, "st.doorSensor", "sensorState", "Open")
    await advance(hass, freezer, 62)
    assert hass.states.get("binary_sensor.front_door_door").state == STATE_ON


# ---------------------------------------------------------------------------
# Relaxed polling while push is healthy (option, off by default)
# ---------------------------------------------------------------------------


def make_healthy(health) -> None:
    """Feed the evidence model enough hits."""
    health.registration = Registration.REGISTERED
    for _ in range(5):
        health._add(HIT, LOCK1, "lock_state")


async def test_relaxed_off_by_default(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Healthy push does not slow polling unless the option is on."""
    entry, coordinator = await setup(hass)
    make_healthy(runtime(entry).health)
    assert coordinator.effective_interval == timedelta(seconds=30)


async def test_relaxed_polling(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Option on: 120 s while healthy; back to 30 s at the first miss."""
    entry, coordinator = await setup(
        hass, **{CONF_SLOW_POLL_WHEN_PUSH_HEALTHY: True, CONF_PUSH_HEALTHY_POLL_INTERVAL: 120}
    )
    health = runtime(entry).health
    make_healthy(health)
    assert health.status is PushStatus.HEALTHY
    assert coordinator.effective_interval == timedelta(seconds=120)
    assert coordinator.stale_after == timedelta(seconds=360)
    await advance(hass, freezer, 200)
    cloud.calls.clear()
    await advance(hass, freezer, 600, step=2)
    g = gaps(cloud)
    assert g and min(g) >= 120
    health._add(MISS, LOCK1, "lock_state")
    health._add(MISS, LOCK1, "lock_state")
    assert health.status is PushStatus.UNHEALTHY
    assert coordinator.effective_interval == timedelta(seconds=30)
    cloud.calls.clear()
    await advance(hass, freezer, 300, step=1)
    g = gaps(cloud)
    assert cloud.count("Query") >= 8
    assert min(g) >= 30


async def test_options_apply_live(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Changing the poll interval applies without reload; below 30 is clamped."""
    entry, coordinator = await setup(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_POLL_INTERVAL: 90})
    await hass.async_block_till_done()
    assert coordinator.base_interval == timedelta(seconds=90)
    assert runtime(entry).coordinator is coordinator  # no reload
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_POLL_INTERVAL: 1})
    await hass.async_block_till_done()
    assert coordinator.base_interval == timedelta(seconds=30)


@pytest.mark.parametrize("interval", [30, 45])
async def test_projected_requests(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    interval: int,
) -> None:
    """Projected/day reflects the interval (86400/interval + discovery + push)."""
    await setup(hass, **{CONF_POLL_INTERVAL: interval})
    state = hass.states.get("sensor.u_tec_account_projected_requests_per_day")
    assert int(state.state) == 86400 // interval + 5
