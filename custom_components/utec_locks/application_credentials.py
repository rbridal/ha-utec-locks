"""Application credentials support for U-tec OpenAPI OAuth — stub.

Production: AuthImplementation that unwraps U-tec's wrapped token response
``{"code":200,"data":{"access_token":...,"expires_in":...}}`` for both code
exchange and refresh (HA stock helpers expect a flat token dict).
"""

from __future__ import annotations

from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import OAUTH_AUTHORIZE_URL, OAUTH_TOKEN_URL


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return auth implementation for this domain (scaffold: stock)."""
    return config_entry_oauth2_flow.LocalOAuth2Implementation(
        hass,
        auth_domain,
        credential.client_id,
        credential.client_secret,
        OAUTH_AUTHORIZE_URL,
        OAUTH_TOKEN_URL,
    )


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return U-tec authorization server settings."""
    return AuthorizationServer(
        authorize_url=OAUTH_AUTHORIZE_URL,
        token_url=OAUTH_TOKEN_URL,
    )
