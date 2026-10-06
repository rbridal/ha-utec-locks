"""Application credentials for U-tec OpenAPI OAuth.

U-tec's token endpoint wraps the token (DESIGN.md A5)::

    {"code": 200, "data": {"access_token": "...", "expires_in": 601200, ...}}

Home Assistant's stock helpers expect RFC 6749 fields at the top level, so
:class:`UtecAuthImplementation` unwraps the envelope for both the
authorization-code exchange and token refresh. A standard (unwrapped) token
response passes through unchanged. An error envelope raises
:class:`~.api.errors.UtecTokenError`.

The client secret is never put into the browser's authorize URL (A7).
"""

from __future__ import annotations

import contextlib
from typing import Any

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .api.errors import UtecTokenError
from .const import OAUTH_AUTHORIZE_URL, OAUTH_TOKEN_URL

_REAUTH_HINTS = ("invalid", "expired", "revoked", "unauthorized", "denied")


def unwrap_token(result: Any) -> dict[str, Any]:
    """Return the flat token dict from a wrapped or standard response."""
    if isinstance(result, dict):
        if "access_token" in result:
            return result
        data = result.get("data")
        if isinstance(data, dict) and "access_token" in data:
            return data
        code = result.get("code")
        error = result.get("error") or result.get("message") or result.get("msg")
        label = str(error or code or "missing_access_token")
        reauth = False
        with contextlib.suppress(TypeError, ValueError):
            reauth = 400 <= int(code) < 500
        if any(hint in label.lower() for hint in _REAUTH_HINTS):
            reauth = True
        raise UtecTokenError(label[:64], reauth=reauth)
    raise UtecTokenError("malformed_token_response", reauth=False)


class UtecAuthImplementation(AuthImplementation):
    """Application-credentials implementation that unwraps U-tec tokens."""

    async def _token_request(self, data: dict) -> dict:
        """Make a token request and unwrap U-tec's envelope."""
        return unwrap_token(await super()._token_request(data))


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return U-tec authorization server settings."""
    return AuthorizationServer(
        authorize_url=OAUTH_AUTHORIZE_URL,
        token_url=OAUTH_TOKEN_URL,
    )


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return the token-unwrapping implementation (code exchange and refresh)."""
    return UtecAuthImplementation(
        hass,
        auth_domain,
        credential,
        await async_get_authorization_server(hass),
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Placeholders for the credential prompt."""
    return {
        "more_info_url": "https://github.com/rbridal/ha-utec-locks#1-get-your-openapi-credentials",
        "redirect_uri": "https://my.home-assistant.io/redirect/oauth",
    }
