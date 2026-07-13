"""Core system metrics: CPU, load, memory, swap, uptime, CPU throttling.

All values come from psutil and are cheap to read. CPU utilization
uses the non-blocking form of ``cpu_percent`` (delta since previous
call), primed once at startup.

``cpu.throttle`` reports the hardware's own thermal verdict: 1 when
any core's Linux thermal_throttle counter grew since the previous
pass (Intel exposes these; on other platforms the metric is absent).
"""

from __future__ import annotations

import glob
import os
import time
from typing import Any

import psutil

from oo_agent.plugin import Collector

_THROTTLE_GLOB = "/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"


def _throttle_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in glob.glob(_THROTTLE_GLOB):
        try:
            with open(path, encoding="ascii") as fh:
                counts[path] = int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return counts


class SystemCollector(Collector):
    name = "system"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        # Prime the interval-less cpu_percent counters; the first real
        # collect() then returns utilization since this point.
        psutil.cpu_percent(interval=None, percpu=True)
        self._primed_at = time.monotonic()
        self._throttle_prev = _throttle_counts()

    def collect(self) -> dict[str, Any]:
        if time.monotonic() - self._primed_at < 1.0:
            # --once mode: no time has passed since priming, take a
            # short blocking sample instead of a meaningless 0.0.
            per_core = psutil.cpu_percent(interval=0.5, percpu=True)
        else:
            per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_total = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        metrics: dict[str, Any] = {
            "cpu.util": cpu_total,
            "cpu.util.core": [round(v, 1) for v in per_core],
            "cpu.num": len(per_core),
            "mem.pused": round(mem.percent, 1),
            "mem.used": mem.total - mem.available,
            "mem.total": mem.total,
            "swap.pused": round(swap.percent, 1),
            "swap.used": swap.used,
            "swap.total": swap.total,
            "system.uptime": int(time.time() - psutil.boot_time()),
        }
        if hasattr(os, "getloadavg"):
            load1, load5, load15 = os.getloadavg()
            metrics["system.load1"] = round(load1, 2)
            metrics["system.load5"] = round(load5, 2)
            metrics["system.load15"] = round(load15, 2)
        current = _throttle_counts()
        if current:
            grew = any(
                value > self._throttle_prev.get(path, value)
                for path, value in current.items()
            )
            metrics["cpu.throttle"] = int(grew)
            self._throttle_prev = current
        return {"metrics": metrics}
