# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/rbridal/ha-utec-locks/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rbridal/ha-utec-locks/releases/tag/v0.1.0
