"""Collection scheduling and payload assembly.

Single-threaded loop: collectors are cheap (reads of /proc, /sys and
library calls), so they run sequentially at their own intervals. A
collector that raises is disabled until restart (WARN once) — the
agent itself never dies because of one broken source.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from oo_agent.core.hostinfo import agent_info
from oo_agent.core.registry import Entry

log = logging.getLogger("scheduler")

# Metric key prefix per sensor unit for the flat numeric channel.
_SENSOR_METRIC = {
    "C": "sensor.temp",
    "rpm": "sensor.fan",
    "V": "sensor.volt",
    "W": "sensor.power",
    "A": "sensor.curr",
}


def _run_entry(entry: Entry) -> dict[str, Any] | None:
    """One isolated collect() call; disables the entry on failure."""
    try:
        started = time.monotonic()
        payload = entry.collector.collect() or {}
        elapsed = time.monotonic() - started
        if elapsed > 5:
            log.warning("collector %s: slow collect (%.1fs)", entry.name, elapsed)
        return payload
    except Exception as exc:  # noqa: BLE001 - isolation by design
        entry.disabled = True
        log.warning(
            "collector %s: failed, disabled until restart: %s", entry.name, exc
        )
        log.debug("collector %s traceback", entry.name, exc_info=True)
        return None


def _merge(result: dict[str, Any], entry: Entry, payload: dict[str, Any]) -> None:
    prefix = f"custom.{entry.name}." if entry.custom else ""
    for key, value in (payload.get("metrics") or {}).items():
        result["metrics"][prefix + key] = value
    for sensor in payload.get("sensors") or []:
        result["sensors"].append(sensor)
        metric = _SENSOR_METRIC.get(sensor.get("unit", ""))
        if metric and isinstance(sensor.get("value"), (int, float)):
            result["metrics"][f"{metric}[{sensor['id']}]"] = sensor["value"]
    for key, value in (payload.get("inventory") or {}).items():
        existing = result["inventory"].get(key)
        if isinstance(existing, list) and isinstance(value, list):
            # Two collectors may feed one inventory list (e.g. NVIDIA
            # and AMD GPU collectors both append to "gpus").
            existing.extend(value)
        else:
            result["inventory"][key] = value


class Scheduler:
    def __init__(self, entries: list[Entry]) -> None:
        self.entries = entries

    def _active(self) -> list[Entry]:
        return [e for e in self.entries if not e.disabled]

    def capabilities(self) -> list[str]:
        return sorted(e.name for e in self._active())

    def run_once(self, include: Callable[[Entry], bool] = lambda e: True) -> dict:
        """Run matching collectors now and assemble one push payload."""
        result: dict[str, Any] = {
            "agent": agent_info(),
            "ts": int(time.time()),
            "metrics": {},
            "sensors": [],
            "inventory": {},
            "capabilities": self.capabilities(),
        }
        for entry in self._active():
            if not include(entry):
                continue
            payload = _run_entry(entry)
            if payload is not None:
                _merge(result, entry, payload)
        if not result["inventory"]:
            del result["inventory"]
        return result

    def run_forever(
        self,
        sink: Callable[[dict], None],
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Daemon loop: tick every second, run due collectors, push batches.

        ``sink`` receives one assembled payload per base tick during
        which at least one collector ran. ``should_stop`` is checked
        once per tick so a service wrapper can request a clean exit.
        """
        next_run: dict[str, float] = {e.name: 0.0 for e in self.entries}
        while should_stop is None or not should_stop():
            now = time.monotonic()
            due = [
                e
                for e in self._active()
                if now >= next_run.get(e.name, 0.0)
            ]
            if due:
                for entry in due:
                    next_run[entry.name] = now + entry.interval
                payload = self.run_once(include=lambda e: e in due)
                if payload["metrics"] or payload.get("inventory"):
                    sink(payload)
            time.sleep(1)
