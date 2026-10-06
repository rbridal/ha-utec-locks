"""Setup, unload, u_tec block and basic entity creation."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.utec_locks.const import DOMAIN

from .conftest import LOCK1, FakeUtecCloud


async def test_setup_creates_entities(
    hass: HomeAssistant, setup_entry, cloud: FakeUtecCloud
) -> None:
    """Locks, selects and sensors are created; one Discovery and one Query."""
    assert setup_entry.state is ConfigEntryState.LOADED
    assert cloud.count("Discovery") == 1
    assert cloud.count("Query") == 1
    state = hass.states.get("lock.front_door")
    assert state is not None
    assert state.state == "locked"
    assert hass.states.get("lock.shop_door").state == "unlocked"
    assert hass.states.get("select.front_door_lock_mode").state == "normal"
    assert hass.states.get("select.shop_door_lock_mode").state == "passage"
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("lock", DOMAIN, LOCK1)


async def test_user_agent(hass: HomeAssistant, setup_entry, cloud: FakeUtecCloud) -> None:
    """Every request carries the integration User-Agent with the manifest version."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).parent.parent / "custom_components/utec_locks/manifest.json").read_text()
    )
    ua = f"HomeAssistant-utec_locks/{manifest['version']} (+https://github.com/rbridal/ha-utec-locks)"
    assert {c["headers"]["User-Agent"] for c in cloud.calls} == {ua}


async def test_u_tec_blocks_setup(hass: HomeAssistant, cloud: FakeUtecCloud, credentials) -> None:
    """A loaded u_tec entry blocks setup with a repair issue; no API call."""
    from homeassistant.helpers import issue_registry as ir
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import make_entry

    MockConfigEntry(domain="u_tec", state=ConfigEntryState.LOADED).add_to_hass(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, "conflicting_integration")
    assert cloud.calls == []


async def test_setup_retry_on_api_error(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials
) -> None:
    """Discovery failure: setup retried later."""
    from .conftest import make_entry

    cloud.script("Discovery", status=503)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retry_on_first_poll_error(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials
) -> None:
    """First Query failure: setup retried later, no timers left behind."""
    from .conftest import make_entry

    cloud.script("Query", status=503)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials, token_url
) -> None:
    """Auth failure during setup starts reauth."""
    from .conftest import load_fixture, make_entry

    token_url(load_fixture("token_error.json"), status=400)
    cloud.script("Discovery", status=401, times=2)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_missing_credentials_not_ready(hass: HomeAssistant, cloud: FakeUtecCloud) -> None:
    """Deleted application credentials: setup retried, no crash."""
    from homeassistant.setup import async_setup_component

    from .conftest import make_entry

    assert await async_setup_component(hass, "application_credentials", {})
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_and_reload(hass: HomeAssistant, setup_entry, cloud: FakeUtecCloud) -> None:
    """Unload cleanly; reload works."""
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("lock.front_door").state == "unavailable"
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("lock.front_door").state == "locked"


async def test_generates_push_identity(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials
) -> None:
    """Missing webhook id / secret are generated once and stored."""
    from custom_components.utec_locks.const import DATA_PUSH_SECRET, DATA_WEBHOOK_ID

    from .conftest import make_entry

    entry = make_entry()
    data = dict(entry.data)
    data.pop(DATA_WEBHOOK_ID)
    data.pop(DATA_PUSH_SECRET)
    entry = MockConfigEntryFromData(entry, data)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert len(entry.data[DATA_WEBHOOK_ID]) == 32
    assert len(entry.data[DATA_PUSH_SECRET]) >= 40


def MockConfigEntryFromData(entry, data):
    """Clone a MockConfigEntry with new data."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=data,
        options=dict(entry.options),
        version=entry.version,
    )


async def test_remove_device(
    hass: HomeAssistant, setup_entry, cloud: FakeUtecCloud, hass_ws_client
) -> None:
    """Devices can be removed only once their lock left the account."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.setup import async_setup_component

    from custom_components.utec_locks import async_remove_config_entry_device
    from custom_components.utec_locks.helpers import async_get_lock_device

    from .conftest import LOCK2

    assert await async_setup_component(hass, "config", {})
    device = async_get_lock_device(hass, setup_entry.entry_id, LOCK2)
    assert not await async_remove_config_entry_device(hass, setup_entry, device)
    setup_entry.runtime_data.coordinator.removed.add(LOCK2)
    assert await async_remove_config_entry_device(hass, setup_entry, device)
    account = next(
        d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), setup_entry.entry_id)
        if (DOMAIN, f"account_{setup_entry.entry_id}") in d.identifiers
    )
    assert not await async_remove_config_entry_device(hass, setup_entry, account)


async def test_migrate_refuses_future(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials
) -> None:
    """A future entry version is not loaded (downgrade protection)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import make_entry

    base = make_entry()
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(base.data), options=dict(base.options), version=2
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
