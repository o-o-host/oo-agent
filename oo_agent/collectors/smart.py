"""SMART disk health via the external ``smartctl`` binary.

smartmontools is GPL, so it is never linked or vendored — the agent
only runs the system ``smartctl`` as a subprocess and parses its
``--json`` output. Needs root (raw device access); on unprivileged
runs the collector reports itself unavailable.

Temperature limits come from the hardware where exposed:
- NVMe: WCTEMP/CCTEMP (also visible through hwmon, ids differ);
- SATA: SCT "Op limit max" (smartctl merges it into ``temperature``).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.smart")

_TIMEOUT = 30  # seconds per device

# ATA attributes used for the summary fields.
_ATTR_REALLOCATED = 5
_ATTR_WEAR = {
    177: "normalized",  # Wear_Leveling_Count -> wear = 100 - value
    233: "normalized",  # Media_Wearout_Indicator
    231: "normalized",  # SSD_Life_Left
}


def _run(args: list[str]) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("smartctl failed: %s (%s)", exc, " ".join(args))
        return None
    # smartctl exit status is a bit mask; JSON is emitted even when
    # some bits are set (e.g. failing SMART status), so parse anyway.
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.debug("smartctl non-JSON output for %s", " ".join(args))
        return None


def _wear_pct(data: dict[str, Any]) -> int | None:
    nvme_log = data.get("nvme_smart_health_information_log") or {}
    if "percentage_used" in nvme_log:
        return int(nvme_log["percentage_used"])
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    for attr in table:
        if attr.get("id") in _ATTR_WEAR:
            value = attr.get("value")
            if isinstance(value, int):
                return max(0, 100 - value)
    return None


def _reallocated(data: dict[str, Any]) -> int | None:
    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log is not None:
        return int(nvme_log.get("media_errors", 0))
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    for attr in table:
        if attr.get("id") == _ATTR_REALLOCATED:
            raw = (attr.get("raw") or {}).get("value")
            if isinstance(raw, int):
                return raw
    return None


def _disk_type(data: dict[str, Any]) -> str:
    protocol = ((data.get("device") or {}).get("protocol") or "").lower()
    if protocol == "nvme":
        return "NVMe"
    rotation = data.get("rotation_rate")
    if isinstance(rotation, int) and rotation > 0:
        return "HDD"
    return "SSD"


def _attribute_dump(data: dict[str, Any]) -> Any:
    """Compact full attribute dump for server-side storage."""
    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log is not None:
        return nvme_log
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    return [
        {
            "id": attr.get("id"),
            "name": attr.get("name"),
            "value": attr.get("value"),
            "worst": attr.get("worst"),
            "thresh": attr.get("thresh"),
            "raw": (attr.get("raw") or {}).get("value"),
            "failing_now": bool((attr.get("when_failed") or "") == "now"),
        }
        for attr in table
    ]


class SmartCollector(Collector):
    name = "smart"
    inventory = True

    def available(self) -> bool:
        if shutil.which("smartctl") is None:
            log.info("smartctl not found — install smartmontools")
            return False
        scan = _run(["smartctl", "--scan", "--json=c"])
        devices = (scan or {}).get("devices") or []
        if not devices:
            log.info("smartctl found no devices (or no permission)")
            return False
        return True

    def collect(self) -> dict[str, Any]:
        scan = _run(["smartctl", "--scan", "--json=c"]) or {}
        disks: list[dict[str, Any]] = []
        sensors: list[dict[str, Any]] = []
        for device in scan.get("devices") or []:
            dev = device.get("name")
            if not dev or dev.startswith("/dev/bus/"):
                continue  # RAID pass-through handling is a later milestone
            # -n standby: never wake a spun-down disk just to poll it.
            data = _run(["smartctl", "-x", "--json=c", "-n", "standby", dev])
            if not data or data.get("model_name") is None:
                continue
            temperature = data.get("temperature") or {}
            temp_c = temperature.get("current")
            capacity = (data.get("user_capacity") or {}).get("bytes")
            smart_status = data.get("smart_status") or {}
            disk: dict[str, Any] = {
                "dev": dev,
                "type": _disk_type(data),
                "model": data.get("model_name"),
                "serial": data.get("serial_number"),
                "firmware": data.get("firmware_version"),
                "sizeGb": round(capacity / 1000**3) if capacity else None,
                "tempC": temp_c,
                "health": "ok" if smart_status.get("passed") else "fail",
                "powerOnHours": (data.get("power_on_time") or {}).get("hours"),
                "wearPct": _wear_pct(data),
                "reallocated": _reallocated(data),
                "attributes": _attribute_dump(data),
            }
            disks.append(disk)
            if isinstance(temp_c, (int, float)):
                sensor: dict[str, Any] = {
                    "id": f"smart:{dev}",
                    "name": f"{disk['model']} ({dev})",
                    "kind": "disk",
                    "value": float(temp_c),
                    "unit": "C",
                }
                # SATA SCT operating limit; NVMe WCTEMP/CCTEMP land in
                # these same keys in smartctl JSON.
                op_max = temperature.get("op_limit_max")
                crit = temperature.get("limit_max")
                if isinstance(op_max, (int, float)) and op_max > 0:
                    sensor["max"] = float(op_max)
                if isinstance(crit, (int, float)) and crit > 0:
                    sensor["crit"] = float(crit)
                if "max" in sensor or "crit" in sensor:
                    sensor["threshold_source"] = "device"
                sensors.append(sensor)
        return {"sensors": sensors, "inventory": {"smart": disks}}
