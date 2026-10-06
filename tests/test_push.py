"""Push: HTTPS-only registration, Bearer auth, shapes, health, repairs."""

from __future__ import annotations

from datetime import timedelta
import json
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.lock import LockState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.utec_locks.const import (
    CONF_USE_PUSH,
    DATA_CLOUDHOOK_URL,
    DATA_PUSH_SECRET,
    DATA_PUSH_SECRET_PREVIOUS,
    DOMAIN,
    ISSUE_PUSH_NO_HTTPS,
    ISSUE_PUSH_UNHEALTHY,
)
from custom_components.utec_locks.push import is_acceptable_push_url
from custom_components.utec_locks.push_health import (
    HIT,
    LATE,
    MISS,
    PushStatus,
    Registration,
    compute_status,
)

from .conftest import (
    LOCK1,
    LOCK2,
    PUSH_SECRET,
    WEBHOOK_ID,
    FakeUtecCloud,
    advance,
    load_fixture,
    make_entry,
    runtime,
)

FRONT = "lock.front_door"
HOOK = f"/api/webhook/{WEBHOOK_ID}"
EXTERNAL = "https://shop.example.com"


async def setup_push(hass: HomeAssistant, external_url: str | None = EXTERNAL, **options: Any):
    """Entry with push on and the given external URL."""
    hass.config.external_url = external_url
    entry = make_entry(**{CONF_USE_PUSH: True, **options})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def fake_cloud(create: AsyncMock) -> types.SimpleNamespace:
    """Stand-in for homeassistant.components.cloud (avoids its heavy imports)."""

    class CloudNotAvailable(Exception):
        pass

    def remote_ui_url(hass: HomeAssistant) -> str:
        raise CloudNotAvailable

    return types.SimpleNamespace(
        CloudNotAvailable=CloudNotAvailable,
        async_remote_ui_url=remote_ui_url,
        async_active_subscription=lambda hass: True,
        async_get_or_create_cloudhook=create,
        async_delete_cloudhook=AsyncMock(),
    )


def secret(entry) -> str:
    """Current push secret."""
    return entry.data[DATA_PUSH_SECRET]


async def post(client, entry, body: Any, token: str | None = "current", raw: bool = False):
    """POST to the webhook with a Bearer token."""
    headers = {}
    if token == "current":
        token = secret(entry)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = body if raw else json.dumps(body)
    return await client.post(HOOK, data=data, headers=headers)


# ---------------------------------------------------------------------------
# URL selection and registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://shop.example.com/api/webhook/x", True),
        ("https://hooks.nabu.casa/abc", True),
        ("https://8.8.8.8/api/webhook/x", True),
        ("http://shop.example.com/api/webhook/x", False),
        ("https://192.168.1.5:8123/api/webhook/x", False),
        ("https://10.0.0.1/api/webhook/x", False),
        ("https://homeassistant.local:8123/api/webhook/x", False),
        ("https://localhost/api/webhook/x", False),
        ("https://[::1]/api/webhook/x", False),
        ("https://nas/api/webhook/x", False),
        ("not a url", False),
    ],
)
def test_acceptable_push_url(url: str, ok: bool) -> None:
    """HTTPS with a public host only."""
    assert is_acceptable_push_url(url) is ok


async def test_registers_external_https(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """External HTTPS URL: registered with a fresh secret; old one kept as previous."""
    entry = await setup_push(hass)
    sets = cloud.ops("Set")
    assert len(sets) == 1
    note = sets[0]["payload"]["configure"]["notification"]
    assert note["url"] == f"{EXTERNAL}{HOOK}"
    assert note["access_token"] == secret(entry) != PUSH_SECRET
    assert entry.data[DATA_PUSH_SECRET_PREVIOUS] == PUSH_SECRET
    assert len(secret(entry)) >= 40
    health = runtime(entry).health
    assert health.registration is Registration.REGISTERED
    assert health.status is PushStatus.UNVERIFIED
    assert hass.states.get("sensor.u_tec_account_push_status").state == "unverified"


@pytest.mark.parametrize(
    "external",
    [None, "http://shop.example.com", "https://192.168.1.5:8123", "https://ha.local:8123"],
)
async def test_no_https_url_no_registration(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials, external
) -> None:
    """No HTTPS URL: never registered, repair raised, polling unaffected."""
    entry = await setup_push(hass, external)
    assert cloud.count("Set") == 0
    assert runtime(entry).health.status is PushStatus.NO_URL
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PUSH_NO_HTTPS}_{entry.entry_id}")
    before = cloud.count("Query")
    await advance(hass, freezer, 120)
    assert cloud.count("Query") - before >= 2


async def test_cloudhook_preferred(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Active HA Cloud: the cloudhook is registered (and stored)."""
    hass.config.components.add("cloud")
    fake = fake_cloud(AsyncMock(return_value="https://hooks.nabu.casa/xyz"))
    with patch.dict(sys.modules, {"homeassistant.components.cloud": fake}):
        entry = await setup_push(hass)
    assert (
        cloud.ops("Set")[0]["payload"]["configure"]["notification"]["url"]
        == "https://hooks.nabu.casa/xyz"
    )
    assert entry.data[DATA_CLOUDHOOK_URL] == "https://hooks.nabu.casa/xyz"
    assert runtime(entry).push.url_kind == "cloudhook"
    # Removing the entry deletes the cloudhook.
    with patch.dict(sys.modules, {"homeassistant.components.cloud": fake}):
        await hass.config_entries.async_remove(entry.entry_id)
    fake.async_delete_cloudhook.assert_awaited_once()


async def test_cloudhook_failure_falls_back(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Cloudhook error: external HTTPS URL used instead."""
    hass.config.components.add("cloud")
    fake = fake_cloud(AsyncMock(side_effect=RuntimeError("boom")))
    with patch.dict(sys.modules, {"homeassistant.components.cloud": fake}):
        entry = await setup_push(hass)
    assert runtime(entry).push.url_kind == "external"


async def test_cloud_not_connected_at_startup_retries(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """No URL at startup (cloud still connecting): retried on the ladder."""
    hass.config.components.add("cloud")
    create = AsyncMock(side_effect=[RuntimeError("not connected"), "https://hooks.nabu.casa/xyz"])
    fake = fake_cloud(create)
    with patch.dict(sys.modules, {"homeassistant.components.cloud": fake}):
        entry = await setup_push(hass, external_url=None)
        assert runtime(entry).health.status is PushStatus.NO_URL
        await advance(hass, freezer, 5 * 60 + 1, step=30)
    assert cloud.count("Set") == 1
    assert runtime(entry).push.url_kind == "cloudhook"
    assert runtime(entry).health.status is PushStatus.UNVERIFIED
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PUSH_NO_HTTPS}_{entry.entry_id}")


async def test_registration_failure_retries(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Failure: registration_failed, retry after 5 minutes, then registered."""
    cloud.script("Set", status=500)
    entry = await setup_push(hass)
    assert runtime(entry).health.status is PushStatus.REGISTRATION_FAILED
    assert secret(entry) == PUSH_SECRET  # not rotated on failure
    await advance(hass, freezer, 5 * 60 + 1, step=30)
    assert cloud.count("Set") == 2
    assert runtime(entry).health.status is PushStatus.UNVERIFIED


async def test_daily_rotation(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Re-registered with a new secret every 24 h."""
    entry = await setup_push(hass)
    first = secret(entry)
    await advance(hass, freezer, 24 * 3600 + 60, step=600)
    assert cloud.count("Set") == 2
    assert secret(entry) != first
    assert entry.data[DATA_PUSH_SECRET_PREVIOUS] == first


async def test_push_disabled(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """Push off: no registration, status disabled, button refuses."""
    entry = await setup_push(hass, **{CONF_USE_PUSH: False})
    assert cloud.count("Set") == 0
    assert runtime(entry).health.status is PushStatus.DISABLED
    with pytest.raises(HomeAssistantError):
        await runtime(entry).push.async_press_button()


async def test_button_cooldown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Re-register button: works, then a 5-minute cooldown."""
    from homeassistant.helpers import entity_registry as er

    entry = await setup_push(hass)
    reg = er.async_get(hass).async_get("button.u_tec_account_re_register_push")
    assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    push = runtime(entry).push
    await push.async_press_button()
    assert cloud.count("Set") == 2
    with pytest.raises(HomeAssistantError) as err:
        await push.async_press_button()
    assert err.value.translation_key == "push_register_cooldown"
    await advance(hass, freezer, 301, step=30)
    await push.async_press_button()
    assert cloud.count("Set") == 3


# ---------------------------------------------------------------------------
# Webhook auth and parsing
# ---------------------------------------------------------------------------


async def test_webhook_auth(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """Missing or wrong Bearer: 401 before the body is parsed; current secret: 200."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    body = load_fixture("push_envelope.json")
    assert (await post(client, entry, body, token=None)).status == 401
    assert (await post(client, entry, body, token="wrong")).status == 401
    assert (await post(client, entry, "{not json", token="wrong", raw=True)).status == 401
    resp = await client.post(HOOK, data=json.dumps(body), headers={"Authorization": secret(entry)})
    assert resp.status == 401  # no "Bearer " prefix
    assert hass.states.get(FRONT).state == LockState.LOCKED
    resp = await post(client, entry, body)
    assert resp.status == 200
    assert hass.states.get(FRONT).state == LockState.UNLOCKED
    assert hass.states.get(FRONT).attributes["last_report_source"] == "push"
    usage = runtime(entry).usage.data
    assert usage["push_counters"]["rejected_auth"] == 4
    assert usage["push_counters"]["applied"] == 1


async def test_previous_secret_grace(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """The previous secret works for 10 minutes after rotation, then 401."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    body = load_fixture("push_envelope.json")
    assert (await post(client, entry, body, token=PUSH_SECRET)).status == 200
    await advance(hass, freezer, 9 * 60, step=30)
    assert (await post(client, entry, body, token=PUSH_SECRET)).status == 200
    await advance(hass, freezer, 2 * 60, step=30)
    assert (await post(client, entry, body, token=PUSH_SECRET)).status == 401
    assert (await post(client, entry, body)).status == 200


async def test_body_limits_and_garbage(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """Over 64 KB: 413; invalid JSON: 400; GET: 405; junk JSON: 200 (malformed)."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    big = json.dumps({"pad": "x" * 70_000})
    assert (await post(client, entry, big, raw=True)).status == 413
    assert (await post(client, entry, "{oops", raw=True)).status == 400
    resp = await client.get(HOOK, headers={"Authorization": f"Bearer {secret(entry)}"})
    assert resp.status == 405
    assert (await post(client, entry, {"hello": 1})).status == 200
    assert runtime(entry).usage.data["push_counters"]["malformed"] == 3


@pytest.mark.parametrize(
    "fixture",
    [
        "push_envelope.json",
        "push_bare_list.json",
        "push_payload_list.json",
        "push_mapping_states.json",
        "push_lowercase.json",
    ],
)
async def test_push_shapes_apply_without_query(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
    fixture,
) -> None:
    """Every known shape applies directly; a push never causes a Query."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    queries = cloud.count("Query")
    assert (await post(client, entry, load_fixture(fixture))).status == 200
    await hass.async_block_till_done()
    assert hass.states.get(FRONT).state == LockState.UNLOCKED
    assert cloud.count("Query") == queries


async def test_id_only_push_no_query(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """An id-only push for a known lock changes nothing and queries nothing."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    queries = cloud.count("Query")
    assert (await post(client, entry, load_fixture("push_id_only.json"))).status == 200
    await hass.async_block_till_done()
    assert cloud.count("Query") == queries
    assert hass.states.get(FRONT).state == LockState.LOCKED


@pytest.mark.parametrize("fixture", ["push_sync.json", "push_delete.json"])
async def test_sync_and_delete_trigger_debounced_discovery(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
    fixture,
) -> None:
    """DeviceSync/DeviceDelete: discovery, at most once per 10 minutes."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    before = cloud.count("Discovery")
    for _ in range(5):
        assert (await post(client, entry, load_fixture(fixture))).status == 200
    await hass.async_block_till_done()
    assert cloud.count("Discovery") == before
    await advance(hass, freezer, 10 * 60 + 5, step=30)
    assert cloud.count("Discovery") == before + 1


async def test_unknown_device_push(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """A push for an unknown id requests discovery; nothing is applied."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    body = {
        "payload": {
            "devices": [
                {
                    "id": "ZZ",
                    "states": [{"capability": "st.lock", "name": "lockState", "value": "Locked"}],
                }
            ]
        }
    }
    await advance(hass, freezer, 10 * 60 + 5, step=60)
    before = cloud.count("Discovery")
    assert (await post(client, entry, body)).status == 200
    await hass.async_block_till_done()
    assert cloud.count("Discovery") == before + 1


async def test_rejection_warning(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
    caplog,
) -> None:
    """Ten or more rejections an hour log one warning."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    for _ in range(12):
        await post(client, entry, {}, token="bad")
    assert caplog.text.count("unauthenticated push") == 1


async def test_webhook_unregistered_on_unload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """Unload removes the webhook handler."""
    from homeassistant.components import webhook

    entry = await setup_push(hass)
    assert WEBHOOK_ID in hass.data[webhook.DOMAIN]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert WEBHOOK_ID not in hass.data[webhook.DOMAIN]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("registration", "events", "status"),
    [
        (Registration.DISABLED, [HIT] * 5, PushStatus.DISABLED),
        (Registration.NO_URL, [], PushStatus.NO_URL),
        (Registration.FAILED, [HIT] * 5, PushStatus.REGISTRATION_FAILED),
        (Registration.REGISTERED, [HIT, HIT], PushStatus.UNVERIFIED),
        (Registration.REGISTERED, [HIT, HIT, HIT], PushStatus.DEGRADED),
        (Registration.REGISTERED, [HIT, HIT, HIT, HIT], PushStatus.HEALTHY),
        (Registration.REGISTERED, [MISS, HIT, HIT, HIT, HIT], PushStatus.HEALTHY),
        (Registration.REGISTERED, [HIT, HIT, MISS, HIT, MISS], PushStatus.DEGRADED),
        (Registration.REGISTERED, [HIT, HIT, HIT, MISS, MISS], PushStatus.UNHEALTHY),
        (Registration.REGISTERED, [HIT, HIT, HIT, LATE, LATE], PushStatus.DEGRADED),
        (Registration.REGISTERED, [MISS, MISS, HIT], PushStatus.DEGRADED),
    ],
)
def test_compute_status(registration, events, status) -> None:
    """Pure state machine."""
    assert compute_status(registration, events) is status


async def poll_now(hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry) -> None:
    """Run one background poll (waiting out the floor)."""
    await advance(hass, freezer, 31)
    await runtime(entry).coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_health_hit_miss_late(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """Push first then poll agrees: hit. Poll sees a change with no push: miss. Push after: late."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    health = runtime(entry).health
    # hit
    cloud.set_state(LOCK1, "st.lock", "lockState", "Unlocked")
    await post(client, entry, load_fixture("push_envelope.json"))
    await poll_now(hass, freezer, entry)
    assert [e.kind for e in health.evidence] == [HIT]
    # miss: change seen by poll, no push within 30 s
    cloud.set_state(LOCK1, "st.lock", "lockState", "Locked")
    await poll_now(hass, freezer, entry)
    await advance(hass, freezer, 31)
    assert [e.kind for e in health.evidence] == [HIT, MISS]
    # late: change seen by poll, push arrives within the window
    cloud.set_state(LOCK1, "st.lock", "lockState", "Unlocked")
    await poll_now(hass, freezer, entry)
    await advance(hass, freezer, 5)
    await post(client, entry, load_fixture("push_envelope.json"))
    assert [e.kind for e in health.evidence] == [HIT, MISS, LATE]
    assert 0 < health.evidence[-1].latency <= 30


async def test_unhealthy_reregisters_and_repairs_after_24h(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Unhealthy: auto re-register (once per 6 h); repair after 24 h; healthy clears it."""
    entry = await setup_push(hass)
    health = runtime(entry).health
    sets = cloud.count("Set")
    for _ in range(3):
        health._add(MISS, LOCK1, "lock_state")
    await hass.async_block_till_done()
    assert health.status is PushStatus.UNHEALTHY
    assert cloud.count("Set") == sets + 1
    issue_id = f"{ISSUE_PUSH_UNHEALTHY}_{entry.entry_id}"
    await advance(hass, freezer, 23 * 3600, step=1800)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await advance(hass, freezer, 3600 + 60, step=600)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    # Polling never stopped (one poll per simulated step here).
    from homeassistant.util import dt as dt_util

    last = runtime(entry).coordinator.last_success
    assert dt_util.utcnow() - last < timedelta(minutes=11)
    for _ in range(5):
        health._add(HIT, LOCK1, "lock_state")
    assert health.status is PushStatus.HEALTHY
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_push_sensors(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    cloud: FakeUtecCloud,
    credentials,
    hass_client_no_auth,
) -> None:
    """Account push sensors reflect health and the last push."""
    entry = await setup_push(hass)
    client = await hass_client_no_auth()
    assert hass.states.get("binary_sensor.u_tec_account_push_healthy").state == "unknown"
    await post(client, entry, load_fixture("push_envelope.json"))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.u_tec_account_last_push").state not in ("unknown", "unavailable")
    health = runtime(entry).health
    for _ in range(5):
        health._add(HIT, LOCK2, "lock_state")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.u_tec_account_push_healthy").state == "on"
    assert hass.states.get("sensor.u_tec_account_push_status").state == "healthy"


async def test_use_push_toggle_reloads(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Turning push off reloads the entry and leaves status disabled."""
    entry = await setup_push(hass)
    old = runtime(entry)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_USE_PUSH: False})
    await hass.async_block_till_done()
    assert runtime(entry) is not old
    assert runtime(entry).health.status is PushStatus.DISABLED


async def test_relaxed_interval_then_unhealthy_back_to_base(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cloud: FakeUtecCloud, credentials
) -> None:
    """Relaxed polling ends at the first miss; polling resumes at the base interval."""
    from custom_components.utec_locks.const import CONF_SLOW_POLL_WHEN_PUSH_HEALTHY

    entry = await setup_push(hass, **{CONF_SLOW_POLL_WHEN_PUSH_HEALTHY: True})
    coordinator = runtime(entry).coordinator
    health = runtime(entry).health
    for _ in range(5):
        health._add(HIT, LOCK1, "lock_state")
    assert coordinator.effective_interval == timedelta(seconds=120)
    health._add(MISS, LOCK1, "lock_state")
    assert health.status is PushStatus.DEGRADED
    assert coordinator.effective_interval == timedelta(seconds=30)
