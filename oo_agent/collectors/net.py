"""Aggregate network throughput (KB/s) over physical-like interfaces.

Loopback and virtual interfaces (docker bridges, veth pairs) are
excluded so container-to-container chatter does not inflate host
traffic numbers.
"""

from __future__ import annotations

import time
from typing import Any

import psutil

from oo_agent.core.rates import RateTracker
from oo_agent.plugin import Collector

_SKIP_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "vnet", "tap", "kube")


def _counters() -> dict[str, float]:
    rx = tx = 0
    for nic, io in psutil.net_io_counters(pernic=True).items():
        if nic.startswith(_SKIP_PREFIXES):
            continue
        rx += io.bytes_recv
        tx += io.bytes_sent
    return {"rx": float(rx), "tx": float(tx)}


class NetCollector(Collector):
    name = "net"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._tracker = RateTracker()
        self._tracker.prime(_counters())

    def collect(self) -> dict[str, Any]:
        if self._tracker.age < 1.0:
            time.sleep(0.5)  # --once mode: sample over a short window
        rates = self._tracker.rates(_counters())
        if not rates:
            return {}
        return {
            "metrics": {
                "net.rx.kbps": round(rates.get("rx", 0.0) / 1024, 1),
                "net.tx.kbps": round(rates.get("tx", 0.0) / 1024, 1),
            }
        }
