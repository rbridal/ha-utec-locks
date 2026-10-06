"""U-tec Locks for Home Assistant (domain: utec_locks).

A locks-only integration for the U-tec OpenAPI. See docs/DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.loader import async_get_integration

from .api.client import UtecClient
from .api.errors import UtecAuthError, UtecError
from .auth import OAuthTokenProvider
from .commands import CommandExecutor
from .const import (
    CONF_USE_PUSH,
    CONFLICTING_DOMAIN,
    DATA_PUSH_SECRET,
    DATA_WEBHOOK_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    ISSUE_CONFLICT,
    PLATFORMS,
    SEND_CUSTOM_DATA,
    USER_AGENT_TEMPLATE,
)
from .coordinator import UtecAccountCoordinator
from .push import PushManager, generate_secret, generate_webhook_id
from .push_health import PushHealth
from .usage import UsageMeter

_LOGGER = logging.getLogger(__name__)

type UtecConfigEntry = ConfigEntry[UtecRuntime]


@dataclass
class UtecRuntime:
    """Runtime objects attached to a config entry (typed entry.runtime_data)."""

    client: UtecClient
    coordinator: UtecAccountCoordinator
    commands: CommandExecutor
    push: PushManager
    usage: UsageMeter
    health: PushHealth
    options: dict[str, Any]


async def async_setup_entry(hass: HomeAssistant, entry: UtecConfigEntry) -> bool:
    """Set up U-tec Locks from a config entry."""
    if u_tec_loaded(hass):
        create_conflict_issue(hass)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="conflicting_integration",
            translation_placeholders={"domain": CONFLICTING_DOMAIN},
        )
    ir.async_delete_issue(hass, DOMAIN, ISSUE_CONFLICT)

    try:
        implementation = await async_get_config_entry_implementation(hass, entry)
    except (ImplementationUnavailableError, ValueError) as err:
        # ValueError: the application credential was deleted; retry until re-added.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="credentials_unavailable"
        ) from err
    session = OAuth2Session(hass, entry, implementation)

    usage = UsageMeter(hass, entry.entry_id)
    await usage.async_load()
    integration = await async_get_integration(hass, DOMAIN)
    client = UtecClient(
        async_get_clientsession(hass),
        OAuthTokenProvider(hass, entry, session, on_refresh=usage.record_token_refresh),
        user_agent=USER_AGENT_TEMPLATE.format(version=integration.version),
        observer=usage.record_request,
        send_custom_data=SEND_CUSTOM_DATA,
    )
    health = PushHealth(hass)
    coordinator = UtecAccountCoordinator(hass, entry, client, usage, health)
    commands = CommandExecutor(hass, entry, coordinator)
    coordinator.commands = commands

    try:
        await coordinator.async_setup()
    except UtecAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="reauth_required"
        ) from err
    except UtecError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="api_unreachable",
            translation_placeholders={"error": type(err).__name__},
        ) from err
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_timers()

    # Push identity lives in entry.data (writing it never reloads the entry).
    updates: dict[str, Any] = {}
    if not entry.data.get(DATA_WEBHOOK_ID):
        updates[DATA_WEBHOOK_ID] = generate_webhook_id()
    if not entry.data.get(DATA_PUSH_SECRET):
        updates[DATA_PUSH_SECRET] = generate_secret()
    if updates:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})

    push = PushManager(hass, entry, coordinator, health)
    entry.runtime_data = UtecRuntime(
        client=client,
        coordinator=coordinator,
        commands=commands,
        push=push,
        usage=usage,
        health=health,
        options=dict(entry.options),
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _flush_on_stop(_event: Event) -> None:
        await usage.async_flush()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush_on_stop))

    await hass.config_entries.async_forward_entry_setups(entry, [Platform(p) for p in PLATFORMS])
    await push.async_start()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: UtecConfigEntry) -> None:
    """React to option changes only (token / secret writes change entry.data)."""
    runtime = entry.runtime_data
    new = dict(entry.options)
    if new == runtime.options:
        return
    old = runtime.options
    runtime.options = new
    if bool(old.get(CONF_USE_PUSH, True)) != bool(new.get(CONF_USE_PUSH, True)):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    runtime.coordinator.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: UtecConfigEntry) -> bool:
    """Unload a config entry: cancel timers, unregister webhook, flush Store."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform(p) for p in PLATFORMS]
    )
    if unload_ok:
        runtime = entry.runtime_data
        runtime.push.async_stop()
        runtime.commands.async_shutdown()
        runtime.coordinator.async_stop()
        runtime.health.async_shutdown()
        await runtime.usage.async_flush()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete persisted usage totals and the cloudhook."""
    await UsageMeter(hass, entry.entry_id).async_remove()
    webhook_id = entry.data.get(DATA_WEBHOOK_ID)
    if webhook_id and "cloud" in hass.config.components:
        from homeassistant.components import cloud

        try:
            await cloud.async_delete_cloudhook(hass, str(webhook_id))
        except Exception:
            _LOGGER.debug("No cloudhook to delete")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entries (none yet; future versions are refused)."""
    return entry.version <= 1


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: UtecConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a device only if its lock is no longer on the account."""
    coordinator = entry.runtime_data.coordinator
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier == f"account_{entry.entry_id}":
            return False
        if coordinator.is_available(identifier):
            return False
    return True


def default_options() -> dict[str, Any]:
    """Defaults for a new entry."""
    return dict(DEFAULT_OPTIONS)


def u_tec_loaded(hass: HomeAssistant) -> bool:
    """Return True if any legacy ``u_tec`` config entry is loaded.

    Decided: block setup (not just warn). Both integrations would register
    their own push URL on the same U-tec account (last one wins) and double
    API load. We never touch the other integration's entries.
    """
    return any(
        other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(CONFLICTING_DOMAIN)
    )


def create_conflict_issue(hass: HomeAssistant) -> None:
    """Raise the conflicting_integration repair (error severity)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_CONFLICT,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_CONFLICT,
        translation_placeholders={"domain": CONFLICTING_DOMAIN},
    )
    _LOGGER.error(
        "%s setup blocked: legacy integration %s is loaded. Disable or remove %s first",
        DOMAIN,
        CONFLICTING_DOMAIN,
        CONFLICTING_DOMAIN,
    )
