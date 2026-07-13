"""Docker container inventory and per-container CPU/memory usage.

Optional: requires the ``docker`` package (``pip install oo-agent[docker]``)
and access to the Docker socket. One-shot stats calls are slow (~1s
each inside the daemon), so they run in a small thread pool and the
collector defaults to the inventory cadence.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.docker")

_STATS_WORKERS = 8


def docker_client(timeout: int = 10):
    """Docker client factory shared by the container collectors;
    returns ``None`` when the library or the daemon is unavailable."""
    try:
        import docker
    except ImportError:
        log.info("docker package not installed — container collectors off")
        return None
    try:
        client = docker.from_env(timeout=timeout)
        client.ping()
    except Exception as exc:  # noqa: BLE001 - no socket / no permission
        log.info("docker not reachable: %s", exc)
        return None
    return client


def _cpu_pct(stats: dict[str, Any]) -> float | None:
    cpu = stats.get("cpu_stats") or {}
    pre = stats.get("precpu_stats") or {}
    try:
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
    except KeyError:
        return None
    if sys_delta <= 0 or cpu_delta < 0:
        return None
    cores = cpu.get("online_cpus") or len(
        (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
    )
    return round(cpu_delta / sys_delta * (cores or 1) * 100, 1)


def _mem_mb(stats: dict[str, Any]) -> float | None:
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage")
    if usage is None:
        return None
    details = mem.get("stats") or {}
    # Page cache is reclaimable and misleading on charts: cgroup v2
    # reports it as inactive_file, v1 as cache.
    cache = details.get("inactive_file", details.get("cache", 0))
    return round(max(0, usage - cache) / 1024**2, 1)


class DockerCollector(Collector):
    name = "docker"
    inventory = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client = None

    def available(self) -> bool:
        self._client = docker_client()
        return self._client is not None

    def collect(self) -> dict[str, Any]:
        assert self._client is not None
        containers = self._client.containers.list(all=True)
        running = [c for c in containers if c.status == "running"]

        def one_stats(container) -> dict[str, Any] | None:
            try:
                return container.stats(stream=False)
            except Exception as exc:  # noqa: BLE001
                log.debug("stats failed for %s: %s", container.name, exc)
                return None

        stats_by_id: dict[str, dict[str, Any] | None] = {}
        if running:
            with ThreadPoolExecutor(min(_STATS_WORKERS, len(running))) as pool:
                for container, stats in zip(running, pool.map(one_stats, running)):
                    stats_by_id[container.id] = stats

        dockers: list[dict[str, Any]] = []
        for container in containers:
            try:
                # Config.Image comes from the already-fetched attrs.
                # container.image is NOT used: it performs an extra API
                # call that 404s when the image has been deleted while
                # the container still exists.
                image = (container.attrs.get("Config") or {}).get("Image") or ""
                entry: dict[str, Any] = {
                    "name": container.name,
                    "image": image,
                    "state": container.status,
                    "cpuPct": None,
                    "memMb": None,
                }
            except Exception as exc:  # noqa: BLE001 - skip broken container
                log.debug("container skipped: %s", exc)
                continue
            stats = stats_by_id.get(container.id)
            if stats:
                entry["cpuPct"] = _cpu_pct(stats)
                entry["memMb"] = _mem_mb(stats)
            dockers.append(entry)

        # Container counts live in the docker_state collector, which runs
        # at base cadence; this one only ships the heavyweight stats.
        return {"inventory": {"dockers": dockers}}
