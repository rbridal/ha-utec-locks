"""Config flow for U-tec Locks (OAuth via application credentials) — stub.

Real OAuth authorize/token unwrap, account check, reauth, reconfigure, and
options (30 s floor, push HTTPS-only, confirmation toggle) land later.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.helpers import config_entry_oauth2_flow

from homeassistant.config_entries import ConfigEntryState

from .const import CONFLICTING_DOMAIN, DOMAIN, MIN_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle U-tec OpenAPI OAuth2 config flow (scaffold stub)."""

    DOMAIN = DOMAIN
    VERSION = 1
    MINOR_VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra data for authorize URL (scope=openapi; no client_secret)."""
        return {"scope": "openapi"}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Block the flow while the legacy u_tec integration is loaded."""
        if any(
            e.state is ConfigEntryState.LOADED
            for e in self.hass.config_entries.async_entries(CONFLICTING_DOMAIN)
        ):
            return self.async_abort(
                reason="conflicting_integration",
                description_placeholders={"domain": CONFLICTING_DOMAIN},
            )
        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(self, data: dict) -> config_entries.ConfigFlowResult:
        """Create an entry after OAuth (scaffold: not wired to live API yet)."""
        # Production: unwrap token, discover locks, abort on no locks, etc.
        raise NotImplementedError(
            "OAuth create-entry is not implemented in this scaffold pass"
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow stub.

    Polling interval minimum is MIN_POLL_INTERVAL (30 s) and cannot be lowered.
    Push requires HTTPS (Nabu Casa or trusted-CA external URL).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage options (scaffold placeholder)."""
        # Document floor for implementers; form arrives in a later milestone.
        _ = MIN_POLL_INTERVAL
        return self.async_abort(reason="not_implemented")
