"""Docker container state tracking.

Complements the ``docker`` collector: that one gathers heavyweight
per-container CPU/memory stats at inventory cadence, while this one is
cheap (inspect data only) and runs at base cadence, so state changes —
crashes, restart loops, failing health checks, OOM kills — surface
within one metrics interval.

Per container: state, health-check status, exit code, restart count and
policy, OOM flag, start/finish times and uptime. Aggregate counts go to
the numeric channel for alerting (``docker.containers.*``).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from oo_agent.collectors.containers import docker_client
from oo_agent.plugin import Collector

log = logging.getLogger("collector.docker_state")

_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})$"
)


def parse_docker_time(value: str) -> float | None:
    """Epoch seconds from Docker's RFC3339 timestamps (nanosecond
    fractions, ``Z`` suffix, ``0001-01-01`` as the zero value)."""
    if not value or value.startswith("0001-01-01"):
        return None
    match = _TIME_RE.match(value)
    if not match:
        return None
    base, frac, tz = match.groups()
    if tz == "Z":
        tz = "+00:00"
    elif ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    iso = f"{base}.{(frac or '0')[:6].ljust(6, '0')}{tz}"
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def container_entry(attrs: dict[str, Any], now: float) -> dict[str, Any]:
    """Normalized state entry from a full container inspect dict."""
    state = attrs.get("State") or {}
    status = state.get("Status") or "unknown"
    running = status == "running"
    started = parse_docker_time(state.get("StartedAt") or "")
    policy = (attrs.get("HostConfig") or {}).get("RestartPolicy") or {}
    return {
        "id": (attrs.get("Id") or "")[:12],
        "name": (attrs.get("Name") or "").lstrip("/"),
        "image": (attrs.get("Config") or {}).get("Image") or "",
        "state": status,
        "health": (state.get("Health") or {}).get("Status") or None,
        "exit_code": None if running else state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "error": state.get("Error") or None,
        "restart_count": attrs.get("RestartCount", 0),
        "restart_policy": policy.get("Name") or "no",
        "started_at": started,
        "finished_at": None
        if running
        else parse_docker_time(state.get("FinishedAt") or ""),
        "uptime_s": int(now - started) if running and started else None,
    }


class DockerStateCollector(Collector):
    name = "docker_state"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client = None

    def available(self) -> bool:
        self._client = docker_client()
        return self._client is not None

    def collect(self) -> dict[str, Any]:
        assert self._client is not None
        now = time.time()
        entries: list[dict[str, Any]] = []
        for container in self._client.containers.list(all=True):
            try:
                entries.append(container_entry(container.attrs, now))
            except Exception as exc:  # noqa: BLE001 - skip broken container
                log.debug("container skipped: %s", exc)
        entries.sort(key=lambda e: e["name"])

        def count(predicate) -> int:
            return sum(1 for e in entries if predicate(e))

        return {
            "metrics": {
                "docker.containers.total": len(entries),
                "docker.containers.running": count(
                    lambda e: e["state"] == "running"
                ),
                "docker.containers.exited": count(
                    lambda e: e["state"] == "exited"
                ),
                "docker.containers.restarting": count(
                    lambda e: e["state"] == "restarting"
                ),
                "docker.containers.paused": count(
                    lambda e: e["state"] == "paused"
                ),
                # A stopped container keeps its last health state in the
                # inspect data; only running ones can alert as unhealthy.
                "docker.containers.unhealthy": count(
                    lambda e: e["state"] == "running"
                    and e["health"] == "unhealthy"
                ),
                "docker.containers.oom_killed": count(
                    lambda e: e["oom_killed"]
                ),
            },
            "inventory": {"docker_states": entries},
        }
