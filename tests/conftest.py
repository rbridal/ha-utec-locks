"""Shared fixtures: FakeUtecCloud (in-memory account) and a set-up entry."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator
import copy
from datetime import timedelta
import json
from pathlib import Path
import time
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.utec_locks.const import (
    CONF_USE_PUSH,
    DATA_PUSH_SECRET,
    DATA_USER_ID,
    DATA_WEBHOOK_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    OAUTH_TOKEN_URL,
)

API_URL = "https://api.u-tec.com/action"
FIXTURES = Path(__file__).parent / "fixtures"
LOCK1 = "AA:BB:CC:00:00:01"
LOCK2 = "AA:BB:CC:00:00:02"
CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"
USER_ID = "02c7badf2b3d44d953b48b579eb9eeb5"
WEBHOOK_ID = "0123456789abcdef0123456789abcdef"
PUSH_SECRET = "push-secret-current"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def deterministic_random(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed the coordinator/command RNGs (startup jitter, backoff jitter)."""
    import random as _random
    import types

    from custom_components.utec_locks import commands as commands_mod, coordinator as coord_mod

    seeded = _random.Random(1234)
    fake = types.SimpleNamespace(Random=lambda *a: _random.Random(1234), uniform=seeded.uniform)
    monkeypatch.setattr(coord_mod, "random", fake)
    monkeypatch.setattr(commands_mod, "random", fake)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in every test."""


class FakeUtecCloud:
    """In-memory U-tec account behind POST /action.

    * ``devices``: discovery records; ``states``: per-device state lists.
    * ``script(name, ...)`` queues a scripted reply for the next ``name`` op.
    * ``apply_commands``: when True a command changes the device state
      immediately (as a well-behaved lock would); when False the command is
      accepted and nothing happens (the field-reported A22 failure).
    * ``calls`` records every request: op name, payload, headers.
    """

    def __init__(self) -> None:
        """Initialize from the fixtures."""
        disc = load_fixture("discovery.json")
        self.devices: list[dict[str, Any]] = disc["payload"]["devices"]
        query = load_fixture("query_normal.json")
        self.states: dict[str, list[dict[str, Any]]] = {
            d["id"]: d["states"] for d in query["payload"]["devices"]
        }
        self.user: dict[str, Any] = load_fixture("user.json")
        self.scripts: dict[str, deque[Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.apply_commands = True
        self.deferred_seconds: int | None = None
        self.device_errors: dict[str, dict[str, str]] = {}

    # -- helpers --------------------------------------------------------

    def set_state(self, device_id: str, capability: str, name: str, value: Any) -> None:
        """Set one state value."""
        states = self.states.setdefault(device_id, [])
        for item in states:
            if (
                item["capability"].lower() == capability.lower()
                and item["name"].lower() == name.lower()
            ):
                item["value"] = value
                return
        states.append({"capability": capability, "name": name, "value": value})

    def get_state(self, device_id: str, name: str) -> Any:
        """Read one state value."""
        for item in self.states.get(device_id, []):
            if item["name"].lower() == name.lower():
                return item["value"]
        return None

    def script(
        self,
        name: str,
        *,
        status: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
        exc: Exception | None = None,
        times: int = 1,
    ) -> None:
        """Queue a scripted reply for the next ``times`` calls of op ``name``."""
        for _ in range(times):
            self.scripts.setdefault(name, deque()).append((status, body, headers, exc))

    def ops(self, name: str | None = None) -> list[dict[str, Any]]:
        """Recorded calls (optionally for one op)."""
        return [c for c in self.calls if name is None or c["name"] == name]

    def query_ids(self) -> list[list[str]]:
        """Device ids of every Query, in order."""
        return [[d["id"] for d in c["payload"]["devices"]] for c in self.ops("Query")]

    def count(self, name: str) -> int:
        """Number of calls of one op."""
        return len(self.ops(name))

    # -- handler --------------------------------------------------------

    def _reply(self, name: str, payload: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            "post",
            API_URL,
            json={
                "header": {"namespace": "x", "name": name, "messageId": "m", "payloadVersion": "1"},
                "payload": payload,
            },
        )

    async def handle(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        """aiohttp mock side effect."""
        header = data["header"]
        name = header["name"]
        payload = data.get("payload") or {}
        self.calls.append(
            {
                "namespace": header["namespace"],
                "name": name,
                "payload": copy.deepcopy(payload),
                "headers": dict(self._last_headers),
                "at": dt_util.utcnow(),
            }
        )
        queue = self.scripts.get(name)
        if queue:
            status, body, headers, exc = queue.popleft()
            if exc is not None:
                raise exc
            if isinstance(body, (dict, list)):
                return AiohttpClientMockResponse(
                    "post", API_URL, status=status, json=body, headers=headers
                )
            return AiohttpClientMockResponse(
                "post", API_URL, status=status, text=body or "", headers=headers
            )
        if name == "Discovery":
            return self._reply(name, {"devices": copy.deepcopy(self.devices)})
        if name == "Get":
            return self._reply(name, copy.deepcopy(self.user["payload"]))
        if name == "Set":
            return self._reply(name, [])
        if name == "Query":
            out = []
            for ref in payload.get("devices", []):
                did = ref["id"]
                if did in self.states:
                    out.append({"id": did, "states": copy.deepcopy(self.states[did])})
            return self._reply(name, {"devices": out})
        if name == "Command":
            out = []
            for ref in payload["devices"]:
                did = ref["id"]
                if did in self.device_errors:
                    out.append({"id": did, "error": self.device_errors[did]})
                    continue
                cmd = ref["command"]
                if self.apply_commands:
                    if cmd["name"] == "lock":
                        self.set_state(did, "st.lock", "lockState", "Locked")
                    elif cmd["name"] == "unlock":
                        self.set_state(did, "st.lock", "lockState", "Unlocked")
                    elif cmd["name"] == "setMode":
                        self.set_state(did, "st.lock", "lockMode", cmd["arguments"]["mode"])
                entry: dict[str, Any] = {"id": did}
                if self.deferred_seconds is not None:
                    entry["states"] = [
                        {
                            "capability": "st.deferredResponse",
                            "name": "seconds",
                            "value": self.deferred_seconds,
                        }
                    ]
                out.append(entry)
            return self._reply(name, {"devices": out})
        raise AssertionError(f"unexpected op {name}")

    _last_headers: dict[str, str] = {}


@pytest.fixture
def cloud(aioclient_mock: AiohttpClientMocker) -> Generator[FakeUtecCloud]:
    """Register the fake cloud on the mocked aiohttp session."""
    fake = FakeUtecCloud()
    original = aioclient_mock.match_request

    async def match_request(method, url, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        fake._last_headers = dict(headers or {})
        return await original(method, url, headers=headers, **kwargs)

    aioclient_mock.match_request = match_request  # type: ignore[method-assign]
    aioclient_mock.post(API_URL, side_effect=fake.handle)
    yield fake


def make_entry(**options: Any) -> MockConfigEntry:
    """A config entry with a valid token."""
    opts = {**DEFAULT_OPTIONS, CONF_USE_PUSH: False, **options}
    return MockConfigEntry(
        domain=DOMAIN,
        title="U-tec (Rob)",
        unique_id=USER_ID,
        version=1,
        minor_version=1,
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "mock-access",
                "refresh_token": "mock-refresh",
                "expires_in": 601200,
                "expires_at": time.time() + 601200,
                "token_type": "Bearer",
            },
            DATA_USER_ID: USER_ID,
            DATA_WEBHOOK_ID: WEBHOOK_ID,
            DATA_PUSH_SECRET: PUSH_SECRET,
        },
        options=opts,
    )


@pytest.fixture
async def credentials(hass: HomeAssistant) -> None:
    """Import the application credential."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential(CLIENT_ID, CLIENT_SECRET), DOMAIN
    )


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Override per test module to change options."""
    return {}


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, cloud: FakeUtecCloud, credentials: None, entry_options: dict[str, Any]
) -> MockConfigEntry:
    """A loaded entry backed by the fake cloud."""
    entry = make_entry(**entry_options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def token_url(aioclient_mock: AiohttpClientMocker) -> Callable[..., None]:
    """Register a token endpoint reply."""

    def _register(body: Any, status: int = 200) -> None:
        aioclient_mock.post(OAUTH_TOKEN_URL, json=body, status=status)

    return _register


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float, step: float = 1.0
) -> None:
    """Move frozen time forward in steps, firing timers as we go."""
    remaining = seconds
    while remaining > 1e-9:
        delta = min(step, remaining)
        freezer.tick(timedelta(seconds=delta))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= delta


def runtime(entry: MockConfigEntry) -> Any:
    """The entry's runtime data."""
    return entry.runtime_data
