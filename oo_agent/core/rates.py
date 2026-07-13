"""Counter-to-rate conversion for I/O and network collectors.

The agent ships ready-to-plot rates (kbps), so the server side needs
no preprocessing. Counter wrap/reset produces one zero sample instead
of a negative spike.
"""

from __future__ import annotations

import time


class RateTracker:
    """Tracks monotonically increasing counters and yields per-second rates."""

    def __init__(self) -> None:
        self._at: float | None = None
        self._values: dict[str, float] = {}

    def prime(self, values: dict[str, float]) -> None:
        self._at = time.monotonic()
        self._values = dict(values)

    @property
    def age(self) -> float:
        """Seconds since the previous sample; +inf when never primed."""
        return time.monotonic() - self._at if self._at is not None else float("inf")

    def rates(self, values: dict[str, float]) -> dict[str, float]:
        """Per-second deltas against the previous sample, then re-prime."""
        now = time.monotonic()
        if self._at is None or now <= self._at:
            self.prime(values)
            return {}
        dt = now - self._at
        result = {
            key: max(0.0, (value - self._values.get(key, value)) / dt)
            for key, value in values.items()
        }
        self.prime(values)
        return result
