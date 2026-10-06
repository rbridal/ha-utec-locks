"""U-tec Locks for Home Assistant (domain: utec_locks).

Scaffold / stub: wiring only. Production API client, coordinator refresh,
push, and command executor land in later milestones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir

from .const import CONFLICTING_DOMAIN, DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from .commands import CommandExecutor
    from .coordinator import UtecAccountCoordinator
    from .push import PushManager
    from .usage import UsageMeter

_LOGGER = logging.getLogger(__name__)

type UtecConfigEntry = ConfigEntry[UtecRuntime]


@dataclass
class UtecRuntime:
    """Runtime objects attached to a config entry (typed entry.runtime_data)."""

    coordinator: UtecAccountCoordinator | None = None
    commands: CommandExecutor | None = None
    push: PushManager | None = None
    usage: UsageMeter | None = None
    locks: dict[str, Any] = field(default_factory=dict)
    # client: filled when api/ client is implemented


async def async_setup_entry(hass: HomeAssistant, entry: UtecConfigEntry) -> bool:
    """Set up U-tec Locks from a config entry (scaffold)."""
    if _u_tec_loaded(hass):
        _create_conflict_issue(hass)
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="conflicting_integration",
            translation_placeholders={"domain": CONFLICTING_DOMAIN},
        )
    ir.async_delete_issue(hass, DOMAIN, "conflicting_integration")

    # Scaffold: attach empty runtime; real client/coordinator/push come later.
    from .coordinator import UtecAccountCoordinator

    coordinator = UtecAccountCoordinator(hass, entry)
    entry.runtime_data = UtecRuntime(coordinator=coordinator)

    # Do not refresh against the live API in this scaffold pass.
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(p) for p in PLATFORMS]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UtecConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform(p) for p in PLATFORMS]
    )
    if unload_ok:
        entry.runtime_data = UtecRuntime()  # type: ignore[assignment]
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up after a config entry is removed (scaffold: no-op)."""
    return None


def _u_tec_loaded(hass: HomeAssistant) -> bool:
    """Return True if any legacy ``u_tec`` config entry is loaded.

    Decided: block setup (not just warn). Both integrations would register
    their own push URL on the same U-tec account (last one wins) and double
    API load. We never touch the other integration's entries.
    """
    return any(
        other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(CONFLICTING_DOMAIN)
    )


def _create_conflict_issue(hass: HomeAssistant) -> None:
    """Raise the conflicting_integration repair (error severity)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "conflicting_integration",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="conflicting_integration",
        translation_placeholders={"domain": CONFLICTING_DOMAIN},
    )
    _LOGGER.error(
        "%s setup blocked: legacy integration %s is loaded. "
        "Disable or remove %s first",
        DOMAIN,
        CONFLICTING_DOMAIN,
        CONFLICTING_DOMAIN,
    )
