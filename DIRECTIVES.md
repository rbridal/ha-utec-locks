# ha-utec-locks — Directives

HA-first, locks-only Home Assistant integration for U-tec / Ultraloq via U-tec OpenAPI. Built for households that run locks from HA, not from the door/app all day.

## Ownership
- Home / project directives: **Masta-Rob**. Assista-Rob owns Versa / work directives.
- Flag Rob only for real policy changes.
- Before working on this project, read this file. Conflict = STOP and ask Rob. Safety always wins.

## Safety (always wins)
- Never ship credentials, Client ID/Secret, or tokens in git, issues, or chat.
- Do not publish releases or merge to main without Rob's OK while this is in shop hardware test.

## Directives (priority order, highest first)
- Locks only (no non-lock U-tec devices in scope for v0.x).
- Prefer HA-confirmed local intent: commands from HA get quick confirm; cloud push is best-effort.
- Default confirmation / polling schedule follows Rob's chosen defaults (see DESIGN.md / README).
- HACS-compatible packaging so a later public HACS submit is not a rewrite.
- Shop hardware tests and Passage/Normal alarm coupling happen only with Rob present or with his explicit OK.

## Non-goals
- Not a U-tec app replacement for keypad/fingerprint-heavy households.
- Not Matter/Z-Wave local pairing (cloud OpenAPI path).

## Standing rules that apply here
- No secrets in git (RULES.md #2).
- Nothing flashed, merged, synced, deployed, or retired without Rob's OK (standing estate rule).
- Install/update ownership tracked in MyHACS catalog (approve policy).

## Change log
- 2026-11-04 — Masta-Rob — Initial DIRECTIVES.md from README/DESIGN + standing rules (Rob's OK: yes, pt1410u).
