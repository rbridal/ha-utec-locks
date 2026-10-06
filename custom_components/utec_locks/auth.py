"""Token providers bridging Home Assistant OAuth to the API client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)

from .api.errors import UtecAuthError, UtecTokenError, UtecTransportError

_LOGGER = logging.getLogger(__name__)


class OAuthTokenProvider:
    """Supplies tokens from an OAuth2Session; forced refresh after INVALID_TOKEN."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: OAuth2Session,
        on_refresh: Callable[[], None] | None = None,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._entry = entry
        self._session = session
        self._on_refresh = on_refresh
        self._lock = asyncio.Lock()

    def _access_token(self) -> str:
        return str(self._session.token["access_token"])

    async def async_get_access_token(self) -> str:
        """Return a valid token, refreshing near expiry."""
        before = self._session.token.get("access_token")
        try:
            await self._session.async_ensure_token_valid()
        except Exception as err:
            raise _map_token_error(err) from err
        if self._session.token.get("access_token") != before and self._on_refresh:
            self._on_refresh()
        return self._access_token()

    async def async_force_refresh(self) -> str:
        """Refresh now and persist the token (never reloads the entry)."""
        async with self._lock:
            try:
                new_token = await self._session.implementation.async_refresh_token(
                    self._session.token
                )
            except Exception as err:
                mapped = _map_token_error(err)
                if isinstance(mapped, UtecAuthError):
                    self._entry.async_start_reauth(self._hass)
                raise mapped from err
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, "token": new_token}
            )
            if self._on_refresh:
                self._on_refresh()
            return self._access_token()


def _map_token_error(err: Exception) -> Exception:
    if isinstance(err, OAuth2TokenRequestReauthError):
        return UtecAuthError("token refresh rejected")
    if isinstance(err, UtecTokenError):
        return UtecAuthError(err.code) if err.reauth else UtecTransportError(err.code)
    if isinstance(err, (OAuth2TokenRequestError, ClientError, TimeoutError)):
        return UtecTransportError(f"token refresh failed: {type(err).__name__}")
    return err


class StaticTokenProvider:
    """Fixed token (config flow, before an entry exists)."""

    def __init__(self, access_token: str) -> None:
        """Initialize."""
        self._token = access_token

    async def async_get_access_token(self) -> str:
        """Return the token."""
        return self._token

    async def async_force_refresh(self) -> str:
        """No refresh possible in the flow."""
        raise UtecAuthError("token rejected")
