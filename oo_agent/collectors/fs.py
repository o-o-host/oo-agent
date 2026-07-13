"""Filesystem usage per mounted real filesystem."""

from __future__ import annotations

import logging
from typing import Any

import psutil

from oo_agent.plugin import Collector

log = logging.getLogger("collector.fs")

# Pseudo/ephemeral filesystems that never belong on a capacity chart.
_SKIP_FSTYPES = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "fusectl", "tracefs", "ramfs",
    "autofs", "efivarfs", "nsfs", "iso9660",
}
_SKIP_PREFIXES = ("/snap/", "/var/lib/docker/", "/run/", "/proc/", "/sys/")


class FsCollector(Collector):
    name = "fs"

    def collect(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        disks: list[dict[str, Any]] = []
        seen_devices: set[str] = set()
        for part in psutil.disk_partitions(all=False):
            if part.fstype.lower() in _SKIP_FSTYPES:
                continue
            if part.mountpoint.startswith(_SKIP_PREFIXES):
                continue
            if part.device in seen_devices:  # bind mounts / btrfs subvolumes
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError as exc:
                log.debug("skip %s: %s", part.mountpoint, exc)
                continue
            seen_devices.add(part.device)
            mount = part.mountpoint
            metrics[f"fs.pused[{mount}]"] = round(usage.percent, 1)
            metrics[f"fs.used[{mount}]"] = usage.used
            metrics[f"fs.total[{mount}]"] = usage.total
            disks.append(
                {
                    "path": mount,
                    "device": part.device,
                    "fstype": part.fstype,
                    "usedPct": round(usage.percent, 1),
                    "usedGb": round(usage.used / 1024**3, 1),
                    "totalGb": round(usage.total / 1024**3, 1),
                }
            )
        return {"metrics": metrics, "inventory": {"disks": disks}}
