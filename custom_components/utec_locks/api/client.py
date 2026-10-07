"""Small async client for the U-tec OpenAPI (``POST /action``).

Responsibilities (DESIGN.md section 12):

* build the ``{header, payload}`` envelope with a UUIDv4 ``messageId``;
* add the Bearer token, the project User-Agent and per-kind timeouts;
* classify every response (HTTP status, HTTP-200 error envelopes, per-device
  errors) into ``Ok`` or one :class:`~.errors.UtecError` subclass;
* on ``INVALID_TOKEN`` / 401 / 403 force one token refresh and retry once;
* report every request to an observer (usage accounting) at one choke point;
* never log bodies, tokens or secrets.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
import logging
import time
from typing import Any, Protocol
import uuid

import aiohttp

from .errors import (
    UtecAuthError,
    UtecEnvelopeError,
    UtecError,
    UtecRateLimitError,
    UtecServerError,
    UtecTransportError,
)
from .models import (
    CommandReceipt,
    DiscoveryResult,
    LockInfo,
    QueryResult,
    Source,
    UserInfo,
    ci_get,
    parse_command_receipt,
    parse_discovery,
    parse_query,
    parse_user,
)

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.u-tec.com/action"
NS_DEVICE = "Uhome.Device"
NS_USER = "Uhome.User"
NS_CONFIGURE = "Uhome.Configure"

TIMEOUT_DEFAULT = 15.0
TIMEOUT_DISCOVERY = 20.0
QUERY_BATCH_SIZE = 20  # [Assumed] no documented limit

# Envelope codes that mean "the access token is not valid".
AUTH_ERROR_CODES = frozenset({"INVALID_TOKEN", "TOKEN_EXPIRED", "UNAUTHORIZED"})

# Request kinds (usage accounting).
KIND_QUERY = "query"
KIND_CONFIRM = "confirm_query"
KIND_COMMAND = "command"
KIND_DISCOVERY = "discovery"
KIND_PUSH_REGISTER = "push_register"
KIND_USER = "user"
REQUEST_KINDS = (
    KIND_QUERY,
    KIND_CONFIRM,
    KIND_COMMAND,
    KIND_DISCOVERY,
    KIND_PUSH_REGISTER,
    KIND_USER,
)
# Token refresh (OAuth token endpoint, not /action). Timed for the response-time
# sensors only; it is not an /action request and is not in REQUEST_KINDS.
KIND_TOKEN_REFRESH = "token_refresh"

# Outcomes where no HTTP response arrived (the elapsed time is not a response time).
NO_RESPONSE_OUTCOMES = frozenset({"timeout", "connection", "error"})


class TokenProvider(Protocol):
    """Supplies access tokens (HA's OAuth2Session in production)."""

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing if it is about to expire."""

    async def async_force_refresh(self) -> str:
        """Refresh the token now (after INVALID_TOKEN) and return it."""


@dataclass(frozen=True)
class RequestRecord:
    """Summary of one request (no bodies)."""

    kind: str
    namespace: str
    name: str
    started: datetime
    latency: float
    http_status: int | None
    outcome: str  # ok, auth, http_429, http_5xx, envelope, timeout, connection, error
    code: str | None = None
    devices: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics-friendly dict."""
        return {
            "kind": self.kind,
            "op": f"{self.namespace}/{self.name}",
            "started": self.started.isoformat(),
            "latency_ms": round(self.latency * 1000),
            "http_status": self.http_status,
            "outcome": self.outcome,
            "code": self.code,
            "devices": self.devices,
        }


RequestObserver = Callable[[RequestRecord], None]


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError, IndexError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max(0.0, (when - now).total_seconds())


class UtecClient:
    """U-tec OpenAPI client bound to one account's tokens."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenProvider,
        *,
        user_agent: str,
        observer: RequestObserver | None = None,
        send_custom_data: bool = True,
        url: str = API_URL,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._tokens = tokens
        self._user_agent = user_agent
        self._observer = observer
        self._send_custom_data = send_custom_data
        self._url = url
        self.exchanges: deque[RequestRecord] = deque(maxlen=20)

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def async_get_user(self) -> UserInfo:
        """Uhome.User / Get."""
        payload = await self._async_call(NS_USER, "Get", {}, kind=KIND_USER)
        return parse_user(payload)

    async def async_discover(self) -> DiscoveryResult:
        """Uhome.Device / Discovery."""
        payload = await self._async_call(
            NS_DEVICE, "Discovery", {}, kind=KIND_DISCOVERY, timeout=TIMEOUT_DISCOVERY
        )
        return parse_discovery(payload)

    async def async_query(
        self,
        locks: Sequence[LockInfo],
        *,
        kind: str = KIND_QUERY,
        source: Source = Source.POLL,
    ) -> QueryResult:
        """Uhome.Device / Query for several locks (batched in groups of 20)."""
        reports: dict[str, Any] = {}
        errors: dict[str, str] = {}
        unknown: set[str] = set()
        for start in range(0, len(locks), QUERY_BATCH_SIZE):
            group = locks[start : start + QUERY_BATCH_SIZE]
            devices = [self._device_ref(lock) for lock in group]
            payload = await self._async_call(
                NS_DEVICE,
                "Query",
                {"devices": devices},
                kind=kind,
                device_count=len(group),
            )
            result = parse_query(
                payload,
                [lock.device_id for lock in group],
                source,
                datetime.now(UTC),
            )
            reports.update(result.reports)
            errors.update(result.device_errors)
            unknown.update(result.unknown_ids)
        return QueryResult(reports=reports, device_errors=errors, unknown_ids=frozenset(unknown))

    async def async_command(
        self, lock: LockInfo, name: str, arguments: Mapping[str, Any] | None = None
    ) -> CommandReceipt:
        """Uhome.Device / Command on capability st.lock."""
        command: dict[str, Any] = {"capability": "st.lock", "name": name}
        if arguments:
            command["arguments"] = dict(arguments)
        device = self._device_ref(lock)
        device["command"] = command
        payload = await self._async_call(
            NS_DEVICE, "Command", {"devices": [device]}, kind=KIND_COMMAND, device_count=1
        )
        return parse_command_receipt(payload, lock.device_id)

    async def async_register_push(self, url: str, secret: str) -> None:
        """Uhome.Configure / Set (notification URL + secret)."""
        await self._async_call(
            NS_CONFIGURE,
            "Set",
            {"configure": {"notification": {"access_token": secret, "url": url}}},
            kind=KIND_PUSH_REGISTER,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _device_ref(self, lock: LockInfo) -> dict[str, Any]:
        ref: dict[str, Any] = {"id": lock.device_id}
        if self._send_custom_data and lock.custom_data:
            ref["customData"] = dict(lock.custom_data)
        return ref

    async def _async_call(
        self,
        namespace: str,
        name: str,
        payload: Mapping[str, Any],
        *,
        kind: str,
        timeout: float = TIMEOUT_DEFAULT,
        device_count: int = 0,
    ) -> Any:
        """Send a request; on an auth failure refresh the token and retry once."""
        token = await self._tokens.async_get_access_token()
        try:
            return await self._async_once(
                namespace, name, payload, token, kind, timeout, device_count
            )
        except UtecAuthError:
            _LOGGER.debug("Auth failure on %s/%s; forcing token refresh", namespace, name)
        token = await self._tokens.async_force_refresh()
        return await self._async_once(namespace, name, payload, token, kind, timeout, device_count)

    async def _async_once(
        self,
        namespace: str,
        name: str,
        payload: Mapping[str, Any],
        token: str,
        kind: str,
        timeout: float,
        device_count: int,
    ) -> Any:
        body = {
            "header": {
                "namespace": namespace,
                "name": name,
                "messageId": str(uuid.uuid4()),
                "payloadVersion": "1",
            },
            "payload": dict(payload),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        started = datetime.now(UTC)
        t0 = time.monotonic()
        status: int | None = None
        outcome = "ok"
        code: str | None = None
        try:
            try:
                async with asyncio.timeout(timeout):
                    async with self._session.post(self._url, json=body, headers=headers) as resp:
                        status = resp.status
                        retry_after = resp.headers.get("Retry-After")
                        text = await resp.text()
            except TimeoutError as err:
                outcome = "timeout"
                raise UtecTransportError("timeout") from err
            except aiohttp.ClientError as err:
                outcome = "connection"
                raise UtecTransportError(type(err).__name__) from err
            try:
                return self._classify(status, retry_after, text)
            except UtecAuthError:
                outcome = "auth"
                raise
            except UtecRateLimitError:
                outcome = "http_429"
                raise
            except UtecServerError:
                outcome = "http_5xx"
                raise
            except UtecEnvelopeError as err:
                outcome = "envelope"
                code = err.code
                raise
        finally:
            record = RequestRecord(
                kind=kind,
                namespace=namespace,
                name=name,
                started=started,
                latency=time.monotonic() - t0,
                http_status=status,
                outcome=outcome,
                code=code,
                devices=device_count,
            )
            self.exchanges.append(record)
            _LOGGER.debug(
                "%s/%s kind=%s devices=%d status=%s outcome=%s code=%s %.0f ms",
                namespace,
                name,
                kind,
                device_count,
                status,
                outcome,
                code,
                record.latency * 1000,
            )
            if self._observer is not None:
                try:
                    self._observer(record)
                except Exception:  # pragma: no cover - observer must never break I/O
                    _LOGGER.exception("Usage observer failed")

    @staticmethod
    def _classify(status: int, retry_after: str | None, text: str) -> Any:
        """Classify an HTTP response (DESIGN.md 12.2)."""
        if status in (401, 403):
            raise UtecAuthError(f"HTTP {status}")
        if status == 429:
            raise UtecRateLimitError(parse_retry_after(retry_after))
        if status >= 500:
            raise UtecServerError(status)
        if status >= 400:
            raise UtecEnvelopeError(f"HTTP_{status}")
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as err:
            raise UtecEnvelopeError("MALFORMED_RESPONSE") from err
        if not isinstance(data, Mapping):
            # A bare list as the whole body: treat it as the payload.
            return data
        payload = ci_get(data, "payload", {})
        error = ci_get(payload, "error") if isinstance(payload, Mapping) else None
        if error is None:
            error = ci_get(data, "error")
        if error:
            if isinstance(error, Mapping):
                code = str(ci_get(error, "code") or "UNKNOWN_ERROR")
                message = ci_get(error, "message")
            else:
                code, message = str(error), None
            if code.upper() in AUTH_ERROR_CODES:
                raise UtecAuthError(code)
            raise UtecEnvelopeError(code, str(message) if message is not None else None)
        return payload if payload is not None else {}


__all__ = [
    "KIND_TOKEN_REFRESH",
    "NO_RESPONSE_OUTCOMES",
    "REQUEST_KINDS",
    "RequestRecord",
    "TokenProvider",
    "UtecClient",
    "UtecError",
    "parse_retry_after",
]
