"""Dataclasses and enums for vendor payloads — stub.

Production: lenient case-insensitive parsing of lockState, lockMode, battery,
door sensor, healthCheck; LockInfo from discovery; LockReport observations.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class LockStateValue(StrEnum):
    """st.lock.lockState values."""

    LOCKED = "locked"
    UNLOCKED = "unlocked"
    JAMMED = "jammed"
    UNKNOWN = "unknown"


class LockMode(IntEnum):
    """st.lock.lockMode values."""

    NORMAL = 0
    PASSAGE = 1
    LOCKED = 2


class DoorState(StrEnum):
    """st.doorSensor.sensorState values."""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Source(StrEnum):
    """Where a LockReport came from."""

    POLL = "poll"
    PUSH = "push"
    CONFIRM = "confirm"
