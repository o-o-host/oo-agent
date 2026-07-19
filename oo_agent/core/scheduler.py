"""Collection scheduling and payload assembly.

Single-threaded loop: collectors are cheap (reads of /proc, /sys and
library calls), so they run sequentially at their own intervals. A
collector that raises backs off with an exponentially growing cooldown
and is retried — one transient hiccup (a Docker API timeout, a busy
SMART device) must not silence a source until restart. The agent
itself never dies because of one broken collector.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from oo_agent.core import sdnotify
from oo_agent.core.hostinfo import agent_info
from oo_agent.core.registry import Entry

log = logging.getLogger("scheduler")

# Failure cooldown: 5 min after the first failure, doubling up to 1 h.
_BACKOFF_BASE = 300.0
_BACKOFF_MAX = 3600.0

# Metric key prefix per sensor unit for the flat numeric channel.
_SENSOR_METRIC = {
    "C": "sensor.temp",
    "rpm": "sensor.fan",
    "V": "sensor.volt",
    "W": "sensor.power",
    "A": "sensor.curr",
}


def _run_entry(entry: Entry) -> dict[str, Any] | None:
    """One isolated collect() call; backs the entry off on failure."""
    try:
        started = time.monotonic()
        payload = entry.collector.collect() or {}
        elapsed = time.monotonic() - started
        if elapsed > 5:
            log.warning("collector %s: slow collect (%.1fs)", entry.name, elapsed)
        entry.failures = 0
        entry.retry_at = 0.0
        return payload
    except Exception as exc:  # noqa: BLE001 - isolation by design
        entry.failures += 1
        cooldown = min(_BACKOFF_BASE * 2 ** (entry.failures - 1), _BACKOFF_MAX)
        entry.retry_at = time.monotonic() + cooldown
        log.warning(
            "collector %s: failed (attempt %d), retrying in %d min: %s",
            entry.name, entry.failures, int(cooldown // 60), exc,
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
        now = time.monotonic()
        return sorted(e.name for e in self._active() if now >= e.retry_at)

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
        sdnotify.ready()
        while should_stop is None or not should_stop():
            sdnotify.watchdog_ping()
            now = time.monotonic()
            due = [
                e
                for e in self._active()
                if now >= next_run.get(e.name, 0.0) and now >= e.retry_at
            ]
            if due:
                for entry in due:
                    next_run[entry.name] = now + entry.interval
                payload = self.run_once(include=lambda e: e in due)
                if payload["metrics"] or payload.get("inventory"):
                    sink(payload)
            time.sleep(1)
