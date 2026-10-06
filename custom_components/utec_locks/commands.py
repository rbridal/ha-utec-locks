"""Command executor, confirmation burst, loop detection — stub.

Uses CONFIRMATION_SCHEDULE_SECONDS from const (1/1/1/1/2/3/5/8/13/21).
Absolute ticks 1, 2, 3, 4, 6, 9, 14, 22, 35, 56 s; at most 10 queries per
command, batched across pending locks. No hourly confirmation budget or cap
(decided): every accepted command gets the full schedule. not_confirmed at ~90 s.
Commands are never blocked or skipped.
"""

from __future__ import annotations

from .const import CONFIRMATION_SCHEDULE_SECONDS, CONFIRMATION_TIMEOUT_SECONDS


class CommandExecutor:
    """Sends lock/unlock/setMode; runs bounded confirmation (scaffold stub)."""

    def __init__(self) -> None:
        """Store schedule for later wiring."""
        self.schedule = CONFIRMATION_SCHEDULE_SECONDS
        self.timeout_seconds = CONFIRMATION_TIMEOUT_SECONDS
