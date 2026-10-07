"""API response times over a rolling window of recent requests (in memory).

Fed with one :class:`~.api.client.RequestRecord` per HTTP request: every call
to ``/action`` (through the usage meter, the client's single observer) and
every OAuth token refresh (through the token provider).

* **Last** is the duration of the most recent request, whatever its outcome.
  A request that timed out or failed to connect keeps its elapsed time; the
  ``outcome`` attribute says what happened.
* **Average** is the mean over the last :data:`~.const.LATENCY_WINDOW`
  requests that got an HTTP response (any status, error envelopes included).
  Requests with no response at all (timeout, connection error) stay in the
  window but are excluded from the average and maximum and counted as
  ``failed_requests``; a 15 s timeout would otherwise swamp the number.

Nothing is persisted: timings describe the cloud right now, so after a restart
the sensors start again from the first request of setup.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .api.client import NO_RESPONSE_OUTCOMES, RequestRecord
from .const import LATENCY_WINDOW


@dataclass(frozen=True)
class LatencySample:
    """One timed request (no bodies, tokens, ids or names)."""

    kind: str
    operation: str
    outcome: str
    http_status: int | None
    duration_ms: float
    measured_at: datetime

    @property
    def responded(self) -> bool:
        """True when an HTTP response arrived (any status)."""
        return self.outcome not in NO_RESPONSE_OUTCOMES

    @classmethod
    def from_record(cls, record: RequestRecord) -> LatencySample:
        """Build from a client request record."""
        return cls(
            kind=record.kind,
            operation=f"{record.namespace}/{record.name}",
            outcome=record.outcome,
            http_status=record.http_status,
            duration_ms=max(0.0, record.latency * 1000),
            measured_at=record.started + timedelta(seconds=max(0.0, record.latency)),
        )

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics-friendly dict."""
        return {
            "request_type": self.kind,
            "operation": self.operation,
            "outcome": self.outcome,
            "http_status": self.http_status,
            "duration_ms": round(self.duration_ms),
            "measured_at": self.measured_at.isoformat(),
        }


class LatencyTracker:
    """Last and rolling-average response time of the U-tec cloud."""

    def __init__(self, window: int = LATENCY_WINDOW) -> None:
        """Initialize an empty window."""
        self._samples: deque[LatencySample] = deque(maxlen=window)

    @property
    def window(self) -> int:
        """Window size in requests."""
        return self._samples.maxlen or 0

    def add(self, record: RequestRecord) -> LatencySample:
        """Record one request."""
        sample = LatencySample.from_record(record)
        self._samples.append(sample)
        return sample

    @property
    def last(self) -> LatencySample | None:
        """Most recent request, whatever its outcome."""
        return self._samples[-1] if self._samples else None

    def _responded(self) -> list[float]:
        return [s.duration_ms for s in self._samples if s.responded]

    @property
    def last_ms(self) -> int | None:
        """Duration of the most recent request in ms."""
        last = self.last
        return round(last.duration_ms) if last else None

    @property
    def average_ms(self) -> int | None:
        """Mean of the responded requests in the window (ms), or None."""
        values = self._responded()
        return round(sum(values) / len(values)) if values else None

    @property
    def max_ms(self) -> int | None:
        """Slowest responded request in the window (ms), or None."""
        values = self._responded()
        return round(max(values)) if values else None

    @property
    def min_ms(self) -> int | None:
        """Fastest responded request in the window (ms), or None."""
        values = self._responded()
        return round(min(values)) if values else None

    @property
    def sample_count(self) -> int:
        """Responded requests in the window (the ones averaged)."""
        return sum(1 for s in self._samples if s.responded)

    @property
    def failed_count(self) -> int:
        """Requests in the window with no response (timeout / connection)."""
        return sum(1 for s in self._samples if not s.responded)

    def last_attributes(self) -> dict[str, Any]:
        """Attributes for the last-response-time sensor."""
        last = self.last
        if last is None:
            return {}
        return {
            "request_type": last.kind,
            "operation": last.operation,
            "outcome": last.outcome,
            "http_status": last.http_status,
            "measured_at": last.measured_at.isoformat(),
        }

    def average_attributes(self) -> dict[str, Any]:
        """Attributes for the average-response-time sensor."""
        last = self.last
        return {
            "window_requests": self.window,
            "sample_count": self.sample_count,
            "failed_requests": self.failed_count,
            "max_ms": self.max_ms,
            "min_ms": self.min_ms,
            "last_measured_at": last.measured_at.isoformat() if last else None,
        }

    def snapshot(self) -> dict[str, Any]:
        """Diagnostics view."""
        return {
            "last_ms": self.last_ms,
            "average_ms": self.average_ms,
            **self.average_attributes(),
            "samples": [s.as_dict() for s in self._samples],
        }
