"""Usage meter: persisted totals, 24 h windows, corruption, removal."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.utec_locks.usage import UsageMeter, store_key

from .conftest import FakeUtecCloud, advance, make_entry, runtime


async def test_totals_persist_across_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_storage: dict[str, Any],
) -> None:
    """Totals survive unload/reload via the Store."""
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 125)
    total = runtime(entry).usage.data["requests_total"]
    assert total >= 4
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[store_key(entry.entry_id)]["data"]
    assert stored["requests_total"] == total
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    usage = runtime(entry).usage
    assert usage.data["requests_total"] == total + 2  # + discovery + query
    assert usage.requests_last_24h == total + 2
    assert int(hass.states.get("sensor.u_tec_account_api_requests").state) == total + 2
    assert hass.states.get("sensor.u_tec_account_api_requests").attributes["query"] >= 3


async def test_window_rolls_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """The 24 h window forgets old hours; the total does not."""
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    usage = runtime(entry).usage
    for _ in range(5):
        usage.record_command_sent()
    assert usage.requests_last_hour >= 2
    await hass.config_entries.async_unload(entry.entry_id)
    meter = UsageMeter(hass, entry.entry_id)
    await meter.async_load()
    freezer.tick(25 * 3600)
    assert meter.requests_last_24h == 0
    assert meter.data["requests_total"] >= 2
    assert meter.data["commands_sent"] == 5


async def test_corrupt_store_is_tolerated(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials, hass_storage: dict[str, Any]
) -> None:
    """A corrupt or wrong-shaped usage file starts fresh instead of failing setup."""
    entry = make_entry()
    hass_storage[store_key(entry.entry_id)] = {
        "version": 1,
        "minor_version": 1,
        "key": store_key(entry.entry_id),
        "data": "garbage",
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert runtime(entry).usage.data["requests_total"] == 2


async def test_partial_store_merged(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Missing keys are filled with defaults."""
    hass_storage[store_key("x")] = {
        "version": 1,
        "minor_version": 1,
        "key": store_key("x"),
        "data": {"requests_total": 7},
    }
    meter = UsageMeter(hass, "x")
    await meter.async_load()
    assert meter.data["requests_total"] == 7
    assert meter.data["errors_total"] == 0


async def test_remove_entry_deletes_store(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials, hass_storage: dict[str, Any]
) -> None:
    """Removing the entry deletes its usage file."""
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    assert store_key(entry.entry_id) in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(entry.entry_id) not in hass_storage


async def test_error_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Errors are classified in the errors sensor."""
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cloud.script("Query", status=503)
    cloud.script("Query", exc=TimeoutError())
    cloud.script("Query", status=429)
    await advance(hass, freezer, 1200, step=5)
    attrs = hass.states.get("sensor.u_tec_account_api_errors").attributes
    assert attrs["http_5xx"] == 1
    assert attrs["timeout"] == 1
    assert attrs["http_429"] == 1
    assert int(hass.states.get("sensor.u_tec_account_api_errors").state) == 3
