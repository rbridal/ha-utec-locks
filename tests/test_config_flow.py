"""Config flow: OAuth (wrapped and standard tokens), account id, reauth, options."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qs, urlparse

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.utec_locks.const import (
    CONF_POLL_INTERVAL,
    CONF_USE_PUSH,
    DATA_USER_ID,
    DATA_USER_ID_SOURCE,
    DEFAULT_OPTIONS,
    DOMAIN,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
)

from .conftest import CLIENT_ID, USER_ID, FakeUtecCloud, load_fixture, make_entry

REDIRECT = "https://example.com/auth/external/callback"


@pytest.fixture(autouse=True)
def _host(current_request_with_host: None) -> None:
    """OAuth flows need a current request with a host."""


async def start_oauth(
    hass: HomeAssistant, hass_client_no_auth, flow_id: str, result: dict[str, Any]
) -> None:
    """Check the authorize URL and complete the external step."""
    state = config_entry_oauth2_flow._encode_jwt(
        hass, {"flow_id": flow_id, "redirect_uri": REDIRECT}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    url = urlparse(result["url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == OAUTH_AUTHORIZE_URL
    query = parse_qs(url.query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["scope"] == ["openapi"]
    assert query["response_type"] == ["code"]
    assert "client_secret" not in query
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200


async def run_flow(
    hass: HomeAssistant,
    hass_client_no_auth,
    aioclient_mock: AiohttpClientMocker,
    token: Any,
    token_status: int = 200,
    context: dict[str, Any] | None = None,
    flow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a full user flow through the token exchange."""
    if flow is None:
        flow = await hass.config_entries.flow.async_init(
            DOMAIN, context=context or {"source": config_entries.SOURCE_USER}
        )
    await start_oauth(hass, hass_client_no_auth, flow["flow_id"], flow)
    aioclient_mock.post(OAUTH_TOKEN_URL, json=token, status=token_status)
    return await hass.config_entries.flow.async_configure(flow["flow_id"])


@pytest.mark.parametrize(
    ("token", "access"),
    [("token_wrapped.json", "wrapped-access"), ("token_standard.json", "std-access")],
)
async def test_full_flow(
    hass: HomeAssistant,
    hass_client_no_auth,
    aioclient_mock,
    cloud: FakeUtecCloud,
    credentials,
    token,
    access,
) -> None:
    """Wrapped and standard token responses both create the entry."""
    result = await run_flow(hass, hass_client_no_auth, aioclient_mock, load_fixture(token))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "U-tec (Rob)"
    data = result["data"]
    assert data["token"]["access_token"] == access
    assert data[DATA_USER_ID] == USER_ID
    assert data[DATA_USER_ID_SOURCE] == "user_get"
    assert result["result"].unique_id == USER_ID
    assert result["options"] == DEFAULT_OPTIONS
    assert cloud.ops("Get")[0]["headers"]["Authorization"] == f"Bearer {access}"
    # Token request carried the client credentials.
    token_calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == OAUTH_TOKEN_URL]
    assert token_calls
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED


async def test_token_error(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, cloud, credentials
) -> None:
    """A wrapped error response aborts the flow."""
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_error.json")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in ("oauth_error", "oauth_failed", "oauth_unauthorized")


async def test_no_locks(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, cloud: FakeUtecCloud, credentials
) -> None:
    """Account without locks: abort no_locks."""
    cloud.devices = [
        d
        for d in cloud.devices
        if d["category"] != "SmartLock" and "lock" not in d.get("handleType", "")
    ]
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_locks"


@pytest.mark.parametrize(("status", "reason"), [(500, "cannot_connect"), (401, "invalid_auth")])
async def test_discovery_errors(
    hass: HomeAssistant,
    hass_client_no_auth,
    aioclient_mock,
    cloud: FakeUtecCloud,
    credentials,
    status,
    reason,
) -> None:
    """Discovery failure aborts with a clear reason."""
    cloud.script("Discovery", status=status, times=2)
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json")
    )
    assert result["reason"] == reason


async def test_user_get_auth_error(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, cloud: FakeUtecCloud, credentials
) -> None:
    """User Get auth failure: invalid_auth."""
    cloud.script("Get", status=401, times=2)
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json")
    )
    assert result["reason"] == "invalid_auth"


async def test_user_get_fallback(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, cloud: FakeUtecCloud, credentials
) -> None:
    """User Get unavailable: unique id from the client id hash."""
    cloud.script("Get", status=500)
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    expected = "client_" + hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:24]
    assert result["result"].unique_id == expected
    assert result["data"][DATA_USER_ID_SOURCE] == "client_id_hash"
    assert result["title"] == "U-tec"


async def test_duplicate(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, cloud, credentials
) -> None:
    """The same account twice: already_configured."""
    make_entry().add_to_hass(hass)
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json")
    )
    assert result["reason"] == "already_configured"


async def test_u_tec_blocks_flow(hass: HomeAssistant, cloud, credentials) -> None:
    """The flow refuses to start while u_tec is loaded."""
    legacy = MockConfigEntry(domain="u_tec", state=ConfigEntryState.LOADED)
    legacy.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "conflicting_integration"


async def test_u_tec_not_loaded_does_not_block(hass: HomeAssistant, cloud, credentials) -> None:
    """A u_tec entry that is not loaded does not block."""
    MockConfigEntry(domain="u_tec", state=ConfigEntryState.NOT_LOADED).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP


async def test_missing_credentials(hass: HomeAssistant) -> None:
    """Without application credentials the flow asks for them."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "application_credentials", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


async def test_reauth_same_account(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, setup_entry, cloud
) -> None:
    """Reauth on the same account updates the token and reloads."""
    entry = setup_entry
    flow = await entry.start_reauth_flow(hass)
    assert flow["step_id"] == "reauth_confirm"
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json"), flow=flow
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"]["access_token"] == "wrapped-access"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_wrong_account(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, setup_entry, cloud: FakeUtecCloud
) -> None:
    """Reauth with a different account aborts and changes nothing."""
    entry = setup_entry
    cloud.user = {"payload": {"user": {"id": "someone-else", "firstName": "Eve"}}}
    flow = await entry.start_reauth_flow(hass)
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json"), flow=flow
    )
    assert result["reason"] == "wrong_account"
    assert entry.data["token"]["access_token"] == "mock-access"


async def test_reauth_user_get_failure(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, setup_entry, cloud: FakeUtecCloud
) -> None:
    """Reauth never guesses the account: User Get failure aborts cannot_connect."""
    cloud.script("Get", status=503)
    flow = await setup_entry.start_reauth_flow(hass)
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_wrapped.json"), flow=flow
    )
    assert result["reason"] == "cannot_connect"


async def test_reconfigure(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, setup_entry, cloud
) -> None:
    """Reconfigure re-runs OAuth for the same account."""
    flow = await setup_entry.start_reconfigure_flow(hass)
    result = await run_flow(
        hass, hass_client_no_auth, aioclient_mock, load_fixture("token_standard.json"), flow=flow
    )
    assert result["reason"] == "reconfigure_successful"
    assert setup_entry.data["token"]["access_token"] == "std-access"


async def test_options_flow(hass: HomeAssistant, setup_entry) -> None:
    """Options save as ints; the schema rejects values below 30 s."""
    import voluptuous as vol

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {**DEFAULT_OPTIONS, CONF_POLL_INTERVAL: 10},
        )
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**DEFAULT_OPTIONS, CONF_POLL_INTERVAL: 45.0, CONF_USE_PUSH: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options[CONF_POLL_INTERVAL] == 45
    assert isinstance(setup_entry.options[CONF_POLL_INTERVAL], int)
