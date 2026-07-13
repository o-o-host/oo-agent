"""AMD GPU telemetry straight from amdgpu sysfs.

No third-party AMD library is used (the available ones are GPL);
everything needed lives under /sys/class/drm/card*/device: load,
VRAM, and the chip's own hwmon node for temperatures / power / fan.
Read-only sysfs access — zero impact on compute workloads.
"""

from __future__ import annotations

import glob
import logging
import os
import sys
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.gpu_amd")

_AMD_VENDOR = "0x1002"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_num(path: str, scale: float = 1.0) -> float | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return float(raw) / scale
    except ValueError:
        return None


def _cards() -> list[str]:
    cards = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if os.path.basename(card).count("-"):
            continue  # skip connector entries like card1-DP-1
        device = os.path.join(card, "device")
        if _read(os.path.join(device, "vendor")) == _AMD_VENDOR:
            cards.append(device)
    return cards


def _hwmon_dir(device: str) -> str | None:
    matches = glob.glob(os.path.join(device, "hwmon", "hwmon*"))
    return matches[0] if matches else None


def _temps(hwmon: str) -> dict[str, dict[str, float]]:
    """Temperature channels by label (edge/junction/mem)."""
    channels: dict[str, dict[str, float]] = {}
    for input_path in sorted(glob.glob(os.path.join(hwmon, "temp[0-9]*_input"))):
        prefix = input_path[: -len("_input")]
        value = _read_num(input_path, scale=1000.0)
        if value is None or value < -50 or value > 200:
            continue
        label = _read(prefix + "_label") or os.path.basename(prefix)
        entry: dict[str, float] = {"value": round(value, 1)}
        crit = _read_num(prefix + "_crit", scale=1000.0)
        emergency = _read_num(prefix + "_emergency", scale=1000.0)
        if crit is not None and 0 < crit <= 250:
            entry["max"] = round(crit, 1)
        if emergency is not None and 0 < emergency <= 250:
            entry["crit"] = round(emergency, 1)
        channels[label.lower()] = entry
    return channels


class AmdGpuCollector(Collector):
    name = "gpu_amd"

    def available(self) -> bool:
        return sys.platform.startswith("linux") and bool(_cards())

    def collect(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        sensors: list[dict[str, Any]] = []
        gpus: list[dict[str, Any]] = []
        for index, device in enumerate(_cards()):
            pci_id = _read(os.path.join(device, "device")) or "unknown"
            name = f"AMD GPU {pci_id}"
            load = _read_num(os.path.join(device, "gpu_busy_percent"))
            vram_used = _read_num(os.path.join(device, "mem_info_vram_used"))
            vram_total = _read_num(os.path.join(device, "mem_info_vram_total"))

            core_temp = vram_temp = power_w = fan_pct = None
            temp_channels: dict[str, dict[str, float]] = {}
            hwmon = _hwmon_dir(device)
            if hwmon:
                temp_channels = _temps(hwmon)
                core = temp_channels.get("junction") or temp_channels.get("edge")
                core_temp = core["value"] if core else None
                mem = temp_channels.get("mem")
                vram_temp = mem["value"] if mem else None
                # power1_average on older kernels, power1_input on new.
                power_w = _read_num(
                    os.path.join(hwmon, "power1_average"), scale=1e6
                ) or _read_num(os.path.join(hwmon, "power1_input"), scale=1e6)
                pwm = _read_num(os.path.join(hwmon, "pwm1"))
                pwm_max = _read_num(os.path.join(hwmon, "pwm1_max")) or 255.0
                if pwm is not None and pwm_max > 0:
                    fan_pct = round(pwm / pwm_max * 100)

            gpus.append(
                {
                    "vendor": "amd",
                    "name": name,
                    "load": round(load) if load is not None else None,
                    "memUsedMb": round(vram_used / 1024**2) if vram_used else None,
                    "memTotalMb": round(vram_total / 1024**2) if vram_total else None,
                    "coreTempC": core_temp,
                    "vramTempC": vram_temp,
                    "powerW": round(power_w, 1) if power_w is not None else None,
                    "fanPct": fan_pct,
                }
            )
            if load is not None:
                metrics[f"gpu.util[{index}]"] = round(load, 1)
            if vram_used is not None and vram_total:
                metrics[f"gpu.mem.used[{index}]"] = int(vram_used)
                metrics[f"gpu.mem.total[{index}]"] = int(vram_total)
                metrics[f"gpu.mem.pused[{index}]"] = round(
                    vram_used / vram_total * 100, 1
                )
            if power_w is not None:
                metrics[f"gpu.power[{index}]"] = round(power_w, 1)
            for label, channel in temp_channels.items():
                kind = "vram" if label == "mem" else "gpu"
                sensor: dict[str, Any] = {
                    "id": f"gpu:amd{index}/{label}",
                    "name": f"{name} · {label}",
                    "kind": kind,
                    "value": channel["value"],
                    "unit": "C",
                }
                if "max" in channel:
                    sensor["max"] = channel["max"]
                if "crit" in channel:
                    sensor["crit"] = channel["crit"]
                if "max" in sensor or "crit" in sensor:
                    sensor["threshold_source"] = "device"
                sensors.append(sensor)
        return {"metrics": metrics, "sensors": sensors, "inventory": {"gpus": gpus}}
