"""Aggregate disk read/write throughput (kbps)."""

from __future__ import annotations

import time
from typing import Any

import psutil

from oo_agent.core.rates import RateTracker
from oo_agent.plugin import Collector


def _counters() -> dict[str, float]:
    io = psutil.disk_io_counters()
    if io is None:
        return {}
    return {"read": float(io.read_bytes), "write": float(io.write_bytes)}


class DiskIoCollector(Collector):
    name = "diskio"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._tracker = RateTracker()
        self._tracker.prime(_counters())

    def available(self) -> bool:
        return psutil.disk_io_counters() is not None

    def collect(self) -> dict[str, Any]:
        if self._tracker.age < 1.0:
            # --once mode: sample over a short window instead of a
            # near-zero delta right after priming.
            time.sleep(0.5)
        rates = self._tracker.rates(_counters())
        if not rates:
            return {}
        # "kbps" throughout the contract means kibibytes per second
        # (the UI renders it as KB/s), not kilobits.
        return {
            "metrics": {
                "diskio.read.kbps": round(rates.get("read", 0.0) / 1024, 1),
                "diskio.write.kbps": round(rates.get("write", 0.0) / 1024, 1),
            }
        }
