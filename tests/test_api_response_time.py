"""API response-time sensors: last, rolling average, failures, token refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import time
import types
from typing import Any

from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.utec_locks.api import client as client_mod
from custom_components.utec_locks.api.client import RequestRecord, UtecClient
from custom_components.utec_locks.api.errors import UtecTransportError
from custom_components.utec_locks.const import LATENCY_WINDOW
from custom_components.utec_locks.latency import LatencyTracker

from .conftest import FakeUtecCloud, load_fixture, make_entry, runtime

LAST = "sensor.u_tec_account_last_api_response_time"
AVERAGE = "sensor.u_tec_account_average_api_response_time"
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def rec(ms: float, outcome: str = "ok", kind: str = "query", **kw: Any) -> RequestRecord:
    """A request record taking ``ms`` milliseconds."""
    return RequestRecord(
        kind=kind,
        namespace=kw.pop("namespace", "Uhome.Device"),
        name=kw.pop("name", "Query"),
        started=kw.pop("started", T0),
        latency=ms / 1000,
        http_status=kw.pop("http_status", 200 if outcome == "ok" else None),
        outcome=outcome,
        **kw,
    )


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def test_tracker_empty() -> None:
    """No requests yet: everything unknown."""
    tracker = LatencyTracker()
    assert tracker.last_ms is None
    assert tracker.average_ms is None
    assert tracker.max_ms is None
    assert tracker.last_attributes() == {}
    assert tracker.average_attributes()["sample_count"] == 0
    assert tracker.average_attributes()["last_measured_at"] is None


def test_tracker_last_average_max() -> None:
    """Last is the newest; average/min/max over the window."""
    tracker = LatencyTracker()
    for ms in (100, 300, 200):
        tracker.add(rec(ms))
    assert tracker.last_ms == 200
    assert tracker.average_ms == 200
    assert tracker.max_ms == 300
    assert tracker.min_ms == 100
    attrs = tracker.last_attributes()
    assert attrs == {
        "request_type": "query",
        "operation": "Uhome.Device/Query",
        "outcome": "ok",
        "http_status": 200,
        "measured_at": (T0 + timedelta(milliseconds=200)).isoformat(),
    }


def test_tracker_window_rolls() -> None:
    """Only the last LATENCY_WINDOW requests are averaged."""
    tracker = LatencyTracker()
    assert tracker.window == LATENCY_WINDOW == 20
    for _ in range(10):
        tracker.add(rec(5000))
    for _ in range(LATENCY_WINDOW):
        tracker.add(rec(100))
    assert tracker.average_ms == 100
    assert tracker.max_ms == 100
    assert tracker.sample_count == LATENCY_WINDOW


def test_tracker_failures_excluded_from_average() -> None:
    """Timeouts/connection errors: last shows them, average and max skip them."""
    tracker = LatencyTracker()
    tracker.add(rec(100))
    tracker.add(rec(300, outcome="http_5xx", http_status=503))  # a response: counted
    tracker.add(rec(15000, outcome="timeout"))
    assert tracker.last_ms == 15000
    assert tracker.last_attributes()["outcome"] == "timeout"
    assert tracker.average_ms == 200
    assert tracker.max_ms == 300
    attrs = tracker.average_attributes()
    assert attrs["sample_count"] == 2
    assert attrs["failed_requests"] == 1
    assert attrs["window_requests"] == 20


def test_tracker_all_failed() -> None:
    """A window of failures has no average."""
    tracker = LatencyTracker(window=2)
    tracker.add(rec(100))
    tracker.add(rec(20, outcome="connection"))
    tracker.add(rec(15000, outcome="timeout"))
    assert tracker.average_ms is None
    assert tracker.failed_count == 2
    assert tracker.last_ms == 15000


# ---------------------------------------------------------------------------
# Client timing
# ---------------------------------------------------------------------------


class _Tokens:
    async def async_get_access_token(self) -> str:
        return "t"

    async def async_force_refresh(self) -> str:
        return "t"


async def test_client_times_each_request(
    hass: HomeAssistant, cloud: FakeUtecCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every HTTP request is timed with the monotonic clock, failures too."""
    ticks = iter([10.0, 10.25, 20.0, 20.5])
    monkeypatch.setattr(client_mod, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    tracker = LatencyTracker()
    client = UtecClient(
        async_get_clientsession(hass), _Tokens(), user_agent="ua", observer=tracker.add
    )
    await client.async_discover()
    assert tracker.last_ms == 250
    assert tracker.last_attributes()["request_type"] == "discovery"
    assert tracker.last_attributes()["operation"] == "Uhome.Device/Discovery"
    cloud.script("Get", exc=TimeoutError())
    with pytest.raises(UtecTransportError):
        await client.async_get_user()
    assert tracker.last_ms == 500
    assert tracker.last_attributes()["outcome"] == "timeout"
    assert tracker.average_ms == 250
    assert tracker.failed_count == 1


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


async def test_sensors_created_as_diagnostics(
    hass: HomeAssistant, setup_entry, cloud: FakeUtecCloud
) -> None:
    """Both sensors exist on the account device with the right metadata."""
    registry = er.async_get(hass)
    account = registry.async_get("sensor.u_tec_account_api_requests").device_id
    for entity_id in (LAST, AVERAGE):
        reg = registry.async_get(entity_id)
        assert reg is not None
        assert reg.entity_category is EntityCategory.DIAGNOSTIC
        assert reg.device_id == account
        st = hass.states.get(entity_id)
        assert st.attributes["device_class"] == "duration"
        assert st.attributes["unit_of_measurement"] == UnitOfTime.MILLISECONDS
        assert st.attributes["state_class"] == "measurement"
        assert float(st.state) >= 0
    # Setup ran discovery + query.
    last = hass.states.get(LAST).attributes
    assert last["request_type"] == "query"
    assert last["operation"] == "Uhome.Device/Query"
    assert last["outcome"] == "ok"
    assert last["http_status"] == 200
    avg = hass.states.get(AVERAGE).attributes
    assert avg["sample_count"] == 2
    assert avg["failed_requests"] == 0
    assert avg["window_requests"] == 20
    assert "max_ms" in avg


async def test_sensors_update_on_failure(
    hass: HomeAssistant, freezer, setup_entry, cloud: FakeUtecCloud
) -> None:
    """A timed-out poll shows in Last (outcome timeout) and as a failed request."""
    from .conftest import advance

    cloud.script("Query", exc=TimeoutError())
    await advance(hass, freezer, 65)
    usage = runtime(setup_entry).usage
    assert usage.latency.failed_count == 1
    avg = hass.states.get(AVERAGE).attributes
    assert avg["failed_requests"] == 1
    assert avg["sample_count"] >= 2
    outcomes = [s["outcome"] for s in usage.latency.snapshot()["samples"]]
    assert "timeout" in outcomes


async def test_values_follow_tracker(hass: HomeAssistant, setup_entry, cloud) -> None:
    """Sensor states mirror the tracker after a request."""
    usage = runtime(setup_entry).usage
    usage.record_request(rec(420))
    await hass.async_block_till_done()
    assert hass.states.get(LAST).state == "420"
    assert int(hass.states.get(AVERAGE).state) == usage.latency.average_ms


async def test_token_refresh_is_timed_not_counted(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials, token_url
) -> None:
    """An expired token is refreshed at setup: timed as token_refresh, not an /action request."""
    token_url(load_fixture("token_wrapped.json"))
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "token": {**entry.data["token"], "expires_at": time.time() - 10}},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    usage = runtime(entry).usage
    samples = usage.latency.snapshot()["samples"]
    assert samples[0]["request_type"] == "token_refresh"
    assert samples[0]["operation"] == "OAuth/token"
    assert samples[0]["outcome"] == "ok"
    assert usage.data["token_refreshes"] == 1
    assert usage.data["requests_total"] == 2  # discovery + query only
    assert "token_refresh" not in usage.data["requests_by_kind"]


async def test_provider_records_outcomes(hass: HomeAssistant) -> None:
    """The token provider reports ok, auth, transient and transport failures."""
    from aiohttp import ClientConnectionError
    from homeassistant.exceptions import (
        OAuth2TokenRequestReauthError,
        OAuth2TokenRequestTransientError,
    )

    from custom_components.utec_locks.api.errors import UtecAuthError, UtecTokenError
    from custom_components.utec_locks.auth import OAuthTokenProvider

    class _Impl:
        def __init__(self) -> None:
            self.result: Any = {"access_token": "new"}

        async def async_refresh_token(self, token: dict) -> dict:
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class _Session:
        def __init__(self) -> None:
            self.token = {"access_token": "old"}
            self.implementation = _Impl()

    entry = make_entry()
    entry.add_to_hass(hass)
    session = _Session()
    records: list[RequestRecord] = []
    provider = OAuthTokenProvider(hass, entry, session, observer=records.append)  # type: ignore[arg-type]

    await provider.async_force_refresh()
    assert (records[-1].kind, records[-1].outcome, records[-1].http_status) == (
        "token_refresh",
        "ok",
        200,
    )

    def _err(cls: type, status: int) -> Exception:
        return cls(request_info=None, history=(), status=status, domain="utec_locks")

    for exc, outcome, status in (
        (_err(OAuth2TokenRequestReauthError, 400), "auth", 400),
        (_err(OAuth2TokenRequestTransientError, 503), "http_5xx", 503),
        (_err(OAuth2TokenRequestTransientError, 429), "http_429", 429),
        (UtecTokenError("invalid_grant", reauth=True), "auth", 200),
        (UtecTokenError("weird", reauth=False), "envelope", 200),
        (TimeoutError(), "timeout", None),
        (ClientConnectionError(), "connection", None),
        (RuntimeError("x"), "error", None),
    ):
        session.implementation.result = exc
        with pytest.raises((UtecAuthError, UtecTransportError, RuntimeError)):
            await provider.async_force_refresh()
        assert (records[-1].outcome, records[-1].http_status) == (outcome, status), exc
    await hass.async_block_till_done()


async def test_diagnostics_include_response_times(
    hass: HomeAssistant, hass_client, setup_entry, cloud
) -> None:
    """Diagnostics carry the timing window (no tokens)."""
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)
    timing = diag["api_response_time"]
    assert timing["sample_count"] == 2
    assert {s["request_type"] for s in timing["samples"]} == {"discovery", "query"}
    assert "mock-access" not in str(timing)
