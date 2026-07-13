"""CPU package power draw from the Linux RAPL powercap interface.

``/sys/class/powercap/intel-rapl*`` exposes monotonically increasing
energy counters (µJ) per domain — package, core, uncore, dram, psys.
The interface is served by the same driver on modern AMD CPUs (Zen 2+)
despite the ``intel-`` prefix. Reading ``energy_uj`` requires root on
hardened kernels; the collector disables itself when unreadable.

The first pass only primes the counters; every following pass ships
watts as ``W`` sensors (mirrored to ``sensor.power[...]`` metrics).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from oo_agent.core.rates import RateTracker
from oo_agent.plugin import Collector

log = logging.getLogger("collector.rapl")

_RAPL_ROOT = "/sys/class/powercap"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def discover_domains(root: str = _RAPL_ROOT) -> list[tuple[str, str, str]]:
    """(sensor id, label, energy_uj path) per readable RAPL domain."""
    domains: list[tuple[str, str, str]] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for entry in entries:
        if not entry.startswith("intel-rapl"):
            continue
        energy = os.path.join(root, entry, "energy_uj")
        name = _read(os.path.join(root, entry, "name"))
        if name is None or _read(energy) is None:
            continue
        # entry is like intel-rapl:0 (socket) or intel-rapl:0:1 (its
        # subdomain); prefix subdomain labels with the socket index so
        # "core"/"dram" stay unique on multi-socket hosts.
        parts = entry.split(":")[1:]
        label = name if len(parts) < 2 else f"{name}-{parts[0]}"
        domains.append((f"rapl:{entry}", label, energy))
    return domains


class RaplCollector(Collector):
    name = "rapl"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._rates = RateTracker()
        self._domains: list[tuple[str, str, str]] = []

    def available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        self._domains = discover_domains()
        if not self._domains:
            log.info("RAPL not readable (needs root on most kernels)")
            return False
        return True

    def collect(self) -> dict[str, Any]:
        counters: dict[str, float] = {}
        for sensor_id, _label, path in self._domains:
            raw = _read(path)
            if raw is not None and raw.isdigit():
                counters[sensor_id] = float(raw)
        watts = {
            key: value / 1e6  # µJ/s -> W
            for key, value in self._rates.rates(counters).items()
        }
        sensors = [
            {
                "id": sensor_id,
                "name": f"CPU power {label}",
                "kind": "cpu",
                "value": round(watts[sensor_id], 1),
                "unit": "W",
            }
            for sensor_id, label, _path in self._domains
            if sensor_id in watts and watts[sensor_id] < 5000
        ]
        return {"sensors": sensors}
