"""Offline debounce (two consecutive reports) and per-lock offline-report tracking."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.lock import LockState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.utec_locks.api.models import LockReport, PushMessage, Source
from custom_components.utec_locks.const import CONF_POLL_INTERVAL
from custom_components.utec_locks.state import LockStateStore
from custom_components.utec_locks.usage import store_key

from .conftest import LOCK1, LOCK2, FakeUtecCloud, advance, make_entry, runtime

FRONT = "lock.front_door"  # LOCK1: locked, normal
SHOP = "lock.shop_door"  # LOCK2: unlocked, passage
FRONT_CLOUD = "binary_sensor.front_door_cloud_connection"
SHOP_CLOUD = "binary_sensor.shop_door_cloud_connection"
FRONT_MODE = "select.front_door_lock_mode"
FRONT_COUNT = "sensor.front_door_offline_reports"
FRONT_LAST = "sensor.front_door_last_offline_report"
SHOP_COUNT = "sensor.shop_door_offline_reports"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Background poll out of the way; tests poll by hand."""
    return {CONF_POLL_INTERVAL: 3600}


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry) -> None:
    """One background poll, past the 30 s floor guard."""
    await advance(hass, freezer, 31)
    await runtime(entry).coordinator.async_refresh()
    await hass.async_block_till_done()


def offline(cloud: FakeUtecCloud, device_id: str, value: bool = True) -> None:
    """Make U-tec report a lock offline (or online)."""
    cloud.set_state(device_id, "st.healthCheck", "status", "Offline" if value else "Online")


def state(hass: HomeAssistant, entity_id: str) -> str:
    """Current state string."""
    return hass.states.get(entity_id).state


# ---------------------------------------------------------------------------
# Store unit tests
# ---------------------------------------------------------------------------


def _report(online: bool | None, **kw: Any) -> LockReport:
    return LockReport(LOCK1, NOW, Source.POLL, online=online, **kw)


def test_store_holds_first_offline_report() -> None:
    """First offline report is held; the second applies; online resets."""
    from custom_components.utec_locks.api.models import LockStateValue

    store = LockStateStore()
    store.apply(_report(True, lock_state=LockStateValue.LOCKED))
    held = _report(False, lock_state=LockStateValue.UNLOCKED)
    assert store.hold_offline(held) is True
    snap = store.get(LOCK1)
    assert snap.online is True
    assert snap.lock_state is LockStateValue.LOCKED  # cached offline value not applied
    assert snap.offline_reports == 1
    assert store.last_reports[LOCK1] is held
    # A report without a health status neither counts nor resets.
    assert store.hold_offline(_report(None)) is False
    store.apply(_report(None, battery_level=3))
    assert store.get(LOCK1).offline_reports == 1
    # Second consecutive offline report applies.
    second = _report(False)
    assert store.hold_offline(second) is False
    store.apply(second)
    assert store.get(LOCK1).online is False
    assert store.get(LOCK1).offline_reports == 2
    # Already offline: further offline reports apply directly.
    assert store.hold_offline(_report(False)) is False
    store.apply(_report(False))
    assert store.get(LOCK1).offline_reports == 3
    # Online resets.
    store.apply(_report(True))
    assert store.get(LOCK1).offline_reports == 0
    assert store.get(LOCK1).online is True


def test_store_threshold_one_disables_debounce() -> None:
    """With a threshold of 1 the first offline report applies (old behavior)."""
    store = LockStateStore(offline_confirm_reports=1)
    store.apply(_report(True))
    assert store.hold_offline(_report(False)) is False


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


async def test_single_offline_blip_is_not_shown(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    cloud: FakeUtecCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One offline report then online: no unknown, but the blip is counted."""
    caplog.set_level(logging.INFO, logger="custom_components.utec_locks")
    assert state(hass, FRONT_COUNT) == "0"
    assert state(hass, FRONT_LAST) == STATE_UNKNOWN
    offline(cloud, LOCK1)
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT) == LockState.LOCKED
    assert state(hass, FRONT_MODE) == "normal"
    assert state(hass, FRONT_CLOUD) == STATE_ON
    assert hass.states.get(FRONT_CLOUD).attributes["offline_reports"] == 1
    assert hass.states.get(FRONT).attributes["cloud_status"] == "online"
    assert state(hass, FRONT_COUNT) == "1"
    assert state(hass, FRONT_LAST) != STATE_UNKNOWN
    assert (
        "Front Door: U-tec reported device offline (report 1 of 2 needed; showing unknown: no)"
        in caplog.text
    )
    offline(cloud, LOCK1, False)
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT) == LockState.LOCKED
    assert state(hass, FRONT_CLOUD) == STATE_ON
    assert hass.states.get(FRONT_CLOUD).attributes["offline_reports"] == 0
    assert state(hass, FRONT_COUNT) == "1"  # history kept
    assert "Front Door: U-tec reported device online again after 1 offline report(s)" in (
        caplog.text
    )
    assert LOCK1 not in caplog.text


async def test_two_offline_reports_show_unknown(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    cloud: FakeUtecCloud,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive offline reports: unknown and cloud connection off."""
    caplog.set_level(logging.INFO, logger="custom_components.utec_locks")
    offline(cloud, LOCK1)
    await poll(hass, freezer, setup_entry)
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT) == STATE_UNKNOWN
    assert state(hass, FRONT_MODE) == STATE_UNKNOWN
    assert state(hass, FRONT_CLOUD) == STATE_OFF
    assert hass.states.get(FRONT_CLOUD).attributes["offline_reports"] == 2
    assert state(hass, FRONT_COUNT) == "2"
    assert "(report 2 of 2 needed; showing unknown: yes)" in caplog.text
    offline(cloud, LOCK1, False)
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT) == LockState.LOCKED
    assert state(hass, FRONT_CLOUD) == STATE_ON
    assert "online again after 2 offline report(s)" in caplog.text


async def test_fetch_error_between_offline_reports(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_entry, cloud: FakeUtecCloud
) -> None:
    """Offline, fetch error, offline: fetch errors neither count nor reset; trips."""
    offline(cloud, LOCK1)
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT) == LockState.LOCKED
    cloud.script("Query", status=503)
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT_COUNT) == "1"
    assert hass.states.get(FRONT_CLOUD).attributes["offline_reports"] == 1
    assert state(hass, FRONT) == LockState.LOCKED
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT) == STATE_UNKNOWN
    assert state(hass, FRONT_CLOUD) == STATE_OFF
    assert state(hass, FRONT_COUNT) == "2"


async def test_fetch_errors_do_not_count(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_entry, cloud: FakeUtecCloud
) -> None:
    """API failures (5xx, timeout) are not offline reports."""
    cloud.script("Query", status=503)
    await poll(hass, freezer, setup_entry)
    cloud.script("Query", exc=TimeoutError())
    await poll(hass, freezer, setup_entry)
    assert state(hass, FRONT_COUNT) == "0"
    assert state(hass, SHOP_COUNT) == "0"
    assert state(hass, FRONT) == LockState.LOCKED
    assert hass.states.get(FRONT_CLOUD).attributes["offline_reports"] == 0


async def test_per_device_independence(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_entry, cloud: FakeUtecCloud
) -> None:
    """Each lock has its own counter."""
    offline(cloud, LOCK1)
    await poll(hass, freezer, setup_entry)
    offline(cloud, LOCK1, False)
    offline(cloud, LOCK2)
    await poll(hass, freezer, setup_entry)
    # Front reset by online; Shop at 1 of 2: neither shows offline.
    assert state(hass, FRONT) == LockState.LOCKED
    assert state(hass, SHOP) == LockState.UNLOCKED
    assert hass.states.get(SHOP_CLOUD).attributes["offline_reports"] == 1
    offline(cloud, LOCK1)
    await poll(hass, freezer, setup_entry)
    # Shop's second consecutive report trips it; Front is back at 1 of 2.
    assert state(hass, SHOP) == STATE_UNKNOWN
    assert state(hass, SHOP_CLOUD) == STATE_OFF
    assert state(hass, FRONT) == LockState.LOCKED
    assert state(hass, FRONT_CLOUD) == STATE_ON
    assert state(hass, FRONT_COUNT) == "2"
    assert state(hass, SHOP_COUNT) == "2"


async def test_push_online_resets_and_push_offline_counts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_entry, cloud: FakeUtecCloud
) -> None:
    """A push that says online resets the counter; one that says offline counts."""
    coordinator = runtime(setup_entry).coordinator
    offline(cloud, LOCK1)
    await poll(hass, freezer, setup_entry)
    health = {"capability": "st.healthCheck", "name": "status"}
    coordinator.async_handle_push(
        PushMessage("devicestate", [{"id": LOCK1, "states": [{**health, "value": "Online"}]}])
    )
    await hass.async_block_till_done()
    assert hass.states.get(FRONT_CLOUD).attributes["offline_reports"] == 0
    await poll(hass, freezer, setup_entry)  # still offline in the cloud: 1 of 2 again
    assert state(hass, FRONT) == LockState.LOCKED
    coordinator.async_handle_push(
        PushMessage("devicestate", [{"id": LOCK1, "states": [{**health, "value": "Offline"}]}])
    )
    await hass.async_block_till_done()
    assert state(hass, FRONT) == STATE_UNKNOWN
    assert state(hass, FRONT_COUNT) == "3"


async def test_confirmation_reports_count(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_entry
) -> None:
    """Confirmation-query reports count like polls (online and offline)."""
    coordinator = runtime(setup_entry).coordinator

    def confirm(online: bool) -> None:
        coordinator.async_apply_reports(
            [LockReport(LOCK1, datetime.now(UTC), Source.CONFIRM, online=online)]
        )

    confirm(False)
    await hass.async_block_till_done()
    assert state(hass, FRONT) == LockState.LOCKED
    confirm(True)
    confirm(False)
    await hass.async_block_till_done()
    assert state(hass, FRONT) == LockState.LOCKED
    confirm(False)
    await hass.async_block_till_done()
    assert state(hass, FRONT) == STATE_UNKNOWN
    assert state(hass, FRONT_COUNT) == "3"


async def test_offline_history_persists_and_is_diagnostic(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_storage: dict[str, Any],
    hass_client,
) -> None:
    """Count and last time survive a reload; ids never reach diagnostics."""
    entry = make_entry(**{CONF_POLL_INTERVAL: 3600})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for entity_id in (FRONT_COUNT, FRONT_LAST):
        assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(FRONT_COUNT).attributes["state_class"] == "total_increasing"
    assert hass.states.get(FRONT_LAST).attributes["device_class"] == "timestamp"
    offline(cloud, LOCK1)
    await poll(hass, freezer, entry)
    last = state(hass, FRONT_LAST)
    assert state(hass, FRONT_COUNT) == "1"

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert LOCK1 not in json.dumps(diag)
    assert "offline_reports" not in diag["usage"]
    front = next(lock for lock in diag["locks"] if lock["state"]["offline_reports_total"])
    assert front["state"]["offline_reports_consecutive"] == 1
    assert front["state"]["last_offline_report"] is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[store_key(entry.entry_id)]["data"]
    assert stored["offline_reports"][LOCK1]["count"] == 1
    offline(cloud, LOCK1, False)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert state(hass, FRONT_COUNT) == "1"
    assert state(hass, FRONT_LAST) == last
    assert state(hass, SHOP_COUNT) == "0"
