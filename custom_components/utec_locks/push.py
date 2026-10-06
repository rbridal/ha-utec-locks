"""Push (webhook) manager (DESIGN.md section 6).

* **HTTPS only** (firm floor): a Nabu Casa cloudhook (HTTPS) is preferred
  when Home Assistant Cloud is active; otherwise an external HTTPS URL with no
  private host. Plain HTTP webhooks are never registered, because the push
  secret travels in the ``Authorization`` header.
* The secret is ``secrets.token_urlsafe(32)``, stored in ``entry.data`` and
  rotated on every registration (setup, every 24 h, the button, automatic
  re-registration). The previous secret stays valid for 10 minutes.
* Every push must carry ``Authorization: Bearer <secret>`` (constant-time
  compare), or it gets 401 before the body is parsed.
* Pushes never stop or slow polling and never cause a Query.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import ipaddress
import json
import logging
import secrets
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util
from yarl import URL

from .api.errors import UtecAuthError, UtecError
from .api.models import normalize_push
from .const import (
    CONF_USE_PUSH,
    DATA_CLOUDHOOK_URL,
    DATA_PUSH_SECRET,
    DATA_PUSH_SECRET_PREVIOUS,
    DATA_PUSH_SECRET_ROTATED_AT,
    DATA_WEBHOOK_ID,
    DOMAIN,
    ISSUE_PUSH_NO_HTTPS,
    ISSUE_PUSH_UNHEALTHY,
    PUSH_AUTO_REREGISTER_MIN_GAP,
    PUSH_MAX_BODY_BYTES,
    PUSH_PREVIOUS_SECRET_GRACE,
    PUSH_REGISTER_BUTTON_COOLDOWN,
    PUSH_REJECTION_WARN_THRESHOLD,
    PUSH_RETRY_DELAYS,
    PUSH_SECRET_ROTATION_INTERVAL,
    PUSH_UNHEALTHY_REPAIR_AFTER,
)
from .push_health import PushHealth, PushStatus, Registration

if TYPE_CHECKING:
    from .coordinator import UtecAccountCoordinator

_LOGGER = logging.getLogger(__name__)


_PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


def generate_secret() -> str:
    """New push secret."""
    return secrets.token_urlsafe(32)


def generate_webhook_id() -> str:
    """Random 32-hex webhook id."""
    return secrets.token_hex(16)


def is_acceptable_push_url(url: str) -> bool:
    """HTTPS, with a public host name (no IPs, .local, localhost, private)."""
    try:
        parsed = URL(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.host:
        return False
    host = parsed.host.lower().rstrip(".")
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "." in host
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


class PushManager:
    """Registers, rotates, verifies and normalizes U-tec push."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: UtecAccountCoordinator,
        health: PushHealth,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._health = health
        self._registered_handler = False
        self._pending_secret: str | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._retry_unsub: CALLBACK_TYPE | None = None
        self._repair_unsub: CALLBACK_TYPE | None = None
        self._retry_index = 0
        self._last_button: datetime | None = None
        self._last_auto: datetime | None = None
        self._rejections: deque[datetime] = deque(maxlen=100)
        self._last_rejection_warning: datetime | None = None
        self.last_registration: datetime | None = None
        self.last_registration_error: str | None = None
        self.url_kind: str | None = None  # cloudhook / external / None
        self.bad_since: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Push option."""
        return bool(self._entry.options.get(CONF_USE_PUSH, True))

    @property
    def webhook_id(self) -> str:
        """Webhook id from entry data."""
        return str(self._entry.data[DATA_WEBHOOK_ID])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Register the HA webhook and start registration in the background."""
        self._unsubs.append(self._health.async_add_listener(self._on_status_change))
        if not self.enabled:
            self._health.set_registration(Registration.DISABLED)
            ir.async_delete_issue(self._hass, DOMAIN, self._issue(ISSUE_PUSH_NO_HTTPS))
            return
        webhook.async_register(
            self._hass,
            DOMAIN,
            "U-tec Locks push",
            self.webhook_id,
            self._async_handle_webhook,
            local_only=False,
            allowed_methods=["POST"],
        )
        self._registered_handler = True
        self._unsubs.append(
            async_track_time_interval(
                self._hass,
                self._async_daily,
                PUSH_SECRET_ROTATION_INTERVAL,
                name=f"{DOMAIN} push re-registration",
            )
        )
        self._entry.async_create_background_task(
            self._hass, self.async_register("setup"), f"{DOMAIN} push registration"
        )

    @callback
    def async_stop(self) -> None:
        """Unregister the HA webhook handler and cancel timers (unload)."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for attr in ("_retry_unsub", "_repair_unsub"):
            unsub = getattr(self, attr)
            if unsub:
                unsub()
                setattr(self, attr, None)
        if self._registered_handler:
            webhook.async_unregister(self._hass, self.webhook_id)
            self._registered_handler = False

    async def async_remove(self) -> None:
        """Entry removal: also delete the cloudhook (best effort)."""
        if "cloud" not in self._hass.config.components:
            return
        from homeassistant.components import cloud

        try:
            await cloud.async_delete_cloudhook(self._hass, self.webhook_id)
        except Exception:
            _LOGGER.debug("No cloudhook to delete")

    def _issue(self, base: str) -> str:
        return f"{base}_{self._entry.entry_id}"

    # ------------------------------------------------------------------
    # URL selection
    # ------------------------------------------------------------------

    async def _async_resolve_url(self) -> str | None:
        """Cloudhook if Cloud is active, else an external HTTPS URL, else None."""
        if "cloud" in self._hass.config.components:
            from homeassistant.components import cloud

            if cloud.async_active_subscription(self._hass):
                try:
                    url = await cloud.async_get_or_create_cloudhook(self._hass, self.webhook_id)
                except Exception as err:
                    _LOGGER.warning(
                        "Could not create a Home Assistant Cloud webhook (%s); "
                        "trying the external URL",
                        type(err).__name__,
                    )
                else:
                    if url and url.startswith("https://"):
                        if self._entry.data.get(DATA_CLOUDHOOK_URL) != url:
                            self._update_data({DATA_CLOUDHOOK_URL: url})
                        self.url_kind = "cloudhook"
                        return url
        try:
            base = get_url(
                self._hass,
                allow_internal=False,
                allow_ip=False,
                require_ssl=True,
                require_standard_port=False,
                prefer_external=True,
            )
        except NoURLAvailableError:
            self.url_kind = None
            return None
        url = base.rstrip("/") + webhook.async_generate_path(self.webhook_id)
        if not is_acceptable_push_url(url):
            self.url_kind = None
            return None
        self.url_kind = "external"
        return url

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _update_data(self, updates: dict[str, Any]) -> None:
        """Write entry.data (never triggers a reload; listener checks options)."""
        self._hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, **updates}
        )

    async def async_register(self, reason: str) -> bool:
        """Register (or re-register) with a fresh secret."""
        if not self.enabled:
            return False
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
        url = await self._async_resolve_url()
        if url is None:
            log = (
                _LOGGER.debug
                if self._health.registration is Registration.NO_URL
                else _LOGGER.warning
            )
            log(
                "Push not possible: no Home Assistant Cloud webhook and no external "
                "HTTPS URL. Polling continues"
            )
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self._issue(ISSUE_PUSH_NO_HTTPS),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_PUSH_NO_HTTPS,
            )
            self._health.set_registration(Registration.NO_URL)
            # HA Cloud may simply not be connected yet (startup): try again later.
            self._schedule_retry()
            return False
        ir.async_delete_issue(self._hass, DOMAIN, self._issue(ISSUE_PUSH_NO_HTTPS))
        new_secret = generate_secret()
        self._pending_secret = new_secret
        try:
            await self._coordinator.client.async_register_push(url, new_secret)
        except UtecAuthError:
            self._pending_secret = None
            self._entry.async_start_reauth(self._hass)
            self._registration_failed("auth")
            return False
        except UtecError as err:
            self._pending_secret = None
            self._registration_failed(type(err).__name__)
            return False
        old = self._entry.data.get(DATA_PUSH_SECRET)
        self._update_data(
            {
                DATA_PUSH_SECRET: new_secret,
                DATA_PUSH_SECRET_PREVIOUS: old,
                DATA_PUSH_SECRET_ROTATED_AT: dt_util.utcnow().isoformat(),
            }
        )
        self._pending_secret = None
        self._retry_index = 0
        self.last_registration = dt_util.utcnow()
        self.last_registration_error = None
        _LOGGER.info("Registered U-tec push (%s, %s)", self.url_kind, reason)
        if self._health.registration is not Registration.REGISTERED:
            self._health.set_registration(Registration.REGISTERED)
        return True

    def _registration_failed(self, error: str) -> None:
        self.last_registration_error = error
        delay = self._schedule_retry()
        _LOGGER.warning(
            "U-tec push registration failed (%s); retrying in %s. Polling continues",
            error,
            delay,
        )
        self._health.set_registration(Registration.FAILED)

    def _schedule_retry(self) -> timedelta:
        """Retry registration on the 5 min / 15 min / 1 h / 6 h ladder."""
        delay = PUSH_RETRY_DELAYS[min(self._retry_index, len(PUSH_RETRY_DELAYS) - 1)]
        self._retry_index += 1
        if self._retry_unsub:
            self._retry_unsub()

        @callback
        def _retry(_now: datetime) -> None:
            self._retry_unsub = None
            self._entry.async_create_background_task(
                self._hass, self.async_register("retry"), f"{DOMAIN} push retry"
            )

        self._retry_unsub = async_call_later(self._hass, delay, _retry)
        return delay

    async def _async_daily(self, _now: datetime | None = None) -> None:
        await self.async_register("daily")

    async def async_press_button(self) -> None:
        """Re-register push now (5-minute cooldown)."""
        if not self.enabled:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="push_disabled")
        now = dt_util.utcnow()
        if self._last_button and now - self._last_button < PUSH_REGISTER_BUTTON_COOLDOWN:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="push_register_cooldown"
            )
        self._last_button = now
        await self.async_register("button")

    # ------------------------------------------------------------------
    # Health reactions
    # ------------------------------------------------------------------

    @callback
    def _on_status_change(self, old: PushStatus, new: PushStatus) -> None:
        self._coordinator.async_push_status_changed(old, new)
        bad = new in (PushStatus.UNHEALTHY, PushStatus.REGISTRATION_FAILED)
        now = dt_util.utcnow()
        if bad and self.bad_since is None:
            self.bad_since = now
            self._repair_unsub = async_call_later(
                self._hass, PUSH_UNHEALTHY_REPAIR_AFTER, self._async_check_repair
            )
        elif not bad:
            self.bad_since = None
            if self._repair_unsub:
                self._repair_unsub()
                self._repair_unsub = None
            if new in (PushStatus.HEALTHY, PushStatus.DISABLED):
                ir.async_delete_issue(self._hass, DOMAIN, self._issue(ISSUE_PUSH_UNHEALTHY))
        if new is PushStatus.UNHEALTHY and (
            self._last_auto is None or now - self._last_auto >= PUSH_AUTO_REREGISTER_MIN_GAP
        ):
            self._last_auto = now
            self._entry.async_create_background_task(
                self._hass, self.async_register("unhealthy"), f"{DOMAIN} push re-register"
            )

    @callback
    def _async_check_repair(self, _now: datetime) -> None:
        self._repair_unsub = None
        if self.bad_since is None:
            return
        if self._health.status in (PushStatus.UNHEALTHY, PushStatus.REGISTRATION_FAILED):
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self._issue(ISSUE_PUSH_UNHEALTHY),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_PUSH_UNHEALTHY,
            )

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def _secret_ok(self, token: str) -> bool:
        if not token:
            return False
        ok = False
        current = self._entry.data.get(DATA_PUSH_SECRET)
        if current and secrets.compare_digest(token, str(current)):
            ok = True
        if self._pending_secret and secrets.compare_digest(token, self._pending_secret):
            ok = True
        previous = self._entry.data.get(DATA_PUSH_SECRET_PREVIOUS)
        rotated = self._entry.data.get(DATA_PUSH_SECRET_ROTATED_AT)
        if previous and rotated:
            rotated_at = dt_util.parse_datetime(str(rotated))
            if (
                rotated_at is not None
                and dt_util.utcnow() - rotated_at <= PUSH_PREVIOUS_SECRET_GRACE
                and secrets.compare_digest(token, str(previous))
            ):
                ok = True
        return ok

    def _note_rejection(self) -> None:
        now = dt_util.utcnow()
        self._rejections.append(now)
        recent = [t for t in self._rejections if now - t <= timedelta(hours=1)]
        if len(recent) >= PUSH_REJECTION_WARN_THRESHOLD and (
            self._last_rejection_warning is None
            or now - self._last_rejection_warning > timedelta(hours=1)
        ):
            self._last_rejection_warning = now
            _LOGGER.warning(
                "Rejected %d unauthenticated push requests in the last hour "
                "(possible probing, or a stale registration from another install)",
                len(recent),
            )

    async def _async_handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Any
    ) -> web.Response:
        """Handle one push (U-tec POSTs JSON with a Bearer secret)."""
        usage = self._coordinator.usage
        if request.method != "POST":
            return web.Response(status=405)
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not self._secret_ok(token):
            self._note_rejection()
            usage.record_push("rejected_auth", authenticated=False)
            return web.Response(status=401)
        length = getattr(request, "content_length", None)
        if length is not None and length > PUSH_MAX_BODY_BYTES:
            usage.record_push("malformed")
            return web.Response(status=413)
        text = await request.text()
        if len(text.encode("utf-8", "replace")) > PUSH_MAX_BODY_BYTES:
            usage.record_push("malformed")
            return web.Response(status=413)
        try:
            data = json.loads(text)
        except ValueError:
            usage.record_push("malformed")
            return web.Response(status=400)
        message = normalize_push(data)
        top_keys = sorted(str(k) for k in data) if isinstance(data, dict) else ["<list>"]
        _LOGGER.debug("Push received: header=%s keys=%s", message.kind or "-", top_keys)
        counter = self._coordinator.async_handle_push(message)
        usage.record_push(counter)
        return web.Response(status=200)

    def diagnostics(self) -> dict[str, Any]:
        """Push manager details for diagnostics (no URLs or secrets)."""
        return {
            "enabled": self.enabled,
            "url_kind": self.url_kind,
            "last_registration": (
                self.last_registration.isoformat() if self.last_registration else None
            ),
            "last_registration_error": self.last_registration_error,
            "bad_since": self.bad_since.isoformat() if self.bad_since else None,
            "rejections_last_hour": len(
                [t for t in self._rejections if dt_util.utcnow() - t <= timedelta(hours=1)]
            ),
        }
