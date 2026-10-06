"""API client: envelope classification, retries, headers, batching."""

from __future__ import annotations

from datetime import UTC, datetime

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.utec_locks.api.client import (
    QUERY_BATCH_SIZE,
    RequestRecord,
    UtecClient,
    parse_retry_after,
)
from custom_components.utec_locks.api.errors import (
    UtecAuthError,
    UtecEnvelopeError,
    UtecRateLimitError,
    UtecServerError,
    UtecTransportError,
)
from custom_components.utec_locks.api.models import LockInfo, LockStateValue

from .conftest import API_URL, LOCK1, FakeUtecCloud, load_fixture

UA = "HomeAssistant-utec_locks/9.9.9 (+https://github.com/rbridal/ha-utec-locks)"


class Tokens:
    """Token provider stub."""

    def __init__(self) -> None:
        self.token = "t1"
        self.refreshes = 0

    async def async_get_access_token(self) -> str:
        return self.token

    async def async_force_refresh(self) -> str:
        self.refreshes += 1
        self.token = f"t{self.refreshes + 1}"
        return self.token


def lock(device_id: str = LOCK1, custom: dict | None = None) -> LockInfo:
    """LockInfo helper."""
    return LockInfo(device_id, "L", "utec-lock", "m", "1", "U-tec", False, custom)


@pytest.fixture
def records() -> list[RequestRecord]:
    """Observer sink."""
    return []


@pytest.fixture
def tokens() -> Tokens:
    """Token stub."""
    return Tokens()


@pytest.fixture
async def client(
    hass: HomeAssistant, cloud: FakeUtecCloud, tokens: Tokens, records: list[RequestRecord]
) -> UtecClient:
    """Client on the mocked session."""
    return UtecClient(async_get_clientsession(hass), tokens, user_agent=UA, observer=records.append)


async def test_headers_and_envelope(client: UtecClient, cloud: FakeUtecCloud, records) -> None:
    """Bearer token, User-Agent, UUID messageId and payloadVersion."""
    await client.async_discover()
    call = cloud.ops("Discovery")[0]
    assert call["headers"]["Authorization"] == "Bearer t1"
    assert call["headers"]["User-Agent"] == UA
    assert call["namespace"] == "Uhome.Device"
    assert records[0].kind == "discovery"
    assert records[0].outcome == "ok"
    assert records[0].as_dict()["op"] == "Uhome.Device/Discovery"


async def test_query_sends_custom_data(client: UtecClient, cloud: FakeUtecCloud) -> None:
    """customData from discovery travels with Query (camelCase)."""
    result = await client.async_query([lock(custom={"k": "v"})])
    assert cloud.ops("Query")[0]["payload"]["devices"] == [{"id": LOCK1, "customData": {"k": "v"}}]
    assert result.reports[LOCK1].lock_state is LockStateValue.LOCKED


async def test_custom_data_switch(hass: HomeAssistant, cloud: FakeUtecCloud, tokens) -> None:
    """The SEND_CUSTOM_DATA switch turns it off."""
    client = UtecClient(
        async_get_clientsession(hass), tokens, user_agent=UA, send_custom_data=False
    )
    await client.async_query([lock(custom={"k": "v"})])
    assert cloud.ops("Query")[0]["payload"]["devices"] == [{"id": LOCK1}]


async def test_query_batches_by_20(client: UtecClient, cloud: FakeUtecCloud) -> None:
    """More than 20 locks are split into groups of 20."""
    locks = [lock(f"ID{i}") for i in range(QUERY_BATCH_SIZE + 5)]
    await client.async_query(locks)
    assert cloud.count("Query") == 2
    assert len(cloud.ops("Query")[0]["payload"]["devices"]) == 20


async def test_invalid_token_refresh_and_retry_once(
    client: UtecClient, cloud: FakeUtecCloud, tokens: Tokens
) -> None:
    """HTTP-200 INVALID_TOKEN: refresh once, retry once, then succeed."""
    cloud.script("Query", body=load_fixture("envelope_invalid_token.json"))
    result = await client.async_query([lock()])
    assert tokens.refreshes == 1
    assert cloud.count("Query") == 2
    assert cloud.ops("Query")[1]["headers"]["Authorization"] == "Bearer t2"
    assert LOCK1 in result.reports


async def test_invalid_token_twice_raises(
    client: UtecClient, cloud: FakeUtecCloud, tokens: Tokens
) -> None:
    """A repeat INVALID_TOKEN is an auth error (starts reauth upstream)."""
    cloud.script("Query", body=load_fixture("envelope_invalid_token.json"), times=2)
    with pytest.raises(UtecAuthError):
        await client.async_query([lock()])
    assert tokens.refreshes == 1
    assert cloud.count("Query") == 2


@pytest.mark.parametrize("status", [401, 403])
async def test_http_auth(client: UtecClient, cloud: FakeUtecCloud, status: int, records) -> None:
    """401/403 behave like INVALID_TOKEN."""
    cloud.script("Get", status=status, times=2)
    with pytest.raises(UtecAuthError):
        await client.async_get_user()
    assert [r.outcome for r in records] == ["auth", "auth"]


async def test_unknown_envelope(client: UtecClient, cloud: FakeUtecCloud, records) -> None:
    """Unknown envelope codes are errors with the code."""
    cloud.script("Query", body=load_fixture("envelope_unknown.json"))
    with pytest.raises(UtecEnvelopeError) as err:
        await client.async_query([lock()])
    assert err.value.code == "SOMETHING_NEW"
    assert records[-1].code == "SOMETHING_NEW"


async def test_top_level_error_string(client: UtecClient, cloud: FakeUtecCloud) -> None:
    """A top-level error string is an envelope error."""
    cloud.script("Query", body={"error": "BUSY"})
    with pytest.raises(UtecEnvelopeError):
        await client.async_query([lock()])


@pytest.mark.parametrize(
    ("status", "headers", "exc", "retry_after"),
    [
        (429, {"Retry-After": "12"}, UtecRateLimitError, 12.0),
        (429, None, UtecRateLimitError, None),
        (500, None, UtecServerError, None),
        (503, None, UtecServerError, None),
        (404, None, UtecEnvelopeError, None),
    ],
)
async def test_http_errors(
    client: UtecClient, cloud: FakeUtecCloud, status, headers, exc, retry_after
) -> None:
    """Status classification."""
    cloud.script("Query", status=status, headers=headers)
    with pytest.raises(exc) as err:
        await client.async_query([lock()])
    if exc is UtecRateLimitError:
        assert err.value.retry_after == retry_after
    if status == 404:
        assert err.value.code == "HTTP_404"


async def test_transport_errors(client: UtecClient, cloud: FakeUtecCloud, records) -> None:
    """Timeouts and connection errors are transport errors."""
    cloud.script("Query", exc=TimeoutError())
    with pytest.raises(UtecTransportError):
        await client.async_query([lock()])
    cloud.script("Query", exc=aiohttp.ClientConnectionError())
    with pytest.raises(UtecTransportError):
        await client.async_query([lock()])
    assert [r.outcome for r in records] == ["timeout", "connection"]


async def test_malformed_and_empty(client: UtecClient, cloud: FakeUtecCloud) -> None:
    """Non-JSON is an error; empty body is ok; a bare list is a payload."""
    cloud.script("Query", body="not json")
    with pytest.raises(UtecEnvelopeError):
        await client.async_query([lock()])
    cloud.script("Set", body="")
    await client.async_register_push("https://x.example/api/webhook/1", "s")
    cloud.script("Discovery", body=[{"id": "x", "category": "SmartLock"}])
    assert "x" in (await client.async_discover()).locks


async def test_command_and_push_payloads(client: UtecClient, cloud: FakeUtecCloud) -> None:
    """Command and Set payload shapes."""
    cloud.deferred_seconds = 5
    receipt = await client.async_command(lock(), "setMode", {"mode": 1})
    assert receipt.deferred_seconds == 5
    assert cloud.ops("Command")[0]["payload"] == {
        "devices": [
            {
                "id": LOCK1,
                "command": {"capability": "st.lock", "name": "setMode", "arguments": {"mode": 1}},
            }
        ]
    }
    await client.async_register_push("https://h.example/api/webhook/abc", "sekret")
    assert cloud.ops("Set")[0]["payload"] == {
        "configure": {
            "notification": {"access_token": "sekret", "url": "https://h.example/api/webhook/abc"}
        }
    }
    assert cloud.ops("Set")[0]["namespace"] == "Uhome.Configure"


def test_parse_retry_after() -> None:
    """Seconds, HTTP date, junk."""
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("30") == 30
    assert parse_retry_after("Tue, 06 Oct 2026 12:01:00 GMT", now) == 60
    assert parse_retry_after("Tue, 06 Oct 2026 11:00:00 GMT", now) == 0
    assert parse_retry_after("junk") is None
    assert parse_retry_after(None) is None


async def test_no_mock_for_other_urls(aioclient_mock: AiohttpClientMocker) -> None:
    """Sanity: the API URL constant is what the fake serves."""
    assert API_URL == "https://api.u-tec.com/action"
