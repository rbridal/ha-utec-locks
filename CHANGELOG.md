# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-10-06

Pre-release for hardware testing.

### Added

- Account diagnostic sensors **Last API response time** and **Average API
  response time** (duration, ms). Every HTTP request to U-tec is timed: Query,
  confirmation checks, commands, discovery, push registration and OAuth token
  refreshes. The average is the mean of the last 20 requests that got an HTTP
  response (error replies included). Timeouts and connection failures stay
  visible in Last (attribute `outcome`) but are excluded from the average and
  counted in `failed_requests`. Not persisted. Diagnostics include the 20-request
  timing window.
- Per-lock diagnostic sensors **Offline reports** (total_increasing) and **Last
  offline report** (timestamp), persisted across restarts. Every report that
  says the lock is offline counts, including single blips that are no longer
  shown. INFO log lines for each offline report and for coming back online
  (lock name only).
- Cloud connection attribute `offline_reports`: consecutive offline reports.

### Changed

- **Offline debounce:** a lock is shown offline (lock and mode `unknown`,
  cloud connection off) only after two consecutive offline reports. After the
  first, the last known state, mode and connectivity are kept. Any online report
  (poll, confirmation or push) resets the count. API fetch failures neither count
  nor reset. Prompted by a single-poll blip on the shop Latch on 2026-10-06 that
  cascaded into alarm-template unknowns.
- README and DESIGN: document Latch-5-NFC hardware verification for Normal,
  Passage, and Locked (mode 2); remove speculative "(to verify)" language.

## [0.1.1] - 2026-10-06

Pre-release for hardware testing.

### Changed

- The confirmation schedule (checks at 1/1/1/1/2/3/5/8/13/21 s) now always runs
  in full after lock, unlock and setMode, whatever U-tec's "result in N s"
  (`st.deferredResponse`) reply says (Rob's decision). In 0.1.0 a 20 s hint
  delayed the first check to 22 s. Honoring the hint is still possible through the
  `HONOR_DEFERRED_HINT` switch, which now defaults to off. The unconfirmed
  timeout of about 90 s is unchanged.

## [0.1.0] - 2026-10-06

First pre-release, for hardware testing. Requires Home Assistant 2026.3.0 or newer.

### Added

- U-tec OpenAPI client in this repository: case-insensitive parsing of all known
  payload shapes, HTTP-200 error envelopes, per-device errors, 429/5xx handling
  with backoff, one token refresh-and-retry on `INVALID_TOKEN`, and the
  `HomeAssistant-utec_locks/<version>` User-Agent.
- One coordinator per account with one batched Query per cycle. 30 s is both the
  firm minimum and the default interval. Optional 120 s interval while push is
  healthy (off by default). Discovery every 6 hours.
- Lock, unlock and setMode are always sent, with no pre-check. A lock command in
  Passage mode logs a warning. No optimistic state: the lock shows
  locking/unlocking until a report confirms it.
- Batched confirmation checks at 1/1/1/1/2/3/5/8/13/21 s (at most 10, no hourly
  cap). `not_confirmed` at about 90 s fires the command-result event.
- Stale or cloud-offline locks show `unknown` but stay available (`stale` and
  `last_known_state` attributes). A lock is unavailable only after it is missing
  from two discoveries; a fixable repair offers to delete it.
- Entities per lock: lock, mode select (Normal/Passage/Locked), door (when
  present), battery low, battery level (5 steps), battery percent (disabled),
  status stale, cloud connection, last report (disabled) and a command-result
  event. Account entities: API request totals (persisted) and 24 h/1 h windows,
  projected requests per day, errors, commands, confirmation queries, pushes,
  push status, push healthy, last push, poll interval and a re-register button.
- HTTPS-only push (Nabu Casa cloudhook or a public HTTPS URL). The Bearer secret
  is rotated on every registration, with a 10-minute grace for the previous one.
  Evidence-based push health, with a repair after 24 h unhealthy. Polling never
  stops.
- OAuth through application credentials (wrapped token responses handled),
  reauth and reconfigure checked against the same account, options flow,
  redacted diagnostics, and repairs.
- Setup and the config flow are blocked while the legacy `u_tec` integration is
  loaded.
- Local brand icon (neutral lock).
- 204 tests against a mocked U-tec cloud (about 95% coverage), passing on HA
  2026.9.4 and 2026.3.1.

## [0.1.0-dev] - 2026-10-06

### Added

- Initial repository scaffold for the `utec_locks` Home Assistant custom integration (0.1.0-dev).
- Packaging stubs: `manifest.json`, `hacs.json`, MIT license, draft README, design document under `docs/`.

[Unreleased]: https://github.com/rbridal/ha-utec-locks/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/rbridal/ha-utec-locks/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/rbridal/ha-utec-locks/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/rbridal/ha-utec-locks/releases/tag/v0.1.0
