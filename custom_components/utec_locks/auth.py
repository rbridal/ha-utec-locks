"""Token providers bridging Home Assistant OAuth to the API client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import logging
import time

from aiohttp import ClientError, ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)

from .api.client import KIND_TOKEN_REFRESH, RequestObserver, RequestRecord
from .api.errors import UtecAuthError, UtecTokenError, UtecTransportError

_LOGGER = logging.getLogger(__name__)


class OAuthTokenProvider:
    """Supplies tokens from an OAuth2Session; forced refresh after INVALID_TOKEN.

    Token refreshes that actually hit the token endpoint are timed and reported
    to ``observer`` (response-time sensors) as ``token_refresh`` records.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: OAuth2Session,
        on_refresh: Callable[[], None] | None = None,
        observer: RequestObserver | None = None,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._entry = entry
        self._session = session
        self._on_refresh = on_refresh
        self._observer = observer
        self._lock = asyncio.Lock()

    def _access_token(self) -> str:
        return str(self._session.token["access_token"])

    def _report(self, started: datetime, t0: float, err: Exception | None) -> None:
        if self._observer is None:
            return
        status, outcome, code = _classify_token_outcome(err)
        record = RequestRecord(
            kind=KIND_TOKEN_REFRESH,
            namespace="OAuth",
            name="token",
            started=started,
            latency=time.monotonic() - t0,
            http_status=status,
            outcome=outcome,
            code=code,
        )
        try:
            self._observer(record)
        except Exception:  # pragma: no cover - observer must never break auth
            _LOGGER.exception("Token timing observer failed")

    async def async_get_access_token(self) -> str:
        """Return a valid token, refreshing near expiry."""
        before = self._session.token.get("access_token")
        started = datetime.now(UTC)
        t0 = time.monotonic()
        try:
            await self._session.async_ensure_token_valid()
        except Exception as err:
            # async_ensure_token_valid only raises from a refresh attempt.
            self._report(started, t0, err)
            raise _map_token_error(err) from err
        if self._session.token.get("access_token") != before:
            self._report(started, t0, None)
            if self._on_refresh:
                self._on_refresh()
        return self._access_token()

    async def async_force_refresh(self) -> str:
        """Refresh now and persist the token (never reloads the entry)."""
        async with self._lock:
            started = datetime.now(UTC)
            t0 = time.monotonic()
            try:
                new_token = await self._session.implementation.async_refresh_token(
                    self._session.token
                )
            except Exception as err:
                self._report(started, t0, err)
                mapped = _map_token_error(err)
                if isinstance(mapped, UtecAuthError):
                    self._entry.async_start_reauth(self._hass)
                raise mapped from err
            self._report(started, t0, None)
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, "token": new_token}
            )
            if self._on_refresh:
                self._on_refresh()
            return self._access_token()


def _classify_token_outcome(err: Exception | None) -> tuple[int | None, str, str | None]:
    """(http_status, outcome, code) for a token request, in the client's outcome classes."""
    if err is None:
        return 200, "ok", None
    if isinstance(err, UtecTokenError):
        # HTTP 200 with an error envelope instead of a token.
        return 200, "auth" if err.reauth else "envelope", err.code
    if isinstance(err, ClientResponseError):
        status = err.status
        if status < 400:
            # e.g. a 200 whose body is not JSON.
            return status, "envelope", None
        if status == 429:
            return status, "http_429", None
        if status >= 500:
            return status, "http_5xx", None
        return status, "auth", None
    if isinstance(err, TimeoutError):
        return None, "timeout", None
    if isinstance(err, ClientError):
        return None, "connection", None
    return None, "error", None


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
