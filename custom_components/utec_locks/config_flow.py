"""Config flow for U-tec Locks (OAuth via application credentials).

* Standard HA application-credentials prompt for each user's own U-tec
  Client ID / Secret; authorize URL gets ``scope=openapi`` and never the secret.
* After the token: ``Uhome.User / Get`` gives the account id used as the
  entry's unique id (fallback: SHA-256 of the client id), then Discovery
  (abort ``no_locks`` when the account exposes no locks).
* Reauth and reconfigure re-run OAuth and must land on the same account.
* Blocked while the legacy ``u_tec`` integration is loaded.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration
import voluptuous as vol

from .api.client import UtecClient
from .api.errors import UtecAuthError, UtecError
from .api.models import UserInfo
from .auth import StaticTokenProvider
from .const import (
    CONF_CONFIRM_COMMANDS,
    CONF_POLL_INTERVAL,
    CONF_PUSH_HEALTHY_POLL_INTERVAL,
    CONF_SLOW_POLL_WHEN_PUSH_HEALTHY,
    CONF_USE_PUSH,
    CONFLICTING_DOMAIN,
    DATA_USER_ID,
    DATA_USER_ID_SOURCE,
    DEFAULT_OPTIONS,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MAX_PUSH_HEALTHY_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_PUSH_HEALTHY_POLL_INTERVAL,
    OAUTH_SCOPE,
    SEND_CUSTOM_DATA,
    USER_AGENT_TEMPLATE,
)

_LOGGER = logging.getLogger(__name__)


def _u_tec_loaded(flow: config_entry_oauth2_flow.AbstractOAuth2FlowHandler) -> bool:
    from . import u_tec_loaded

    return u_tec_loaded(flow.hass)


class OAuth2FlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle the U-tec OpenAPI OAuth2 config flow."""

    DOMAIN = DOMAIN
    VERSION = 1
    MINOR_VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra authorize URL data: scope only (never the client secret)."""
        return {"scope": OAUTH_SCOPE}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return UtecOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Block the flow while the legacy u_tec integration is loaded."""
        if _u_tec_loaded(self):
            return self.async_abort(
                reason="conflicting_integration",
                description_placeholders={"domain": CONFLICTING_DOMAIN},
            )
        return await super().async_step_user(user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-run OAuth (e.g. after rotating the Client Secret)."""
        return await self.async_step_user()

    async def _async_client(self, access_token: str) -> UtecClient:
        integration = await async_get_integration(self.hass, DOMAIN)
        return UtecClient(
            async_get_clientsession(self.hass),
            StaticTokenProvider(access_token),
            user_agent=USER_AGENT_TEMPLATE.format(version=integration.version),
            send_custom_data=SEND_CUSTOM_DATA,
        )

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Identify the account, check for locks, then create or update."""
        client = await self._async_client(str(data["token"]["access_token"]))
        user = UserInfo(None, None)
        try:
            user = await client.async_get_user()
        except UtecAuthError:
            return self.async_abort(reason="invalid_auth")
        except UtecError as err:
            if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
                # Cannot prove it is the same account; do not guess.
                return self.async_abort(reason="cannot_connect")
            _LOGGER.debug("User Get failed (%s); using client-id fallback", type(err).__name__)
        if user.user_id:
            user_id, id_source = user.user_id, "user_get"
        else:
            client_id = str(getattr(self.flow_impl, "client_id", "") or "")
            user_id = "client_" + hashlib.sha256(client_id.encode()).hexdigest()[:24]
            id_source = "client_id_hash"
        await self.async_set_unique_id(user_id)
        data = {**data, DATA_USER_ID: user_id, DATA_USER_ID_SOURCE: id_source}

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()

        try:
            discovery = await client.async_discover()
        except UtecAuthError:
            return self.async_abort(reason="invalid_auth")
        except UtecError:
            return self.async_abort(reason="cannot_connect")
        if not discovery.locks:
            return self.async_abort(reason="no_locks")
        title = f"U-tec ({user.first_name})" if user.first_name else "U-tec"
        return self.async_create_entry(title=title, data=data, options=dict(DEFAULT_OPTIONS))


def _seconds(minimum: float, maximum: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POLL_INTERVAL): _seconds(
            MIN_POLL_INTERVAL.total_seconds(), MAX_POLL_INTERVAL.total_seconds()
        ),
        vol.Required(CONF_USE_PUSH): selector.BooleanSelector(),
        vol.Required(CONF_SLOW_POLL_WHEN_PUSH_HEALTHY): selector.BooleanSelector(),
        vol.Required(CONF_PUSH_HEALTHY_POLL_INTERVAL): _seconds(
            MIN_PUSH_HEALTHY_POLL_INTERVAL.total_seconds(),
            MAX_PUSH_HEALTHY_POLL_INTERVAL.total_seconds(),
        ),
        vol.Required(CONF_CONFIRM_COMMANDS): selector.BooleanSelector(),
    }
)


class UtecOptionsFlow(OptionsFlow):
    """Options: interval (30 s floor), push, relaxed polling, confirmation."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            data = dict(user_input)
            for key in (CONF_POLL_INTERVAL, CONF_PUSH_HEALTHY_POLL_INTERVAL):
                data[key] = int(data[key])
            # Belt and braces: the schema already enforces the floor.
            data[CONF_POLL_INTERVAL] = max(
                int(MIN_POLL_INTERVAL.total_seconds()), data[CONF_POLL_INTERVAL]
            )
            return self.async_create_entry(data=data)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current),
        )
