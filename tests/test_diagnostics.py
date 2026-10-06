"""Diagnostics are redacted: no tokens, secrets, URLs, ids or names."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)

from custom_components.utec_locks.const import DATA_PUSH_SECRET, DATA_WEBHOOK_ID
from custom_components.utec_locks.helpers import async_get_lock_device

from .conftest import CLIENT_SECRET, LOCK1, USER_ID


async def test_entry_diagnostics_redacted(
    hass: HomeAssistant, hass_client, setup_entry, cloud
) -> None:
    """Nothing sensitive in entry diagnostics."""
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)
    text = json.dumps(diag)
    for secret in (
        "mock-access",
        "mock-refresh",
        CLIENT_SECRET,
        setup_entry.data[DATA_PUSH_SECRET],
        setup_entry.data[DATA_WEBHOOK_ID],
        USER_ID,
        LOCK1,
        "Front Door",
        "secret-custom",
        "Rob",
    ):
        assert secret not in text, secret
    assert diag["coordinator"]["base_interval_s"] == 30
    assert diag["exchanges"]
    assert diag["usage"]["requests_total"] >= 2


async def test_device_diagnostics_redacted(
    hass: HomeAssistant, hass_client, setup_entry, cloud
) -> None:
    """Nothing sensitive in device diagnostics."""
    device = async_get_lock_device(hass, setup_entry.entry_id, LOCK1)
    diag = await get_diagnostics_for_device(hass, hass_client, setup_entry, device)
    text = json.dumps(diag)
    for secret in (LOCK1, "Front Door", "secret-custom", USER_ID):
        assert secret not in text, secret
    assert "lock_state" in text
