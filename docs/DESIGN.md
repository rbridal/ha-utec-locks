---
title: "U-tec Locks for Home Assistant: design document for a from-scratch, locks-only integration"
subtitle: "Domain: utec_locks · Repository: rbridal/ha-utec-locks"
date: "October 6, 2026"
---

# How to read this document

This is a design for a brand-new Home Assistant custom integration for U-tec / Ultraloq smart locks. It is a complete rewrite. It is not a fork of LF2b2w/Uhome-HA (domain `u_tec`) and does not depend on the `utec-client` library. Both were studied only to learn how the U-tec OpenAPI behaves.

Every factual claim about the vendor API carries one of these labels:

| Label | Meaning |
|---|---|
| **[Doc]** | Stated in U-tec / Xthings published documentation (OpenAPI Postman collection at doc.api.u-tec.com, or Xthings support articles). |
| **[Code]** | Confirmed by reading the existing integration or `utec-client` source (pinned versions in Appendix C). Shows what someone implemented and shipped, not necessarily what the API guarantees. |
| **[Field]** | Reported by users or contributors from real installs (GitHub issues, forums). Not reproduced by me. |
| **[Assumed]** | A working assumption this design makes. Must be checked during the hardware soak. |
| **[Unknown]** | Nobody has published an answer. Listed in Open Questions. |

Decisions that belong to Rob are collected in Section 19. All Section 19 items were decided on October 6, 2026; the body below is aligned with those answers.

---

# 1. Summary

**What it is.** A small, locks-only integration that talks to the U-tec OpenAPI (`https://api.u-tec.com/action`) with each user's own OAuth credentials. Each lock gets a lock entity, a lock-mode select (Normal / Passage / Locked), battery, an optional door sensor, and staleness diagnostics. The account gets API-usage sensors whose totals survive restarts.

**Who it is for.** People who run their locks from Home Assistant. If you often lock or unlock at the door (keypad, fingerprint, thumb-turn, key) or in the U-tec app, Home Assistant can show the wrong state for a while when U-tec's push fails, and this integration is not for you. The README says so on its first screen.

**Key design decisions.**

1. **One account-level coordinator, one batched Query per cycle** for all locks. Cost does not grow with lock count.
2. **Background polling floor of 30 seconds, hard-coded.** Default is 30 s. Nothing in the UI, services, or manual refresh can poll the full account faster. There is no faster fallback when push is down.
3. **Push (webhook) is used when it works, but never trusted blindly.** A push-health model scores push on evidence (did push deliver the changes that polling later saw?), not on silence. Push status, last push time, and a push-healthy sensor are visible. Polling never stops.
4. **Security commands are never skipped.** Lock, unlock and mode change are always sent when asked. No cached state (already locked, Passage, stale, offline, push unhealthy, backoff) can suppress one.
5. **No optimistic "locked".** After a command the lock shows HA's real transitional states (`locking` / `unlocking`) until the lock confirms, or the attempt times out and HA shows what the lock last reported. Reason: field reports of commands that U-tec accepted but the lock never performed.
6. **Bounded confirmation after commands we send.** At most 10 extra queries per command on a 1/1/1/1/2/3/5/8/13/21-second interval schedule (about 56 s), batched across locks that are pending at the same time. **No hourly confirmation budget or cap.** This is the only polling allowed faster than 30 s, and it only happens because the user just acted.
7. **Stale is unknown, not unavailable.** A lock whose last good report is too old shows `unknown` with `stale: true` and its last known state as attributes. It stays available, because Home Assistant silently drops service calls to unavailable entities, and a lock you cannot command is worse than one whose state is uncertain.
8. **Own small API client inside the integration** (in-repo for v1, not a PyPI package on day one), written to the documented protocol and handling U-tec's quirks (HTTP 200 error envelopes, per-device errors, wrapped OAuth tokens, inconsistent casing, deferred responses). Structured so it can be split into a PyPI library later if Rob ever wants core submission. **Decided (Rob, October 6, 2026).**
9. **Quality-scale target: Silver complete, most of Gold**, with two documented deviations driven by Rob's rules (`entity-unavailable` semantics and a user-configurable poll interval).

**Size.** Estimated 3,300 to 3,900 lines of Python including its own API client, against 5,316 lines on our `feat/lock-mode-select` branch plus about 1,057 lines of `utec-client` it depends on. Measured numbers and method are in Section 16.

**Draft README.** The complete draft README, including the "Is this integration for you?" section at the top, is in Appendix D and in `README.draft.md`.

---

# 2. Goals, non-goals, target user

## 2.1 Goals

1. Correct, honest lock state for HA-first users. When the integration does not know, it says so (unknown plus staleness attributes) instead of guessing.
2. Commands that always go out, with clear feedback: confirmed, not confirmed, or rejected (with the vendor's reason).
3. Fair use of a shared vendor API with no published limits: a firm 30-second background floor, batching, backoff with jitter, bounded confirmation, and visible usage numbers.
4. Guidance and visibility over restriction (Rob's preference) everywhere except the firm floors (30-second polling; HTTPS-only push).
5. Rock-solid engineering: typed, tested to 95%+ coverage, no private HA APIs, clean unload, redacted diagnostics.
6. Different domain from `u_tec`, but **setup is blocked** if the old `u_tec` integration is also loaded (push conflict and doubled load). Disable or remove `u_tec` before setting up U-tec Locks.

## 2.2 Non-goals (explicit)

- **No lights, switches, plugs, bulbs, or a generic device layer.** Discovery results that are not locks are ignored and only counted in diagnostics.
- **No lock user / PIN management** (`st.lockUser` list/add/update/delete). It is documented **[Doc]**, but it handles credentials, and the vendor's `get` response even returns a user's PIN in clear text **[Doc]**. **Decided (Rob, October 6, 2026):** out of scope for v1.
- **No polling faster than 30 s** for background refresh, including no "debug polling mode". (Debug mode was a reasonable answer in the old codebase. In a design built on a firm 30-second floor it would be the one hole in the floor.)
- **No optimistic state** for lock, unlock, or mode.
- **No local control** (BLE, Matter, Z-Wave). Owners of Matter or Z-Wave Ultraloq models should consider those core integrations for local state.
- **No YAML configuration.** UI only.
- **No support for the Xthings Cloud private API** used by the vendor's core `xthings_cloud` integration (different backend, `api.cloud.xthings.com`, username/password login).
- **No automatic import of `u_tec` config entries or entity IDs** (Section 17).
- **No telemetry** sent anywhere except U-tec's API.

## 2.3 Target user

A Home Assistant user whose locks are operated mainly through HA: automations, dashboards, voice assistants routed through HA, presence. The locks are mostly left alone at the door. Typical cases:

- A shop or office door that HA puts into Passage mode during business hours and back to Normal at closing.
- A home where HA locks every door at night or when everyone leaves.
- Latch-model owners who use Passage mode to stop auto-lock during the day.

**Not the target:** households that use the keypad, fingerprint, key or thumb-turn many times a day, or that live in the U-tec app. For them HA will often lag behind reality when push is broken, which (per field reports) is often.

---

# 3. What we know about the U-tec OpenAPI

This section is why the existing code was studied. Everything here is about the vendor API, not about how the old integration is built.

## 3.1 Protocol facts

| # | Fact | Label and source |
|---|---|---|
| A1 | All calls are `POST https://api.u-tec.com/action` with JSON `{header:{namespace,name,messageId,payloadVersion:"1"}, payload:{...}}`. `messageId` should be a UUIDv4. | [Doc] Postman collection; [Code] `utec_client/api.py` |
| A2 | Auth is `Authorization: Bearer <access_token>`. A body-level `authentication` object is also documented; the header wins. | [Doc] |
| A3 | Namespaces used here: `Uhome.Device` (Discovery, Query, Command), `Uhome.Configure` (Set, for the push URL), `Uhome.User` (Get, Logout). | [Doc] |
| A4 | OAuth2 authorization-code flow. Authorize `https://oauth.u-tec.com/authorize`, token `https://oauth.u-tec.com/token`, scope `openapi`. | [Doc]; [Code] `const.py` |
| A5 | The token endpoint wraps the token: `{"code":200,"data":{"access_token":...,"expires_in":601200,...}}`. HA's stock OAuth helpers need it unwrapped for both code exchange and refresh. | [Code] `oauth.py`; [Field] issue #19 (refresh broke) |
| A6 | Access tokens last about 7 days (`expires_in` 601200 s in the captured example). | [Code] comment in `oauth.py` (one sample) |
| A7 | The vendor doc puts `client_secret` in the authorize URL. The working community flow does not (HA's standard authorize URL plus `scope=openapi`), and users authenticate fine. | [Doc] vs [Code] `config_flow.py` `extra_authorize_data`; working installs imply it is not needed |
| A8 | Credentials are per user and self-service: Xthings Home app (3.5.5+) → My Account → OpenAPI → choose role and devices → Activate. The redirect URI is set there; the community uses `https://my.home-assistant.io/redirect/oauth`. | [Doc] Ultraloq help article (per prior report); [Code] README |
| A9 | Discovery returns devices with `id`, `name`, `category` (`SmartLock` for locks), `handleType` (`utec-lock`, `utec-lock-sensor`), `deviceInfo{manufacturer,model,hwVersion}`, optional `attributes`, optional `customData`. | [Doc] |
| A10 | Device `id` is "currently the device's BLE MAC address or serial number, which is encrypted once more for privacy compliance". Field examples look like MAC addresses (`40:4C:CA:8B:1B:5D`). | [Doc] Xthings Foundational APIs; [Field] #30, #68 |
| A11 | `customData` from discovery "must be included with each device operation request". `utec-client` sends a snake_case `custom_data` key, and only when the caller passes one; the old integration passes none. | [Doc]; [Code] `api.py get_device_state`. Whether omitting it matters is [Unknown] |
| A12 | Query takes a list of device ids and returns `payload.devices[].states[]` of `{capability,name,value}`. Several ids per request work (the old integration batches every poll). | [Doc]; [Code] `coordinator.py` bulk poll |
| A13 | Capability `st.lock`: `lockState` in {Locked, Unlocked, Jammed, Unknown}; `lockMode` in {0 Normal, 1 Passage, 2 Locked}. Commands `lock`, `unlock`, `setMode(mode)`. | [Doc] |
| A14 | `st.batteryLevel.level` is an integer 1..5, not a percentage. | [Doc] |
| A15 | Door sensor is `st.doorSensor` / `sensorState` in {Closed, Open, Unknown} on `utec-lock-sensor` devices. `utec-client` checks both `st.DoorSensor` and `st.doorSensor` because casing varies. | [Doc]; [Code] `devices/lock.py` |
| A16 | `st.healthCheck.status` reports Online / Offline; casing varies between vendor examples. | [Doc] |
| A17 | Casing is inconsistent across vendor docs: `st.lock` vs `st.Lock`, `Locked` vs `locked`, `st.batteryLevel` vs `st.BatteryLevel`. Parsing must be case-insensitive. | [Doc] (two vendor documents disagree) |
| A18 | Command replies are not results. They carry `st.deferredResponse.seconds` (1..20; 20 for lock/unlock and 5 for setMode in the examples). The real outcome must be read back (push or Query). | [Doc]; [Field] #68 captured reply with `value: 20` |
| A19 | Errors often come back as **HTTP 200** with `payload.error {code,message}`, for example `INVALID_TOKEN`. | [Doc] error example; [Code] `_raise_for_error_payload`; [Field] #68 |
| A20 | Per-device errors appear in `payload.devices[].error`, for example `DEVICE_OFFLINE` ("Device is offline and cannot accept commands"). | [Doc] (switch examples; same envelope for all devices) |
| A21 | Query intermittently returns HTTP 500, "sometimes twice in a row, sometimes more". | [Field] #24, #49, #68 (Rob's own comment) |
| A22 | Commands can be accepted and still not move the lock: "6 of 12 front-door lock commands never moved the lock while HA showed locked". | [Field] #68 (three U-Bolt Pro WiFi locks) |
| A23 | `setMode` shape is `{"capability":"st.lock","name":"setMode","arguments":{"mode":N}}`. | [Doc]. Hardware behavior on Latch-5-NFC [Field] (Rob, October 6, 2026): see A24–A25 and Passage notes below |
| A24 | Mode 2 ("Locked") on Latch-5-NFC: door locks; RFID / credentials denied (red flash); Home Assistant reports locked. U-tec still does not document the mode. Other models not yet verified. | [Field] Rob Shop Latch-5-NFC hardware verification, October 6, 2026; [Doc] (absence of vendor description) |
| A25 | Latch locks auto-lock and that cannot be disabled except via Passage mode. The API exposes no auto-lock setting or timer. On Latch-5-NFC, Passage auto-unlocks and stays unlocked through a 5+ minute auto-lock window; entering Normal locks the door and restores normal auto-lock. An unlock command while already in Passage produces an odd beep and no change. | Rob's lessons learned; [Doc] shows no such capability; [Field] Latch-5-NFC verification October 6, 2026 |
| A26 | No rate limits, quotas, or 429 behavior are published. The Xthings developer page lists "Authentication & Rate Limits" as a topic with nothing behind it. | [Doc] (absence), prior report Section 4.1 |
| A27 | The vendor's own core integration (`xthings_cloud`, different backend) is `cloud_push` with a fixed 30-minute fallback poll. | [Code] core PR #167885, per prior report |

## 3.2 Push (webhook) facts

| # | Fact | Label and source |
|---|---|---|
| P1 | Push is registered with `Uhome.Configure / Set`, payload `{"configure":{"notification":{"access_token":"<secret>","url":"<url>"}}}`. A successful reply is `payload: []`. | [Doc] Register Notification URL; [Code] `utec_client.api.set_push_status` |
| P2 | The URL must be `http://` or `https://`; for HTTPS the certificate must come from a trusted CA. | [Doc] |
| P3 | The `access_token` in the registration is a secret we choose. U-tec sends it back on each push as `Authorization: Bearer <secret>`. The vendor recommends rotating it periodically. | [Doc] Event Notification article ("Request Authentication ... Bearer") and rotation advice; [Code] `api.py _handle_webhook`; [Field] #43 (contributor: "webhook integrations are fixed now ... code for authorizing them") |
| P4 | Notification envelope: `header.namespace = "Uhome.Notification"`, `header.name` in {`DeviceState`, `DeviceSync`, `DeviceDelete`}. DeviceState carries `payload.devices[].{id,states}`; DeviceSync carries discovery-style records; DeviceDelete carries ids. | [Doc] Xthings "Event Notification" article |
| P5 | The vendor's own example is not valid JSON (states written as `{ {...}, {...} }`), says `playloadVersion`, and uses lowercase values. Real payloads have arrived as the nested envelope, as a bare list of device dicts, and with `payload` itself a list. Entries such as `{'id': ...}` with no states have been logged. | [Doc]; [Code] `coordinator.update_push_data` comments (issue #30 crash); [Field] #68 |
| P6 | Push delivery is unreliable: often never arrives, or works for a while and stops. Vendor staff answered a forum report only by pointing to a support ticket. The upstream maintainer cited this as his reason for very fast polling. | [Field] #49, #68, Ultraloq forum thread 11512, core issues #175810 and #175488 (vendor's other integration) |
| P7 | There is no documented way to read back, list, or delete a registered notification URL. | [Doc] (absence); [Field] forum 11512 |
| P8 | Whether one account (or one OpenAPI client) can have more than one notification URL is not documented. The single `notification` object suggests one slot, so the last registration probably wins. | [Assumed]. Consequence: the old `u_tec` integration and this one would take push from each other on the same account |
| P9 | Pushes carry no timestamp or sequence number in the documented format. | [Doc] (absence) |
| P10 | Whether changes made at the lock (keypad, thumb-turn) produce pushes, or only API-driven changes do: not documented. | [Unknown] |

## 3.3 Implications that shape the design

- Treat every command reply as "accepted for delivery", never as success (A18, A22).
- Treat HTTP 200 as "look inside" (A19, A20). Classify envelope codes; unknown codes are errors.
- Parse leniently and case-insensitively (A15 to A17, P5). Log shape problems at debug level with keys only, never bodies.
- Assume push can stop silently at any time (P6, P7). Never let push suppress polling. Score push on evidence.
- Assume 500s are normal background noise (A21). One or two failures must not flip lock state to unknown, and must lead to backoff, not tighter retries.
- Assume rate limiting could start any day, possibly as an HTTP 200 envelope rather than a 429 (A19, A26).

---

# 4. Architecture

## 4.1 Component view

```
                      +---------------------- Home Assistant -----------------------+
 U-tec OpenAPI        |                                                             |
 api.u-tec.com        |  UtecClient (api/)             AccountCoordinator           |
 /action  <-----------+-- request/envelope/errors <--- background Query (all locks) |
                      |  UsageMeter (counts every      discovery (setup, 6 h, sync) |
                      |  request; persisted Store)     floor, backoff, jitter       |
                      |          ^                     staleness clock              |
                      |          |                            |                     |
                      |  CommandExecutor                      v                     |
                      |  per-lock mutex, send,         LockStateStore               |
                      |  batched confirmation,  -----> (per lock: values,           |
                      |  result events                   source, timestamps)         |
 U-tec push --------->+  PushManager (webhook/cloudhook)      |                     |
 (POST to HA)         |  register, rotate secret,             v                     |
                      |  verify, normalize ----------> Entities: lock, select,      |
                      |  PushHealth (evidence model)   sensor, binary_sensor,       |
                      |                                event, button                |
                      +-------------------------------------------------------------+
```

One config entry per U-tec account. Runtime objects live in `entry.runtime_data`, a typed dataclass:

```python
@dataclass
class UtecRuntime:
    client: UtecClient
    coordinator: AccountCoordinator
    commands: CommandExecutor
    push: PushManager
    usage: UsageMeter
    locks: dict[str, LockInfo]      # from discovery, keyed by device id
```

## 4.2 Data model

Vendor payloads are converted at the edge (inside `api/`) into small immutable dataclasses. Nothing above `api/` touches raw dicts.

```python
class LockStateValue(StrEnum): LOCKED, UNLOCKED, JAMMED, UNKNOWN
class LockMode(IntEnum): NORMAL = 0; PASSAGE = 1; LOCKED = 2
class DoorState(StrEnum): OPEN, CLOSED, UNKNOWN
class Source(StrEnum): POLL, PUSH, CONFIRM

@dataclass(frozen=True)
class LockInfo:            # from discovery
    device_id: str; name: str; handle_type: str; model: str; hw_version: str
    manufacturer: str; has_door_sensor: bool; custom_data: dict | None

@dataclass(frozen=True)
class LockReport:          # one observation from Query or push
    device_id: str
    lock_state: LockStateValue | None   # None = capability absent in this report
    lock_mode: LockMode | None
    door: DoorState | None
    battery_level: int | None           # 1..5
    online: bool | None
    received_at: datetime               # HA clock, UTC
    source: Source
    error_code: str | None              # per-device error, if any

@dataclass(frozen=True)
class CommandReceipt:
    device_id: str
    deferred_seconds: int | None        # st.deferredResponse, clamped to 1..20
    error_code: str | None
```

`LockStateStore` keeps, per lock, the last value of each field with its own timestamp and source. A battery-only push updates battery and does not refresh the lock-state timestamp. This matters for staleness: only a report that actually carries `lockState` makes lock state fresh.

## 4.3 Why one account-level coordinator

- **Cost does not grow with lock count.** One Query carries every lock id (A12). Ten locks cost the same as one.
- **One place to enforce the floor, backoff, and jitter.** Per-lock coordinators would multiply requests and make the floor meaningless.
- **One clock for staleness.** Every lock's freshness is judged against the same cadence.
- Built on HA's `DataUpdateCoordinator` with `config_entry=entry`, `always_update=False`, and a custom refresh `Debouncer` (cooldown 30 s, see 5.3). No private coordinator methods. (The old code calls `_schedule_refresh`, which is private and can break on an HA upgrade.)

Batch size: an account with more than 20 locks has its Query split into groups of 20 **[Assumed]**; there is no documented limit. This only affects very large accounts, and each group counts as a request.

---

# 5. Background polling

## 5.1 The floor and the default

- `MIN_POLL_INTERVAL = 30 s`, a module constant. The options form's minimum is 30. A lower value from any source (a hand-edited `.storage` file, a future migration) is clamped to 30 at load and logged once.
- **Default: 30 s.** Reasons:
  1. Push is unreliable in the field (P6), so for most users polling is the real source of truth for anything HA did not cause: latch auto-lock after an HA unlock, the occasional keypad entry, door open/closed, battery.
  2. 30 s gives a worst-case lag of about 30 s and an average of about 15 s for out-of-band changes, and keeps the staleness threshold (5.5) at 120 s.
  3. The load is modest and does not depend on lock count: **2,880 Query requests per day per account**. That is a third of the old integration's 10-second default (8,640 plus 288 discoveries) and about 60% of the 20-second setting our fork recommended (4,320 plus 288).
  4. Rob already runs 30 s, so it is a known operating point.
- **Max: 3,600 s.**
- For users whose push is proven healthy there is an optional relaxation (6.7), off by default in v1.

**Decided (Rob, October 6, 2026):** default poll interval is **30 s** (equal to the floor).

## 5.2 What one cycle does

1. If the account has no known locks, do nothing (no request).
2. Send one Query with every known lock id, plus each lock's `customData` when discovery supplied one (A11, risk 18.4).
3. Classify the reply (Section 12.2). On success:
   - Each returned device becomes a `LockReport(source=POLL)` for `LockStateStore`.
   - Devices missing from the reply, or carrying a per-device error, are not refreshed. Their age keeps growing.
   - Unknown device ids trigger a debounced discovery (5.4).
   - Observed changes are passed to `PushHealth` as evidence (6.5).
4. Reset backoff. Record latency and outcome in `UsageMeter`.

## 5.3 Floor enforcement (belt and braces)

The 30-second floor applies to every request that queries the whole account, whatever triggered it:

- Scheduled refresh: `update_interval` is never below 30 s.
- Manual refresh (`homeassistant.update_entity`, `async_request_refresh`): routed through `Debouncer(cooldown=30, immediate=False)`.
- A guard inside `_async_update_data`: if the last full-account Query started less than 29.5 s ago (monotonic clock), return cached data without a request and count it as `refresh_suppressed`.

The only requests allowed closer together are the bounded command confirmations in Section 8. They query only locks with a pending command and exist only because the user just acted.

## 5.4 Discovery cadence

- At setup (required; also the `test-before-setup` check).
- Every 6 hours (4 per day). **Decided (Rob, October 6, 2026).** The old integration discovered every 5 minutes (288 per day), though lock inventories rarely change.
- On a `DeviceSync` or `DeviceDelete` push, or an unknown device id in a Query or push, debounced to at most once per 10 minutes.
- On entry reload.

New locks appear without a restart (Gold `dynamic-devices`). A lock missing from two consecutive successful discoveries becomes unavailable and raises a `lock_removed` repair that offers to delete the device (Gold `stale-devices`). Two, not one, so that a single partial discovery cannot remove someone's lock.

## 5.5 Staleness

- A lock's **lock state** is fresh if the newest report carrying `lockState` arrived within `stale_after`.
- `stale_after = max(3 x effective poll interval, 120 s)`. At the 30-second default that is 120 s, which rides out three failed polls in a row (500s come in runs, A21) before the state is declared unknown.
- Door state follows the same rule. Battery does not go stale in the UI (it changes slowly); its entities carry `last_reported` instead.
- A 15-second timer re-checks freshness and writes state only when a lock flips between fresh and stale, so there is no recorder churn.

## 5.6 Failure handling and backoff

| Situation | Behavior |
|---|---|
| HTTP 5xx, timeout (15 s), connection error | Count the failure. Next attempt after `min(base x 2^n, 900 s)` with +/-20% jitter, never below `base`. `n` resets on success. |
| HTTP 429 | Honor `Retry-After` (seconds or HTTP date), clamped to [base, 3,600 s]. Without the header, back off as for 5xx starting at n=2. Three 429s within an hour raise the `rate_limited` repair. |
| HTTP 200 + `INVALID_TOKEN` | Force one token refresh and retry once. If it repeats, raise `ConfigEntryAuthFailed`, which starts reauth. |
| HTTP 200 + other top-level envelope error | Treated like 5xx (backoff). The code is logged (no body) and counted per code. |
| HTTP 401 / 403 | Same as `INVALID_TOKEN`. |
| Five consecutive failures | "Circuit open": one attempt every 900 s until one succeeds. HA's coordinator logs once when updates start failing and once when they recover (Silver `log-when-unavailable`). |

Backoff only slows background polling. It never delays or blocks a user command (Section 8).

## 5.7 Startup jitter

After setup's discovery and first Query, the first scheduled poll is delayed by a random 0 to `base` seconds. This spreads load when many installs restart at the same moment (HA release days, regional power or ISP outages). HA's own sub-second stagger is not enough for a fleet-wide restart.

## 5.8 User-Agent

Every request sends `User-Agent: HomeAssistant-utec_locks/<version> (+https://github.com/rbridal/ha-utec-locks)`, so U-tec can identify the project and contact it instead of blocking it, as the fair-use report recommended. **Decided (Rob, October 6, 2026):** include the GitHub repo URL.

---

# 6. Push (webhook)

## 6.1 What exists: confirmed versus assumed

- **Confirmed [Doc][Code]:** the registration call and payload (P1), URL rules (P2), the secret coming back as a Bearer header (P3, in the Event Notification article and implemented upstream), and the notification envelope names (P4).
- **Confirmed [Code], behavior [Field]:** the old integration registers a Nabu Casa cloudhook when HA Cloud is active, otherwise an external URL; re-registers every 24 h with a fresh secret; checks the Bearer token with a constant-time compare; and accepts several payload shapes. Field reports say push works sometimes and fails often (P6).
- **Assumed:** one notification URL per account or client, so the last registration wins (P8); push covers changes made at the lock as well as API changes (P10); pushes arrive roughly in order (P9 gives no way to check).
- **Unknown:** delivery guarantees, whether U-tec retries when we answer non-2xx, and any way to unregister.

## 6.2 URL selection (HTTPS only)

1. If HA Cloud has an active subscription, create or reuse a cloudhook for our webhook id. The cloudhook URL is stored in the entry so it stays stable across restarts.
2. Otherwise `get_url(hass, allow_internal=False, allow_ip=False, require_ssl=True, prefer_external=True)` plus `webhook.async_generate_url`.
3. Otherwise push is not possible. Push status becomes `no_url`, the `push_no_https_url` repair explains the options (Nabu Casa, or an HTTPS external URL), and polling carries on.

**HTTPS is a firm floor (decided Rob, October 6, 2026).** The push secret travels in the `Authorization` header. Over plain HTTP anyone on the path could read it and then post fake "locked" pushes, which is a real security hole for a lock. The vendor allows HTTP (P2); this integration does not.

**What counts as HTTPS.** Home Assistant Cloud / Nabu Casa **cloudhook** URLs are HTTPS, so they qualify and are preferred when Cloud is active (step 1 above). An external HTTPS URL with a trusted-CA certificate also qualifies (step 2). Plain HTTP local webhooks (including `http://` LAN or reverse-proxy endpoints) do **not** qualify, even if they are reachable from the internet. Rationale: the push secret is sent in a header; it must never travel on an unencrypted path.

Private addresses (RFC 1918, `.local`, loopback) are never registered; U-tec's servers cannot reach them.

## 6.3 Registration and secret rotation

- Webhook id: a random 32-hex-character string generated once per entry and stored in `entry.data`. Registered with `webhook.async_register(..., local_only=False, allowed_methods=["POST"])`.
- Secret: `secrets.token_urlsafe(32)`, stored in `entry.data` as `push_secret`. After a rotation, `push_secret_previous` stays valid for 10 minutes so pushes already in flight are not rejected. The secret is stored rather than kept only in memory: if U-tec is down when HA restarts and re-registration fails, pushes signed with the last registered secret are still accepted.
- Registration runs at setup, every 24 h (1 request per day), from the "Re-register push" button (5-minute cooldown), and automatically when push health turns unhealthy (at most once per 6 hours).
- Registration failure: retry after 5 min, 15 min, 1 h, then every 6 h. Status `registration_failed`.
- Unload unregisters the HA webhook handler. Entry removal also deletes the cloudhook. U-tec has no documented unregister call (P7), so it may keep posting to a dead URL, which HA answers with 404 or 401. The README says so.
- Writing `entry.data` (secret rotation, token refresh) must never trigger a reload: the update listener only acts on option changes.

## 6.4 Handling a push

1. POST only. Body limit 64 KB. Invalid JSON gets 400.
2. `Authorization: Bearer <secret>` is compared in constant time with the current secret and, inside its grace window, the previous one. Missing or wrong gets 401. Rejections are counted; ten or more in an hour log one warning (possible probing, or a stale registration from another install).
3. Normalize: accept the documented envelope, a bare list, or `payload` as a list. `states` may be a list of `{capability,name,value}` or a mapping. Capability names, attribute names, and enum values are compared case-insensitively.
4. Dispatch on `header.name`. `DeviceState` (or no header, with entries that carry states) becomes `LockReport(source=PUSH)` for known locks. `DeviceSync` and `DeviceDelete` trigger a debounced discovery, as do entries with unknown ids.
5. Respond 200 when processing finishes (in-memory work only).
6. A push never causes a Query, apart from the debounced discovery in step 4.

Logging: header names and top-level payload keys only. Never bodies, tokens, or secrets.

## 6.5 Push health: evidence, not silence

Silence proves nothing; locks nobody touches send nothing. So health is scored on evidence events:

- **Hit:** a change (lockState, lockMode, or door) that polling or a confirmation observes had already been delivered by a push for that lock since the previous observation.
- **Miss:** polling or a confirmation observes a change and no push delivers it within 30 s afterward.
- **Late:** a push delivers the value after polling did. Recorded with its latency; not counted as a hit.
- For our own commands: confirmation by push is a hit. Confirmation by query with no matching push within `deferred_seconds + 30 s` is a miss.

| State | Rule | Meaning in the UI |
|---|---|---|
| `disabled` | Push turned off in options | Push not used |
| `no_url` | No HTTPS URL available | Push not possible |
| `registration_failed` | Last registration call failed | Retrying on schedule |
| `unverified` | Registered, fewer than 3 evidence events | Not enough evidence yet |
| `healthy` | At least 4 of the last 5 events are hits, and the newest is a hit | Push is delivering changes |
| `degraded` | Between healthy and unhealthy | Push is patchy |
| `unhealthy` | The two newest events are misses | Push is not delivering changes |

Slow to trust, quick to distrust: one miss drops `healthy` to `degraded`, two in a row make it `unhealthy`.

## 6.6 What happens when push is unhealthy

- Background polling continues at the configured interval, never below 30 s. **There is no faster fallback.** The floor is firm. Speeding up whenever push breaks would turn every vendor push outage into a simultaneous load spike from every install, which is exactly the pattern that gets shared APIs locked down.
- Command confirmation still runs (Section 8), so changes made from HA still confirm in seconds.
- At most one automatic re-registration every 6 hours.
- After 24 hours of `unhealthy` or `registration_failed`, the `push_unhealthy` repair appears. It explains that state can lag behind changes made at the lock or in the U-tec app and how to check the external URL or Nabu Casa. It clears itself when push is healthy again. The 24-hour delay avoids nagging during short vendor incidents.

## 6.7 Optional: slow down polling while push is healthy (off by default)

Option "Slow down polling while push is healthy", default **off** in v1, with "Interval while push is healthy" from 60 to 1,800 s, default / recommended 120 s. When on, the coordinator uses the relaxed interval only in `healthy` and returns to the base interval at the first miss. This is the fair-use lever for users whose push works. It starts off because push has a poor record; whether to turn it on by default later can wait on soak evidence.

**Decided (Rob, October 6, 2026):** include the option; off by default; recommended relaxed interval **120 s**.

---

# 7. State model and availability

## 7.1 Lock entity state

| Condition | `is_locked` | Other flags | Notes |
|---|---|---|---|
| Fresh report `Locked` / `Unlocked` | True / False | | Normal |
| Fresh report `Jammed` | None | `is_jammed=True` | HA shows `jammed` |
| Fresh report `Unknown`, or health `Offline` | None | | Shows `unknown`; attribute `cloud_status: offline` |
| Stale (no fresh `lockState` within `stale_after`) | None | | Shows `unknown`; `stale: true`, `last_known_state`, `last_reported` |
| Command pending (Section 8) | as reported | `is_locking` or `is_unlocking` True | Transitional state until confirmed or timed out |

A lock the cloud reports as offline shows `unknown` rather than its last cached `lockState`: the cloud's cached value for an offline lock is not evidence of where the bolt is now. **Decided (Rob, October 6, 2026):** show `unknown` (not last known state).

Lock entity attributes (`last_reported` and similar excluded from the recorder via `_unrecorded_attributes` where they would churn):

| Attribute | Example | Purpose |
|---|---|---|
| `last_reported` | ISO time | When a report carrying lockState last arrived |
| `last_report_source` | `poll` / `push` / `confirm` | Where it came from |
| `stale` | `false` | True when older than `stale_after` |
| `last_known_state` | `locked` | Kept while stale or offline |
| `lock_mode` | `normal` | Mirrors the select, handy in templates |
| `cloud_status` | `online` | From `st.healthCheck` |
| `pending_command` | `unlock` / null | While a confirmation runs |
| `last_command_result` | `confirmed` / `not_confirmed` / `rejected:DEVICE_OFFLINE` | Last outcome |

## 7.2 Availability

Rob's rule is adopted: locks stay available when their state is stale, so lock and unlock keep working. Verified in HA core: the entity service helper skips unavailable entities without raising (`homeassistant/helpers/service.py`, `if not entity.available: continue`). Someone pressing "Unlock" on an unavailable lock would get nothing at all.

An entity is **unavailable** only when:

1. its lock is gone from the account (missing from two consecutive successful discoveries), or
2. the config entry is not loaded.

Polling failures, auth failures, push failures, offline locks, and stale data all leave entities available, shown as `unknown` plus attributes and diagnostic sensors. While reauth is pending, commands fail with a translated "Re-authentication required" error instead of silently doing nothing.

This departs from the literal Silver `entity-unavailable` rule. The deviation and its reason are recorded in `quality_scale.yaml` (Section 13).

---

# 8. Command path

## 8.1 The security-command rule

**Lock, unlock, and lock-mode changes are always sent when the user or an automation asks.** No cached state can suppress them:

- not "already locked" or "already unlocked",
- not "already in that mode",
- not cached Passage mode,
- not stale or unknown state, not cloud offline,
- not push health, not background backoff or an open circuit.

**Decided (Rob, October 6, 2026):** always send lock, unlock, and mode commands regardless of Passage, cache, stale, or offline — **always send, no pre-check**. A pre-check would cost the same single request as the command, add delay to a security action, and still cannot be fully trusted given eventual consistency. If the cached mode is Passage when a lock command arrives, the command is still sent, a warning is logged ("Front Door is in Passage mode; the lock may ignore lock commands until the mode is Normal"), and the confirmation result tells the user what actually happened. **[Field] Latch-5-NFC (October 6, 2026):** unlock while already in Passage produces an odd beep and no change; Passage itself auto-unlocks and holds unlocked (including through a 5+ minute auto-lock window). Lock-while-in-Passage on other models remains model-dependent; the warning stays.

The only thing that can stop a command reaching U-tec is U-tec itself (errors, rate limiting), and the user always sees that as an error.

## 8.2 Pipeline

```
async_lock() / async_unlock() / select.async_select_option(mode):
  async with per_lock_mutex[device_id]:        # serialize per lock; different locks run in parallel
      cancel any pending confirmation for this lock (the new command supersedes it)
      pending = {kind, expected, started_at}; write state (locking / unlocking)
      receipt = await client.command(device_id, capability, name, args)   # 15 s timeout
      ... classify the reply (8.4) ...
      commands.confirm(device_id, expected, receipt.deferred_seconds)
  return                                        # the service call does not wait for confirmation
```

The service call returns once U-tec accepts the command. Holding a service call open for the whole confirmation window (about 56 s) would block scripts and the UI. Automations that need the outcome wait for the lock state or the command-result event entity.

## 8.3 Confirmation (the bounded burst)

After a command is accepted, the integration reads back only the locks that have a pending command:

- **Schedule:** successive intervals of **1, 1, 1, 1, 2, 3, 5, 8, 13, 21 seconds** after the command reply (Fibonacci-like; absolute times **1, 2, 3, 4, 6, 9, 14, 22, 35, 56 s**). **At most 10 queries per command**, all within about 56 s. **Decided (Rob, October 6, 2026).**
- **No hourly confirmation budget or cap.** Every accepted command gets the full confirmation schedule. **Decided (Rob, October 6, 2026).**
- **Batched:** confirmation is account-level. Each tick sends one Query for every lock with a pending command, so "lock all doors" on four locks costs 10 queries, not 40.
- **Merged with background polls:** if a background poll is due within 3 s of a tick, the tick is skipped and the background poll serves.
- **Ends early** as soon as a push or any query shows the expected value (lockState for lock/unlock, lockMode for setMode). A battery or door report can never confirm a command.
- **Deferred hint ignored by default:** the full 1/1/1/1/2/3/5/8/13/21 s schedule runs after every accepted lock, unlock and setMode, whatever the reply's `deferred_seconds` says. **Decided (Rob, October 6, 2026).** A switch (`HONOR_DEFERRED_HINT`, default off) can make the burst skip ticks that would fire before `deferred_seconds`; later ticks keep their absolute times. Either way the schedule never runs past 56 s, and `deferred_seconds` is still used for the push-evidence window (section 6).
- **Timeout:** not confirmed by about 90 s (the 56 s burst plus at least one background poll) means `not_confirmed`. The transitional state clears, the entity shows the latest reported state (or unknown if stale), a WARNING is logged, the lock's event entity fires `not_confirmed`, and the bus event `utec_locks_command_result` fires with `device_id`, `command`, `expected`, `result`, `seconds`, `queries`.
- **Contradiction check:** if, within 10 s of a confirmation, a report contradicts it (eventual consistency, a stale cloud cache), that report is held and one extra query decides. The newest real observation wins; nothing is invented.

**How this fits the 30-second floor.** The floor governs background, unsolicited polling: the cost every install pays all day. Confirmation queries happen only because a user acted, are capped at 10 per command, end within about 56 s, and cover only pending locks. At 10 commands a day that is at most 100 extra queries, under 4% of the 2,880 daily background queries. Without them, every command would sit in `locking` for up to 30 s, and push outages would make HA-initiated actions feel broken, which is what pushed people toward 1-second polling in the first place.

## 8.4 Command reply classification and retries

| Reply | Handling |
|---|---|
| 2xx, device entry with `st.deferredResponse` | Accepted. Start confirmation. |
| 2xx, device entry with `error` (e.g. `DEVICE_OFFLINE`) | Rejected. Clear pending. Raise a translated `HomeAssistantError` naming the code. Event `rejected`. No confirmation. |
| 2xx, top-level `INVALID_TOKEN` | Not executed. Force a token refresh and retry once. If it repeats, start reauth and raise "Re-authentication required". |
| 2xx, other top-level error | Raise with the code. No retry; we cannot know a retry is safe. |
| 429 | If `Retry-After` is 5 s or less, wait and retry once. Otherwise raise "U-tec is rate limiting requests; try again in N s". Never queued for later. |
| 5xx, timeout, connection error | Ambiguous: the command may have reached the lock. Retry once after 1 to 2 s with jitter (lock, unlock and setMode are idempotent). If the retry fails too, raise "U-tec did not confirm it received the command; the lock may still act. Home Assistant is checking." and **still run confirmation**, so the user learns the real outcome either way. |

No command is retried after the user's call has returned, and nothing is queued for later. A "surprise unlock" minutes after someone gave up is worse than a clear error.

## 8.5 Loop detection (guidance, not blocking)

- **No confirmation budget.** Confirmation always runs the full schedule for every accepted command (Section 8.3). **Decided (Rob, October 6, 2026).**
- **Command loop detection:** more than 6 commands to one lock within 10 minutes raises a `command_loop` repair (for example an automation fighting latch auto-lock) that suggests Passage mode. It never blocks a command.

## 8.6 Optimistic state

None. Commands are accepted without being executed often enough to matter (A22), and an optimistic `locked` shown while the door is actually unlocked is the worst failure a lock integration can have. HA's lock entity supports `locking` and `unlocking` as real states, which are honest and work in dashboards and automations. No opt-in optimistic mode in v1.

**Decided (Rob, October 6, 2026):** no optimistic locked/unlocked state; no opt-in optimistic mode in v1.

## 8.7 Concurrency

- A per-lock `asyncio.Lock` serializes commands for one lock. A second command waits until the first is accepted (not confirmed), then supersedes its confirmation.
- Different locks run in parallel. `PARALLEL_UPDATES = 0` on all platforms; the per-lock mutex is the real limit.
- All timers are cancelled on unload. Every confirmation tick checks after each await that its confirmation is still the current one.

---

# 9. Entities

## 9.1 Per lock (device = the lock; manufacturer, model and hardware version from discovery)

| Entity | Platform | Unique id suffix | Category | Default | Notes |
|---|---|---|---|---|---|
| Lock (named after the device) | `lock` | (device id) | | on | Lock and unlock. No `open` (no API command). Attributes in 7.1. |
| Lock mode | `select` | `_lock_mode` | config | on | Options `normal`, `passage`, `locked` (shown as "Locked (mode 2)"). Shows only the reported mode; none when unknown or stale. Selecting always sends `setMode`. Attribute `pending_mode`. |
| Door | `binary_sensor` (door) | `_door` | | on | Only for `utec-lock-sensor` locks or when `doorSensor` shows up in states. Unknown when stale. |
| Battery | `binary_sensor` (battery) | `_battery_low` | diagnostic | on | On (low) when level is 2 or below. |
| Battery level | `sensor` (enum) | `_battery_level` | diagnostic | on | `critically_low`, `low`, `medium`, `high`, `full` (the API's 1..5). |
| Battery (percent) | `sensor` (battery, %) | `_battery_pct` | diagnostic | **off** | Documented mapping level x 20, for cards that need a percentage. Off by default because the API has no real percentage. |
| Status stale | `binary_sensor` (problem) | `_stale` | diagnostic | on | On when lock state is older than `stale_after`. For alerts and automations. |
| Cloud connection | `binary_sensor` (connectivity) | `_cloud` | diagnostic | on | From `st.healthCheck`. |
| Last report | `sensor` (timestamp) | `_last_report` | diagnostic | off | Time of the last report carrying lockState. |
| Command result | `event` | `_command_result` | | on | Event types `confirmed`, `not_confirmed`, `rejected`, with command, expected value, seconds and query count. |

**Decided (Rob, October 6, 2026):** battery entities are low binary sensor **on**, 5-step enum **on**, percent sensor **off** by default.

Mode 2 ("Locked") is offered because the API documents it, and labeled "Locked (mode 2)" because U-tec does not describe it in docs. **[Field] Latch-5-NFC (October 6, 2026):** entering Locked locks the door, RFID flashes red / denied, and HA shows locked. **Decided (Rob, October 6, 2026):** show mode 2 in the lock-mode select from day one; do not hide it until tested. Other models still need soak coverage.

## 9.2 Account (device "U-tec account", `entry_type=service`)

| Entity | Platform | Default | Persisted | Notes |
|---|---|---|---|---|
| API requests | `sensor`, total_increasing | on | yes | Every call to `/action`. Attributes: counts per kind (query, confirm_query, command, discovery, push_register, user) and `counting_since`. |
| API requests (last 24 h) | `sensor`, measurement | on | yes (hourly buckets) | Rolling. |
| API requests (last hour) | `sensor`, measurement | off | yes (minute buckets) | Rolling. |
| Projected requests per day | `sensor`, measurement | on | no | 86,400 / effective interval + discovery + registrations + the last 24 h of command overhead. "What do my settings cost." |
| API errors | `sensor`, total_increasing | on | yes | Attributes by class: http_5xx, http_429, timeout, connection, envelope (by code), auth. |
| Commands sent | `sensor`, total_increasing | off | yes | Attributes: confirmed, not_confirmed, rejected. |
| Confirmation queries | `sensor`, total_increasing | off | yes | Count of confirmation Query requests. |
| Pushes received | `sensor`, total_increasing | off | yes | Attributes: applied, rejected_auth, malformed, sync, delete. |
| Push status | `sensor`, enum | on | no | States from 6.5. |
| Push healthy | `binary_sensor` (connectivity) | on | no | On only in `healthy`; unknown while `unverified`. |
| Last push | `sensor`, timestamp | on | yes | Any authenticated push. |
| Poll interval in use | `sensor` (duration, s) | off | no | Base, relaxed, or backoff value. |
| Re-register push | `button` | off | | 5-minute cooldown. |

Per-kind counts are attributes rather than separate sensors to keep the entity list short while every number stays visible and usable in templates. Separate sensors can be added later if users ask.

---

# 10. API usage accounting and persistence

- `UsageMeter` wraps the client's single request method, so every request is counted at one choke point: kind, outcome class, latency, envelope code.
- Totals persist with HA's `Store` in `.storage/utec_locks.usage.<entry_id>` (version 1, minor 1, with a migration function that keeps whatever it recognizes).
- Saves use `async_delay_save` with a 60-second delay (many changes, one write), plus an immediate save on unload and HA stop. A corrupt file is logged and counting restarts from zero; it never blocks setup.
- Rolling windows persist as 24 hourly and 60 per-minute buckets, so "last 24 h" survives a restart.
- `counting_since` is stored and shown. Totals are deleted in `async_remove_entry`.
- Token refreshes (OAuth endpoint, not `/action`) are counted separately for diagnostics.
- Diagnostics include the full usage snapshot.

---

# 11. Configuration, reauth, diagnostics, repairs

## 11.1 Config flow (OAuth through application credentials)

The vendor requires the OAuth2 authorization-code flow (A4) with per-user client credentials (A8). The flow uses HA's standard building blocks instead of a custom credential form:

1. `application_credentials.py` supplies the authorization server (authorize and token URLs) and a custom `AuthImplementation` subclass that unwraps the `{code,data}` token envelope for both code exchange and refresh (A5).
2. `config_flow.py` subclasses `AbstractOAuth2FlowHandler` with `extra_authorize_data = {"scope": "openapi"}`. If no credential exists yet, HA's standard prompt asks for the Client ID and Secret, with description text linking to the README's Xthings app steps and naming the redirect URI `https://my.home-assistant.io/redirect/oauth`.
3. After the token: call `Uhome.User / Get` and use the returned user id as the entry's `unique_id` (Bronze `unique-config-entry`; also allows several U-tec accounts). If that call fails or returns no id **[Unknown: the old integration never used it]**, fall back to a SHA-256 hash of the client id, noted in diagnostics.
4. Run Discovery. If it finds no locks, abort with `no_locks` ("Check that your locks are selected under OpenAPI in the Xthings app"). This also covers `test-before-configure`.
5. Create the entry titled "U-tec", plus the account's first name if returned (no other personal data).

The client secret is not put in the browser's authorize URL (A7). It is not needed in practice and would end up in browser history.

## 11.2 Options

| Option | Range | Default |
|---|---|---|
| Polling interval | 30 to 3,600 s (30 is the floor and cannot be lowered) | 30 s |
| Use push notifications | on / off | on |
| Slow down polling while push is healthy | on / off | off |
| Interval while push is healthy | 60 to 1,800 s | 120 s |
| Confirm commands with quick checks | on / off | on |

Options apply live through an update listener that reacts only to `entry.options` changes. Token refreshes and secret rotation write `entry.data` and must never cause a reload or re-registration.

## 11.3 Reauth and reconfigure

- **Reauth:** triggered by `ConfigEntryAuthFailed`. `async_step_reauth`, then `reauth_confirm`, then OAuth. After the token, the user id must match (`_abort_if_unique_id_mismatch`) so a different account cannot be swapped in. On success, `async_update_reload_and_abort`.
- **Reconfigure** (Gold): re-runs OAuth, for example after the user rotates the OpenAPI Client Secret in the Xthings app. Same account check.

## 11.4 Diagnostics (config entry and device)

Included: integration and HA versions; options; coordinator timing (interval in use, last success and failure, backoff level); push status and the last 20 evidence events; the last 20 API exchanges in summary (kind, HTTP status, envelope code, latency; no bodies); the usage snapshot; discovery records; and the last raw state report per lock.

Redacted with `async_redact_data`: `access_token`, `refresh_token`, `client_id`, `client_secret`, `push_secret`, `push_secret_previous`, `webhook_id`, webhook and cloudhook URLs, user id, first and last name, serial numbers, `customData`. Device ids are replaced with a stable short hash (for example `lock_3f9a`) so a user can still point at "this lock" in a bug report without publishing a MAC address.

## 11.5 Repairs

| Issue id | When | Severity | Fixable |
|---|---|---|---|
| `push_unhealthy` | Push unhealthy or registration failing for 24 h | warning | No (guidance); clears itself |
| `push_no_https_url` | Push on, but no cloudhook and no HTTPS external URL | warning | No; can be ignored |
| `rate_limited` | 3 or more 429s in an hour | error | No; clears after 24 h without a 429 |
| `conflicting_integration` | (setup-time only) A loaded `u_tec` config entry exists | error | No; **setup is blocked** until `u_tec` is disabled or removed |
| `command_loop` | More than 6 commands to one lock in 10 min | warning | No; suggests Passage mode |
| `lock_removed` | Lock missing from 2 discoveries | warning | Yes: a repair flow removes the device |

---

# 12. Errors, logging, and the API client

## 12.1 Client responsibilities (`api/`)

- Build requests (A1). Add the Bearer token from HA's `OAuth2Session.async_ensure_token_valid`, the User-Agent, and timeouts (15 s for Query and Command, 20 s for Discovery).
- Use HA's shared `aiohttp` session (`async_get_clientsession`), which covers Platinum's `inject-websession`.
- Classify every response as one of: `Ok(payload)`, `AuthError`, `RateLimited(retry_after)`, `ServerError`, `EnvelopeError(code)`, `TransportError`, with per-device errors attached to `Ok`. All exceptions inherit from `UtecError`.
- Parse into the dataclasses of 4.2 with case-insensitive matching (A15 to A17).
- Never log bodies, tokens, or secrets. Debug logs show namespace and name, device count, status, latency, and envelope code.

## 12.2 Response classification

```
HTTP 401/403                 -> AuthError
HTTP 429                     -> RateLimited(Retry-After)
HTTP 5xx                     -> ServerError
HTTP 2xx, payload.error      -> INVALID_TOKEN: AuthError; anything else: EnvelopeError(code)
HTTP 2xx, devices[].error    -> per-device error recorded on that device (not a request failure)
HTTP 2xx, otherwise          -> Ok
HTTP other 4xx               -> EnvelopeError("HTTP_<status>")
TimeoutError, aiohttp.ClientError -> TransportError
```

## 12.3 Errors shown to users

All user-facing errors use `HomeAssistantError(translation_domain=DOMAIN, translation_key=...)` (Gold `exception-translations`): `device_offline`, `command_rejected` (with the code), `rate_limited` (with seconds), `command_outcome_unknown`, `reauth_required`, `api_unreachable`.

---

# 13. Quality scale alignment

Target: **Silver complete, Gold mostly done**, Platinum in part. A `quality_scale.yaml` with these statuses ships in the integration folder. (The quality scale is a core program: custom integrations can follow it but are not officially ranked.)

| Tier | Rule | Status | How |
|---|---|---|---|
| Bronze | action-setup | exempt | No custom service actions |
| | appropriate-polling | done (deviation noted) | 30 s floor, default 30 s, batched. Core policy discourages user-set intervals; kept configurable upward on purpose |
| | brands | done | `brand/` folder (HA 2026.3+), neutral artwork not the U-tec logo (risk 18.9; decided Oct 6, 2026) |
| | common-modules | done | `entity.py`, `coordinator.py`, `api/` |
| | config-flow, config-flow-test-coverage | done | Every flow branch tested |
| | dependency-transparency | done | No requirements beyond HA; client in repo |
| | docs-actions, docs-triggers, docs-conditions | exempt | None provided |
| | docs-high-level-description, docs-installation-instructions, docs-removal-instructions | done | README |
| | entity-event-setup | done | Dispatcher and timers in `async_added_to_hass`, cleaned up with `async_on_remove` |
| | entity-unique-id, has-entity-name | done | |
| | runtime-data | done | `entry.runtime_data` |
| | test-before-configure, test-before-setup | done | User Get and Discovery in the flow; Discovery and Query in setup (`ConfigEntryNotReady` / `ConfigEntryAuthFailed`) |
| | unique-config-entry | done | U-tec user id |
| Silver | action-exceptions | done | Translated `HomeAssistantError` on every failure path |
| | config-entry-unloading | done | Cancels timers, unregisters the webhook, flushes the Store |
| | docs-configuration-parameters, docs-installation-parameters | done | README tables |
| | entity-unavailable | done, documented deviation | Unavailable only when the lock is gone; otherwise `unknown` plus staleness, because HA skips service calls to unavailable entities (Rob's rule) |
| | integration-owner | done | Rob sole owner in `codeowners` (outside PRs welcome; no co-owners for now) |
| | log-when-unavailable | done | Coordinator logs once down and once recovered; push status changes logged once |
| | parallel-updates | done | `PARALLEL_UPDATES = 0` plus a per-lock mutex |
| | reauthentication-flow | done | 11.3 |
| | test-coverage | done (95% gate) | CI fails under 95% |
| Gold | devices | done | Lock devices plus the account service device |
| | diagnostics | done | 11.4 |
| | discovery, discovery-update-info | exempt | Cloud API; nothing to discover locally |
| | docs-data-update, docs-examples, docs-known-limitations, docs-supported-devices, docs-supported-functions, docs-troubleshooting, docs-use-cases | done | README sections |
| | dynamic-devices | done | Discovery every 6 h and on DeviceSync |
| | entity-category, entity-device-class, entity-disabled-by-default | done | Section 9 |
| | entity-translations, exception-translations, icon-translations | done | `strings.json`, `icons.json` |
| | reconfiguration-flow | done | 11.3 |
| | repair-issues | done | 11.5 |
| | stale-devices | done | `lock_removed` repair flow plus `async_remove_config_entry_device` |
| Platinum | async-dependency, inject-websession | done | In-repo async client on HA's shared session |
| | strict-typing | goal | `mypy --strict` in CI |

---

# 14. Testing plan

## 14.1 Tooling

- `pytest-homeassistant-custom-component` pinned to the minimum supported HA version in one CI job, latest release in a second job.
- `aioresponses` for HTTP-level tests of `api/`. A `FakeUtecCloud` fixture (an in-memory account with locks, scripted replies, and a call log) for integration tests, so tests check behavior and request counts rather than mocking internals.
- HA's `async_fire_time_changed` plus `freezegun` for timers; `syrupy` snapshots for entity states and diagnostics.
- JSON fixtures: discovery (lock, lock-sensor, non-lock devices), Query replies (normal, mixed casing, missing capabilities, per-device error, offline), command replies (deferred 20, deferred 5, device error), envelopes (`INVALID_TOKEN`, unknown code), token responses (wrapped, standard, error), push payloads (documented envelope, bare list, payload-as-list, mapping-style states, lowercase values, `{'id':...}` without states, DeviceSync, DeviceDelete).

## 14.2 Must-have test cases (abridged)

**Floor and scheduling:** values below 30 clamped at load; options form rejects below 30; a manual refresh within 30 s makes no request; scheduled polls never closer than 29.5 s; startup jitter within [0, base]; backoff growth, cap, jitter bounds, and reset; 429 with and without Retry-After; circuit open and close; discovery cadence and debounce.

**Security-command rule:** lock sent when cached locked; unlock sent when cached unlocked; lock sent in cached Passage, with the warning; setMode sent when the cached mode equals the target; commands sent while stale, offline, in backoff, and with push unhealthy; a service call reaches the entity while its state is unknown (entity available).

**Command outcomes:** accepted then confirmed by push, by the burst, and by a background poll; not confirmed at about 90 s (event, log, transitional state cleared); rejected `DEVICE_OFFLINE` (translated error, no burst); `INVALID_TOKEN` refresh-and-retry once, then reauth; 5xx retry once, then `command_outcome_unknown` with confirmation still running; 429 with short and long Retry-After; a new command supersedes a pending confirmation; two pending locks share one query per tick; the full 10-tick schedule runs with no hourly budget; the contradiction check uses exactly one query; a battery or door report never confirms; the full schedule runs despite a `deferred_seconds` hint (and ticks before it are skipped only when the switch is on).

**Push:** URL selection (cloudhook, HTTPS external, none, private address refused); Bearer check (missing, wrong, current, previous inside and after the grace window); oversized body; non-POST; every payload shape; unknown ids trigger one debounced discovery; DeviceSync and DeviceDelete; a push never triggers a Query; health transitions (hits, misses, late); relaxed interval only while healthy and dropped at the first miss; 24-hour repair timing; re-registration schedule and cooldowns; secret rotation.

**State and availability:** fresh, stale, jammed, unknown, and offline mapping; a battery-only push does not refresh lock freshness; a stale flip writes state once; entities stay available through poll and auth failures; unavailable after two discoveries without the lock.

**Usage persistence:** totals survive reload and restart (Store round trip); delayed saves coalesce; unload flushes; a corrupt file is tolerated; removal deletes; rolling buckets stay correct across a restart.

**Config flow:** OAuth happy path with a wrapped token; standard token passthrough; token error envelope; `no_locks` abort; duplicate account abort; User Get failure fallback; reauth with the same and a different account; reconfigure; options validation.

**Diagnostics:** snapshot test proving every redacted field is absent.

## 14.3 CI

GitHub Actions: `ruff` (lint and format), `mypy --strict`, `pytest --cov` with `fail_under = 95`, the `hassfest` action, and the HACS validation action. All required on pull requests.

## 14.4 Hardware soak (manual, before 1.0)

At least two weeks on Rob's locks, including the shop latch, with a written checklist:

1. Lock and unlock from HA: time to confirm, and whether a push arrived. **Partial [Field] Latch-5-NFC:** lock/unlock works; confirmation schedule 1/1/1/1/2/3/5/8/13/21 felt more responsive (v0.1.1). Push timing still open.
2. Keypad and thumb-turn changes: does a push arrive at all (P10)? How long until polling shows the change?
3. `setMode` to Passage and back to Normal: confirmation, time, push. Mode 2: what it does and how to leave it (test with a physical key at hand). **Done for Latch-5-NFC [Field]:** Passage auto-unlocks and holds; Normal locks and behaves normally; Locked (mode 2) locks door, RFID denied (red flash), HA shows locked. Push timing and other models still open.
4. Lock / unlock while in Passage: ignored, rejected, or performed? **Partial [Field] Latch-5-NFC:** unlock while in Passage → odd beep, no change. Lock-while-in-Passage still open for other models.
5. Behavior with and without `customData` in Query and Command (A11).
6. Push secret rotation: any pushes rejected around the rotation?
7. Daily request totals compared with the projection sensor.

A standalone, read-only-by-default probe script (in the spirit of `test_set_mode.py`, which only sends when given `--send` and exactly one lock) ships in `scripts/` for these checks.

---

# 15. Packaging, HACS, versioning

- **Repository layout:** `custom_components/utec_locks/`, `tests/`, `scripts/`, `README.md`, `CHANGELOG.md`, `LICENSE` (MIT; **decided Rob, October 6, 2026**), `hacs.json`, `.github/workflows/`. `quality_scale.yaml` lives inside the integration folder.
- **hacs.json:** `{"name": "U-tec Locks", "homeassistant": "2026.3.0", "render_readme": true, "zip_release": true, "filename": "utec_locks.zip"}`. Minimum HA **2026.3.0** because that release added local `brand/` images for custom integrations. **Decided (Rob, October 6, 2026).**
- **manifest.json:** `domain`, `name`, `version`, `codeowners: ["@rbridal"]`, `config_flow: true`, `dependencies: ["application_credentials", "webhook"]`, `after_dependencies: ["cloud"]`, `iot_class: "cloud_polling"`, `integration_type: "hub"`, `documentation`, `issue_tracker`, `requirements: []`, `loggers: ["custom_components.utec_locks"]`. `iot_class` is `cloud_polling` because polling is the guaranteed path and push is opportunistic.
- **Distribution:** start as a HACS custom repository. Submitting to the HACS default list later needs brand assets, releases, and passing HACS validation. **(Rob's call** on timing.)
- **Versioning:** SemVer. 0.x until the soak checklist passes, then 1.0.0. Tags `vX.Y.Z`; GitHub Releases with the zip built by the release workflow; Keep a Changelog. Soak builds as pre-releases (HACS can offer them to users who opt in).
- **Config entry versioning:** `VERSION = 1`, `MINOR_VERSION = 1`, and `async_migrate_entry` from day one. The Store has its own version and migration.

---

# 16. Module layout and size estimate

## 16.1 Proposed layout

| File | Responsibility | Est. lines |
|---|---|---:|
| `__init__.py` | Setup, unload, remove; runtime_data wiring; platform forwarding | 150 |
| `const.py` | Constants (floor, timings, keys) | 80 |
| `application_credentials.py` | Auth server, token-unwrapping implementation | 50 |
| `config_flow.py` | OAuth flow, account check, reauth, reconfigure, options | 260 |
| `api/__init__.py`, `api/errors.py` | Exports, exception hierarchy | 70 |
| `api/client.py` | Request building, envelope classification, timeouts, User-Agent | 300 |
| `api/models.py` | Dataclasses, enums, lenient case-insensitive parsing | 230 |
| `coordinator.py` | Account coordinator, floor guard, backoff, jitter, discovery, staleness clock | 340 |
| `state.py` | LockStateStore and freshness rules | 120 |
| `commands.py` | Executor, per-lock mutex, batched confirmation, loop detection, events | 320 |
| `push.py` | URL selection, registration, rotation, webhook handler, normalization | 300 |
| `push_health.py` | Evidence model and state machine | 150 |
| `usage.py` | UsageMeter, rolling buckets, Store persistence | 240 |
| `entity.py` | Base classes, device info | 80 |
| `lock.py`, `select.py`, `event.py`, `button.py` | Action and event platforms | 330 |
| `sensor.py`, `binary_sensor.py` | Per-lock and account sensors (entity descriptions) | 380 |
| `diagnostics.py`, `repairs.py` | Diagnostics with redaction; repair issues and the remove-lock flow | 200 |
| **Python total (estimate)** | | **about 3,600 (range 3,300 to 3,900)** |
| `strings.json`, `translations/en.json`, `icons.json`, `manifest.json`, `quality_scale.yaml` | | about 750 |
| `tests/` | | about 5,000 to 6,500 |

## 16.2 Measured size of the existing project

Measured on October 6, 2026 from the local checkout `/workspace/uhome-debug-mode`, reading every `.py` file with `git show <ref>:<file>` (method in Appendix C). "Code" excludes blank lines, comments, and docstrings.

| Codebase | Integration .py files | Raw lines | Non-blank | Code | Tests (raw / code) |
|---|---:|---:|---:|---:|---:|
| Upstream `origin/main` (v0.6.1, `f7bd55d`) | 15 | 3,615 | 3,097 | 2,530 | 6,047 / 4,125 |
| Our `feat/lock-mode-select` (`8f44145`) | 21 | 5,316 | 4,587 | 3,813 | 7,842 / 5,553 |
| `utec-client` 0.5.0 / 0.5.1 (runtime dependency; identical code) | 10 | 1,057 | | | |
| of which lock-relevant (api, auth, const, exceptions, device, device_const, lock) | 7 | 873 | | | |

The existing integration also ships `strings.json` and `translations/en.json`, 263 lines each on the lock-mode branch (130 each upstream). README: 93 lines upstream, 255 on our branch.

## 16.3 Comparison

| | Python it owns | Plus runtime library | Effective total |
|---|---:|---:|---:|
| Upstream `u_tec` v0.6.1 | 3,615 | 1,057 | 4,672 |
| Our `feat/lock-mode-select` | 5,316 | 1,057 | 6,373 |
| **New `utec_locks` (estimate)** | **about 3,600** (including its own client, about 600) | 0 | **about 3,600** |

The rewrite is estimated at roughly the size of upstream's integration code alone: about 23% smaller than upstream counting its library, and about 44% smaller than our current branch counting its library, while adding push-health scoring, persisted rolling usage, repairs, account checks on reauth, reconfigure, event entities, and stricter error handling. The savings come from dropping lights and switches (526 lines in `light.py` and `switch.py` alone on our branch, plus their option steps), debug polling (243 lines in `debug_polling.py` and `button.py`), the custom credential form and its migrations (much of the 660-line `config_flow.py` and about 105 lines of migration code in `__init__.py`), and using HA's standard application-credentials flow.

New-code numbers are estimates from the layout above, not measurements. Expect +/-15%.

---

# 17. Naming, coexistence and migration

## 17.1 Domain and repository

What I found (checked before the decision):

- `u_tec` is taken twice: by the existing custom integration and by HA core's **brand** entry `homeassistant/brands/u_tec.json` (verified; it lists `ultraloq`).
- `ultraloq` is an HA core **virtual integration** (Z-Wave; verified in `homeassistant/components/ultraloq/manifest.json`). A custom integration with that domain would override a core domain, which HA advises against.
- `xthings_cloud` is the vendor's core integration.
- Not found in HA core components, the brands repo's `custom_integrations` folder, or the HACS default list (checked October 6, 2026): `utec_locks`, `ultraloq_openapi`, `uhome_locks`. (Not checked: every custom repository on GitHub. A quick code search before the first tag is worthwhile.)

**Decided (Rob, October 6, 2026):** domain **`utec_locks`**, display name **"U-tec Locks"** (README subtitle "community integration for the U-tec OpenAPI"), repository **`rbridal/ha-utec-locks`**. (The product-name alternative `ultraloq_openapi` / `ha-ultraloq-openapi` was not chosen.) A domain cannot change after release without breaking every install.

## 17.2 Conflict with `u_tec`: setup is blocked

Different domains mean both *could* be installed, but **setup of U-tec Locks is blocked** while any loaded `u_tec` config entry exists. **Decided (Rob, October 6, 2026):** block setup (not just warn). Reasons:

1. **Push conflict [Assumed, P8]:** each integration registers its own URL. The last one wins and the other silently loses push. Both re-register daily, so they keep taking it from each other.
2. **Double API load:** two coordinators polling the same account.
3. **Two entities per lock**, easy to mix up in automations.

At config-flow and entry-setup time, if a loaded `u_tec` entry is found, setup aborts with a clear error (and the `conflicting_integration` repair explains that `u_tec` must be disabled or removed first). The new integration never touches the other integration's files or entries.

## 17.3 Migration steps (README)

1. List the automations, scripts and dashboards that use `lock.*` entities from `u_tec`.
2. Disable (or delete) the `u_tec` config entry.
3. Install and set up U-tec Locks. The same OpenAPI Client ID and Secret can be reused. Whether two live OAuth grants on one client invalidate each other's refresh tokens is [Unknown], one more reason to disable the old entry first.
4. If you want automations to keep working unchanged, rename the new entities to the old entity ids (possible once the old ones are gone).
5. Remove `u_tec` when satisfied.

Automatic migration is out of scope **(Rob's call** if an importer is wanted later).

---

# 18. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 18.1 | Push never works for most users | High [Field] | Out-of-band changes lag up to about 30 s | Polling never stops; push-health visibility; 24-hour repair; target-user framing in the README |
| 18.2 | Push conflict with `u_tec` or another client on the same account | Medium [Assumed] | Silent loss of push | **Setup blocked** while `u_tec` is loaded; README migration steps |
| 18.3 | U-tec introduces rate limits, possibly as HTTP 200 envelopes | Medium | Throttled or blocked accounts | Floor, batching, backoff, 429 handling, per-code envelope counts, User-Agent, usage sensors |
| 18.4 | `customData` is required for some locks or commands | Unknown [Doc A11] | Commands quietly ignored | Send it (camelCase) when discovery provides it, behind a constant that can switch it off; test in the soak |
| 18.5 | `setMode` or mode 2 behaves unexpectedly on untested models | Low–Medium [Field on Latch-5-NFC; other models open] | Keypad lockout or a lock stuck in a mode | "Locked (mode 2)" label; README documents Latch-5-NFC semantics; continue soak on other models (mode 2 shown from day one) |
| 18.6 | Accepted commands that never execute | Medium [Field A22] | Wrong state if shown optimistically | No optimism; confirmation; `not_confirmed` events |
| 18.7 | Payload drift (casing, shapes) | High [Doc][Field] | Parse failures | Lenient case-insensitive parser; fixtures for every known shape; malformed counts in diagnostics |
| 18.8 | Vendor endpoint or brand change (U-tec to Xthings), or API shutdown | Low to medium | Integration stops working | Endpoints in one constants file; nothing can prevent a shutdown; README says community and unaffiliated |
| 18.9 | Trademark use of U-tec / Ultraloq names and logos | Low | Takedown request | Nominative naming, "not affiliated" statement, neutral brand icon (decided Rob, October 6, 2026: not the U-tec logo) |
| 18.10 | Device id format changes (A10) | Low | Duplicate devices | Unique ids taken from the API id as-is; stale-device handling |
| 18.11 | Single maintainer | Medium | Slow fixes | Small codebase, high test coverage, clear docs |
| 18.12 | Users want faster polling and fork it | Medium | Extra load from forks | Out of our control; the README explains why, and the usage sensors make cost visible |

---

# 19. Decisions for Rob (all decided October 6, 2026)

All items below are **decided**. There are no remaining open Section 19 calls.

1. **Domain and repo name.** Domain `utec_locks`, repository `rbridal/ha-utec-locks` (display name "U-tec Locks").
2. **Default poll interval.** 30 s (equal to the floor).
3. **Slow-down-while-push-is-healthy.** Include the option; off by default in v1; recommended relaxed interval 120 s.
4. **Mode 2 ("Locked").** Show in the lock-mode select from day one (do not hide until tested).
5. **No optimistic state.** No optimistic locked/unlocked state; no opt-in optimistic mode in v1.
6. **Confirmation schedule and budget.** Schedule **1/1/1/1/2/3/5/8/13/21 seconds** (10 checks; absolute times 1, 2, 3, 4, 6, 9, 14, 22, 35, 56 s), batched. **No hourly confirmation budget or cap.**
7. **HTTPS-only push.** Firm floor. Home Assistant Cloud / Nabu Casa cloudhook qualifies (it is HTTPS). Plain HTTP local webhooks do not. Rationale: the push secret travels in a header.
8. **Passage plus lock command (and always-send).** Always send lock, unlock, and mode commands regardless of Passage, cache, stale, or offline; no pre-check.
9. **Cloud "offline".** Show `unknown` (not last known state).
10. **Battery.** Low binary sensor **on** + 5-step enum **on**; percent sensor **off** by default.
11. **API client.** In-repo API client for v1 (not a separate PyPI package on day one).
12. **License.** MIT.
12b. **Outside contributors / co-owners.** Outside pull requests are welcome; Rob stays sole owner for now (no co-owners).
13. **Minimum HA version.** 2026.3.0 (local brand images).
14. **Brand artwork.** Neutral brand icon, not the U-tec logo.
15. **User-Agent.** Includes the GitHub repo URL (`https://github.com/rbridal/ha-utec-locks`).
16. **Discovery cadence.** Every 6 hours.
17. **Conflict with `u_tec`.** If the old `u_tec` integration is also loaded: **BLOCK setup** (not just warn).
18. **Lock user / PIN management.** Out of scope for v1.

---

# 20. Implementation plan

| Milestone | Content | Exit criteria |
|---|---|---|
| M0 | Section 19 decided; Rob creates the repo | Names fixed |
| M1 | `api/` client, models, fixtures, client tests | 100% coverage on `api/` |
| M2 | Config flow, application credentials, coordinator (floor, backoff, discovery, staleness), read-only lock entity | Flow and coordinator tests green |
| M3 | Command executor, confirmation, events, select | Security-command and confirmation tests green |
| M4 | Push manager, push health, repairs | Push tests green |
| M5 | Usage meter and sensors, diagnostics, remaining entities, translations, icons | 95% coverage, mypy strict, hassfest, HACS validation |
| M6 | Final README, pre-release 0.1.0, hardware soak | Soak checklist complete |
| M7 | Soak fixes, 1.0.0 | Rob's sign-off |

---

# Appendix A. Load arithmetic

Requests per account per day = 86,400 / interval (background) + discovery + push registrations + commands x (1 + confirmation queries).

| Setting | Background | Discovery | Push reg. | 10 commands (worst case) | Total |
|---|---:|---:|---:|---:|---:|
| This design, 30 s default | 2,880 | 4 | 1 | 110 | about 2,995 |
| This design, 60 s | 1,440 | 4 | 1 | 110 | about 1,555 |
| This design, push healthy, relaxed to 120 s | 720 | 4 | 1 | 110 | about 835 |
| Old `u_tec` default 10 s (v0.6.1) | 8,640 | 288 | 1 | not counted | about 8,929 |
| Our fork's 20 s recommendation | 4,320 | 288 | 1 | not counted | about 4,609 |
| Old `u_tec` minimum 1 s (v0.6.1) | 86,400 | 288 | 1 | not counted | about 86,689 |

All figures are independent of lock count because Query is batched. "Worst case" assumes every command uses all 10 confirmation queries with no batching and no push (10 × (1 command + 10 confirms) = 110).

# Appendix B. Sources

- U-tec OpenAPI Postman collection (doc.api.u-tec.com), captured in `/workspace/utec-polling-report/src/utec_collection.json`; extracted text in `/workspace/utec-locks-rewrite/research/vendor_api_collection.txt`.
- Xthings support articles, fetched October 6, 2026: "Event Notification" (support.xthings.com/hc/en-us/articles/39870254872985), "Register URIs for Xthings Home-related event notifications" (articles/45031654798873), "Developer Foundational APIs" (articles/39867633454361).
- Ultraloq community thread "OpenAPI having trouble registering for Event Notifications" (community.ultraloq.com/t/11512).
- LF2b2w/Uhome-HA at `f7bd55d` (v0.6.1); our branches `feat/debug-polling-mode` (`31cc8db`) and `feat/lock-mode-select` (`8f44145`); issue comments #19, #30, #43, #61, #68, #69 as saved in `/workspace/utec-polling-report/src/comments/`.
- `utec-client` 0.5.0 and 0.5.1 wheels from PyPI (identical code), unpacked in `/workspace/utec-locks-rewrite/research/`.
- Prior report `/workspace/utec-polling-report/utec-polling-report.md` (fair use, load model, precedents, `xthings_cloud` 30-minute fallback).
- Home Assistant core, dev branch, October 6, 2026: `homeassistant/helpers/service.py` (unavailable entities skipped), `homeassistant/components/lock/__init__.py` (locking, unlocking, jammed states), `homeassistant/brands/u_tec.json`, `homeassistant/components/ultraloq/manifest.json`, `homeassistant/components/xthings_cloud/manifest.json` and `quality_scale.yaml`. Quality scale rules (developers.home-assistant.io/docs/core/integration-quality-scale/rules). Brand images for custom integrations (developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).
- HACS default integration list (raw.githubusercontent.com/hacs/default/master/integration; 3,262 entries on October 6, 2026).

# Appendix C. Measurement method

For each ref (`origin/main`, `feat/lock-mode-select`), every tracked `.py` file under `custom_components/` and `tests/` was read with `git show <ref>:<path>`. Raw lines = all lines. Non-blank = lines with any non-whitespace character. Code = lines holding a Python token other than comments, blank lines, and statement-position strings (docstrings), using Python's `tokenize` module. `utec-client` was measured with `wc -l` on the unpacked 0.5.0 and 0.5.1 wheels (identical by `diff -r`). `git fetch origin` on October 6, 2026 brought nothing new, so `origin/main` (`f7bd55d`, v0.6.1) is current upstream.


# Appendix D. Draft README.md

The full draft README follows (also saved as `README.draft.md`). Headings are demoted one level here; in-page links are shown as plain text. Domain `utec_locks` and repo `rbridal/ha-utec-locks` are decided (Section 17.1); all Section 19 decisions are locked. Latch-5-NFC mode semantics were verified October 6, 2026 (see A23–A25); keep the live `README.md` as the source of truth if this appendix drifts.

## U-tec Locks for Home Assistant

A community integration for U-tec / Ultraloq smart locks, using U-tec's OpenAPI and your own API credentials.

**Not made, endorsed, or supported by U-tec or Xthings.** Please report problems with this integration [here](https://github.com/rbridal/ha-utec-locks/issues), not to U-tec support.

### Is this integration for you?

**It is for you if Home Assistant is how you run your locks.** Your automations lock up at night and when you leave, your dashboard is where you check the doors, and you mostly leave the locks alone at the door. A shop door that Home Assistant puts into Passage mode during business hours is a perfect fit.

**It is not for you if you often lock or unlock at the door** (keypad, fingerprint, thumb-turn, key) **or often use the U-tec app.** U-tec can send Home Assistant a notification when a lock changes, but those notifications often don't arrive. When they don't, Home Assistant only finds out at its next check, every 30 seconds by default and sometimes longer when U-tec's servers are having trouble. During that gap Home Assistant can show "locked" when the door was just unlocked by hand. If that's how your household uses its locks, the U-tec app will serve you better, or a local connection (Matter or Z-Wave) if your model supports one.

Changes you make *from* Home Assistant are different: the integration checks on them right away and confirms them within seconds.

### At a glance

| | |
|---|---|
| Devices | U-tec / Ultraloq locks available through the U-tec OpenAPI. Locks only. |
| Talks to | U-tec cloud (`api.u-tec.com`), plus optional push notifications to your Home Assistant |
| Updates | Checks every 30 seconds (the minimum), plus push when it works, plus quick checks after your own commands |
| Credentials | Your own OpenAPI Client ID and Secret, from the Xthings Home app |
| Install | HACS (custom repository) |
| Requires | Home Assistant 2026.3 or newer |

### What you get

For each lock:

| Entity | What it shows |
|---|---|
| Lock | Locked, unlocked, locking, unlocking, jammed, or unknown. Lock and unlock. |
| Lock mode | Normal, Passage, or Locked (mode 2). See Lock mode. |
| Door | Open or closed, on locks with a door sensor. |
| Battery | Low or normal. |
| Battery level | Critically low, low, medium, high, or full. U-tec reports five steps, not a percentage. |
| Battery (percent) | Off by default. The five steps shown as 20 to 100% for cards that need a number. |
| Status stale | On when Home Assistant hasn't heard from the lock for too long. |
| Cloud connection | Whether U-tec says the lock is online. |
| Last report | When the lock's state last came in. Off by default. |
| Command result | An event for each lock command: confirmed, not confirmed, or rejected. |

For your U-tec account: API requests (total, last 24 hours, last hour), projected requests per day, API errors, commands, confirmation checks, pushes received, push status, push healthy, last push, poll interval in use, and a button to re-register push. Totals survive restarts.

### Before you install

You need:

1. Home Assistant 2026.3 or newer, with [HACS](https://hacs.xyz/).
2. Your locks set up in the **Xthings Home** app (formerly U-tec / U home), version 3.5.5 or newer.
3. Your Home Assistant URL set under **Settings → System → Network**.
4. For push notifications (optional): Home Assistant Cloud (Nabu Casa), or an external **HTTPS** URL for your Home Assistant with a certificate from a trusted authority. Plain HTTP is not used for push. Without either, everything works on polling alone.

### Install

#### HACS

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/rbridal/ha-utec-locks`, type **Integration**.
3. Find **U-tec Locks**, download it, and restart Home Assistant.

#### Manual

Copy `custom_components/utec_locks` from the latest release into your `config/custom_components/` folder and restart Home Assistant.

### Set up

#### 1. Get your OpenAPI credentials

1. Open the **Xthings Home** app and go to **My Account → OpenAPI**.
2. Turn on OpenAPI, choose your role, and **select the locks** you want Home Assistant to see. Locks you don't select here won't show up.
3. Set **Redirect URI** to exactly `https://my.home-assistant.io/redirect/oauth`. Don't change the hostname.
4. Make sure the scope is **OpenAPI** and save.
5. Note the **Client ID** and **Client Secret**.

#### 2. Add the integration

1. **Settings → Devices & services → Add integration → U-tec Locks.**
2. Enter your Client ID and Client Secret when asked. (Home Assistant stores them under **Settings → Application credentials**.)
3. Sign in to U-tec and approve access.
4. Your locks appear. If none do, check step 1.2.

You can add more than one U-tec account. Each account is its own entry.

### How state updates work

- **Polling.** Every 30 seconds (you can choose longer, not shorter), Home Assistant asks U-tec for the state of all your locks in one request. Ten locks cost the same as one.
- **Push.** If push is set up and working, U-tec tells Home Assistant about changes as they happen. The integration keeps an eye on whether push is actually delivering changes (see Push notifications), and it never stops polling, because push can stop without warning.
- **After your commands.** When you lock, unlock, or change mode from Home Assistant, it checks that lock up to 10 times over about 56 seconds (intervals 1/1/1/1/2/3/5/8/13/21 s) until the change shows up.
- **When it doesn't know.** If Home Assistant hasn't had a good report from a lock for 2 minutes (at the default interval), the lock shows **unknown** and **Status stale** turns on. The lock's attributes still show the last known state and when it was reported. The lock stays usable, so you can still lock and unlock it.

### Lock and unlock: what to expect

- **Commands always go out.** If you press Lock, the integration sends the lock command, even if Home Assistant thinks it's already locked, the state is stale, or U-tec says the lock is offline. It never skips a command because of what it last saw.
- **You see what the lock reports, not a guess.** Right after a command the lock shows **locking** or **unlocking**. It switches to locked or unlocked when the lock confirms it.
- **If the lock doesn't confirm.** After about 90 seconds the lock goes back to showing what U-tec last reported, a warning is logged, and the **Command result** event fires `not_confirmed`. This happens: U-tec sometimes accepts a command and the lock never acts on it.
- **If U-tec refuses.** For example, a lock U-tec says is offline. You get an error right away that says why, and the event fires `rejected`.
- **If U-tec doesn't answer.** The integration tries once more. If that fails too, you get an error saying the lock may still act, and Home Assistant keeps checking so you'll see what really happened.

Commands are never queued for later. If a command fails, it fails now, not as a surprise unlock ten minutes from now.

### Lock mode and Passage

Each lock has a **Lock mode** select with U-tec's three modes:

| Mode | What it does |
|---|---|
| Normal | Regular operation. On Latch-5-NFC, entering Normal locks the door and auto-lock behaves as usual. |
| Passage | The lock auto-unlocks and stays unlocked. On latch models this is the only way to stop auto-lock. Verified on Latch-5-NFC: Passage held the door unlocked through a 5+ minute auto-lock window. An unlock command while already in Passage produces an odd beep and no change. |
| Locked (mode 2) | The door locks. RFID / credentials are denied (red flash on Latch-5-NFC). Home Assistant shows locked. U-tec's docs name this mode but do not describe it; hardware-verified on Latch-5-NFC. |

The select shows the mode the lock last reported. Choosing a mode always sends the command, then Home Assistant checks until the lock reports the new mode.

**Latch auto-lock.** U-tec latch locks lock themselves after a while, and that can't be turned off. If you want a door to stay open during the day, use Passage mode rather than an automation that keeps unlocking it. One command instead of dozens, and the door never relocks in between. If more than 6 commands go to one lock within 10 minutes, Home Assistant shows a repair suggesting Passage mode.

On Latch-5-NFC, switching from Passage back to Normal locks the door. If you want the door locked at closing time, still add a lock action after the mode change as a belt-and-suspenders step (see the example below).

### Options

**Settings → Devices & services → U-tec Locks → Configure.**

| Option | Default | Notes |
|---|---|---|
| Polling interval | 30 s | 30 to 3,600 seconds. 30 seconds is the minimum and can't be lowered. |
| Use push notifications | On | Needs Nabu Casa or an HTTPS external URL. |
| Slow down polling while push is healthy | Off | When push is proven to be working, poll less often. Goes straight back to the normal interval the moment push misses a change. |
| Interval while push is healthy | 120 s | 60 to 1,800 seconds. Only used with the option above. |
| Confirm commands with quick checks | On | The up-to-10 checks after each of your commands. |

### Push notifications

When push is on, the integration registers a URL with U-tec: your Nabu Casa cloudhook if you have Home Assistant Cloud, otherwise your external HTTPS URL. It re-registers with a new secret once a day. Every push must carry that secret or it's rejected.

**Push status** tells you how it's going:

| Status | Meaning |
|---|---|
| Unverified | Registered, but nothing has happened yet to prove it works. |
| Healthy | Push delivered the recent changes Home Assistant saw. |
| Degraded | Push delivered some changes and missed others. |
| Unhealthy | Push missed the last changes. Polling carries on as normal. |
| Registration failed | U-tec didn't accept the registration. It's retried automatically. |
| No URL | No HTTPS URL available, so push isn't possible. |
| Disabled | Turned off in options. |

"Missed" means polling found a change that push never delivered. A quiet lock with no pushes is not counted against push.

If push stays unhealthy for a day, a repair explains what to check. Nothing speeds up when push is down: polling stays at your interval.

Two things to know:

- U-tec has no way to remove a registered URL. After you remove this integration U-tec may keep trying to send to it for a while; Home Assistant ignores those requests.
- U-tec seems to keep one push URL per account. If another integration or app registers its own URL with the same account, one of them loses push. See Moving from the u_tec integration.

### API usage and why there's a 30-second minimum

Every install of every U-tec integration shares the same U-tec servers, and U-tec publishes no limits. Other smart-home companies have added caps or cut off access after heavy third-party use. The 30-second minimum keeps one install at about 2,900 requests a day however many locks it has, and the quick checks after your commands mean you don't need faster polling to get fast feedback.

The account device shows your numbers: total requests, the last 24 hours, and a projection for your current settings. If a number looks high, the diagnostics download shows where requests went.

### Examples

**Tell me when a lock command didn't take:**

```yaml
automation:
  - alias: "Front door: lock command didn't take"
    triggers:
      - trigger: state
        entity_id: event.front_door_command_result
    conditions:
      - condition: template
        value_template: >
          {{ trigger.to_state.attributes.event_type in ['not_confirmed', 'rejected'] }}
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: >
            Front door {{ trigger.to_state.attributes.command }}:
            {{ trigger.to_state.attributes.event_type | replace('_', ' ') }}.
```

**Shop door in Passage mode during business hours, locked at close:**

```yaml
automation:
  - alias: "Shop door: business hours"
    triggers:
      - trigger: time
        at: "08:00:00"
        id: open
      - trigger: time
        at: "17:30:00"
        id: close
    actions:
      - action: select.select_option
        target:
          entity_id: select.shop_door_lock_mode
        data:
          option: "{{ 'passage' if trigger.id == 'open' else 'normal' }}"
      - if:
          - condition: trigger
            id: close
        then:
          - wait_template: "{{ is_state('select.shop_door_lock_mode', 'normal') }}"
            timeout: "00:01:00"
          - action: lock.lock
            target:
              entity_id: lock.shop_door
```

**Warn me if a lock's status has been stale for 10 minutes:**

```yaml
automation:
  - alias: "Lock status stale"
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.front_door_status_stale
          - binary_sensor.back_door_status_stale
        to: "on"
        for: "00:10:00"
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "{{ trigger.to_state.name }}: Home Assistant hasn't heard from this lock for 10 minutes."
```

**Low battery:**

```yaml
automation:
  - alias: "Lock battery low"
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door_battery
        to: "on"
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "Front door lock battery is low."
```

### Troubleshooting

| Symptom | What to check |
|---|---|
| No locks found during setup | In the Xthings app, OpenAPI page, make sure the locks are selected. |
| Lock shows unknown and Status stale is on | U-tec isn't answering, or isn't reporting this lock. Look at **API errors** on the account device. U-tec returns occasional server errors; the integration backs off and keeps trying. |
| Lock shows unknown and Cloud connection is off | U-tec says the lock is offline. Check its Wi-Fi or bridge. Commands are still sent, but U-tec will likely refuse them. |
| State lags behind changes made at the door | Expected when push isn't working. Check **Push status**. See Is this integration for you? |
| Push status stays Unverified | Lock or unlock once from Home Assistant; that creates evidence. |
| Push status No URL | Set up Nabu Casa, or an HTTPS external URL under Settings → System → Network. |
| "Re-authentication required" | Follow the repair or the integration card's prompt to sign in again. |
| "U-tec is rate limiting requests" | Wait the time shown. If it keeps happening, check the request numbers on the account device and open an issue. |

To turn on debug logs: **Settings → Devices & services → U-tec Locks → Enable debug logging**, reproduce the problem, then disable it to download the log. Logs never contain tokens, secrets, or push bodies.

### Known limitations

- State changes made at the lock or in the U-tec app can take up to the polling interval (30 seconds by default) to show up when push isn't working, and longer during U-tec outages.
- U-tec reports battery in five steps, not a percentage.
- U-tec's API has no auto-lock setting. Use Passage mode to keep a latch open.
- U-tec does not document what "Locked (mode 2)" does; on Latch-5-NFC it locks the door and denies RFID. Other models may differ.
- Lock users and PIN codes can't be managed from this integration.
- No local control. Everything goes through U-tec's cloud.
- U-tec's API exposes no Wi-Fi bridge or other non-lock devices to this integration, and lights, switches and plugs are deliberately not supported.

### Supported devices

Any U-tec / Ultraloq lock that appears in the U-tec OpenAPI with category `SmartLock` (handle types `utec-lock` and `utec-lock-sensor`). Tested so far: **Latch-5-NFC** (shop door; lock/unlock, Normal, Passage, and Locked mode verified on v0.1.1).

### Diagnostics and privacy

**Settings → Devices & services → U-tec Locks → three dots → Download diagnostics.** Tokens, secrets, your client ID, push URLs, your name and user id, and serial numbers are removed. Lock ids are replaced with short codes.

The integration talks only to U-tec. It sends no data anywhere else.

### Moving from the u_tec integration

This is a separate integration with its own domain, not an update to `u_tec` (Uhome-HA). **You cannot run both at once:** if `u_tec` is still loaded, U-tec Locks will refuse to set up (they would double your API use and take push from each other).

1. Note which automations, scripts and dashboards use your `u_tec` lock entities.
2. Disable (or delete) the `u_tec` integration entry.
3. Set up U-tec Locks. You can reuse your Client ID and Secret.
4. Rename the new entities to the old entity ids if you want your automations to keep working unchanged.
5. Remove `u_tec` once you're happy.

### Removing the integration

1. **Settings → Devices & services → U-tec Locks → three dots → Delete.**
2. Remove it from HACS (or delete `custom_components/utec_locks`) and restart Home Assistant.
3. Optional: in the Xthings app, turn off OpenAPI or reset the Client Secret.
4. Optional: delete the credential under **Settings → Application credentials**.

### FAQ

**Can I poll faster than 30 seconds?** No. It's a fixed floor, for the reasons in API usage. Commands you send from Home Assistant are still confirmed within seconds.

**Why doesn't the lock show "locked" right after I press Lock?** Because it isn't locked yet. U-tec only says it received the command. The lock shows "locking" until U-tec reports that the lock actually locked.

**Why unknown instead of unavailable?** Home Assistant silently ignores commands sent to unavailable entities. A lock you can't command is worse than a lock whose state is uncertain, so it stays available and says unknown.

**Does this work without Nabu Casa?** Yes. Without Nabu Casa or an HTTPS external URL there's no push, and everything runs on polling.

### Contributing

Issues and pull requests from outside contributors are welcome. Rob remains the sole code owner for now (no co-owners). Changes that add API requests need a note on how many and why. Tests must pass with at least 95% coverage.

### License

MIT. U-tec, Ultraloq and Xthings are trademarks of their owners and are used here only to describe what this integration works with.
