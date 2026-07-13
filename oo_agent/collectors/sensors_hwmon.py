"""Hardware sensors from the Linux hwmon sysfs interface.

Reads /sys/class/hwmon directly — the same source lm-sensors uses,
but with no native library dependency, so it works on bare servers.
Passport thresholds (``temp*_max`` / ``temp*_crit``) are read from the
driver where the hardware exposes them and shipped with
``threshold_source: "device"``; otherwise the server side assigns
thresholds from its accumulated model dataset.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.sensors")

_HWMON_ROOT = "/sys/class/hwmon"

# Driver name -> default sensor kind. Anything unknown falls back to
# "board". Per-label overrides below refine CPU cores and GPU VRAM.
_KIND_BY_DRIVER = {
    "coretemp": "cpu",
    "k10temp": "cpu",
    "zenpower": "cpu",
    "amdgpu": "gpu",
    "nouveau": "gpu",
    "i915": "gpu",
    "xe": "gpu",
    "nvme": "disk",
    "drivetemp": "disk",
    "spd5118": "ram",
    "jc42": "ram",
    "acpitz": "board",
    "pch_skylake": "chipset",
    "pch_cannonlake": "chipset",
}

_CORE_LABELS = ("core ",)
_VRAM_LABELS = ("mem", "vram")


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


def _kind(driver: str, label: str) -> str:
    lowered = label.lower()
    base = _KIND_BY_DRIVER.get(driver, "board")
    if base == "cpu" and lowered.startswith(_CORE_LABELS):
        return "core"
    if base == "gpu" and any(word in lowered for word in _VRAM_LABELS):
        return "vram"
    return base


def _temp_sensors(chip_dir: str, driver: str, chip_id: str) -> list[dict[str, Any]]:
    sensors: list[dict[str, Any]] = []
    for fname in sorted(os.listdir(chip_dir)):
        if not (fname.startswith("temp") and fname.endswith("_input")):
            continue
        prefix = fname[: -len("_input")]
        value = _read_num(os.path.join(chip_dir, fname), scale=1000.0)
        if value is None or value < -50 or value > 200:
            continue  # absent or bogus reading
        label = _read(os.path.join(chip_dir, f"{prefix}_label")) or ""
        sensor: dict[str, Any] = {
            "id": f"{chip_id}/{prefix}",
            "name": label or f"{driver} {prefix}",
            "kind": _kind(driver, label),
            "value": round(value, 1),
            "unit": "C",
        }
        maximum = _read_num(os.path.join(chip_dir, f"{prefix}_max"), scale=1000.0)
        crit = _read_num(os.path.join(chip_dir, f"{prefix}_crit"), scale=1000.0)
        emergency = _read_num(
            os.path.join(chip_dir, f"{prefix}_emergency"), scale=1000.0
        )
        # amdgpu exposes crit + emergency; treat crit as the warning
        # bound and emergency as the hard stop in that case.
        if emergency is not None and crit is not None and maximum is None:
            maximum, crit = crit, emergency
        if maximum is not None and 0 < maximum <= 200:
            sensor["max"] = round(maximum, 1)
        if crit is not None and 0 < crit <= 250:
            sensor["crit"] = round(crit, 1)
        if "max" in sensor or "crit" in sensor:
            sensor["threshold_source"] = "device"
        sensors.append(sensor)
    return sensors


def _fan_sensors(chip_dir: str, driver: str, chip_id: str) -> list[dict[str, Any]]:
    sensors: list[dict[str, Any]] = []
    for fname in sorted(os.listdir(chip_dir)):
        if not (fname.startswith("fan") and fname.endswith("_input")):
            continue
        prefix = fname[: -len("_input")]
        value = _read_num(os.path.join(chip_dir, fname))
        if value is None or value < 0:
            continue
        label = _read(os.path.join(chip_dir, f"{prefix}_label")) or ""
        sensors.append(
            {
                "id": f"{chip_id}/{prefix}",
                "name": label or f"{driver} {prefix}",
                "kind": "fan",
                "value": int(value),
                "unit": "rpm",
            }
        )
    return sensors


def _volt_sensors(chip_dir: str, driver: str, chip_id: str) -> list[dict[str, Any]]:
    sensors: list[dict[str, Any]] = []
    for fname in sorted(os.listdir(chip_dir)):
        if not (fname.startswith("in") and fname.endswith("_input")):
            continue
        prefix = fname[: -len("_input")]
        if not prefix[2:].isdigit():
            continue  # e.g. "intrusion0_input"
        value = _read_num(os.path.join(chip_dir, fname), scale=1000.0)
        if value is None or abs(value) > 100:
            continue
        label = _read(os.path.join(chip_dir, f"{prefix}_label")) or ""
        sensors.append(
            {
                "id": f"{chip_id}/{prefix}",
                "name": label or f"{driver} {prefix}",
                "kind": "board",
                "value": round(value, 3),
                "unit": "V",
            }
        )
    return sensors


def _power_sensors(chip_dir: str, driver: str, chip_id: str) -> list[dict[str, Any]]:
    """power*_input (instant) or power*_average (µW), with the driver's
    cap/crit limits as passport thresholds when present."""
    sensors: list[dict[str, Any]] = []
    for fname in sorted(os.listdir(chip_dir)):
        if not fname.startswith("power"):
            continue
        if not (fname.endswith("_input") or fname.endswith("_average")):
            continue
        prefix = fname.split("_", 1)[0]
        if any(s["id"] == f"{chip_id}/{prefix}" for s in sensors):
            continue  # _input already taken, skip the _average twin
        value = _read_num(os.path.join(chip_dir, fname), scale=1e6)
        if value is None or value < 0 or value > 20000:
            continue
        label = _read(os.path.join(chip_dir, f"{prefix}_label")) or ""
        sensor: dict[str, Any] = {
            "id": f"{chip_id}/{prefix}",
            "name": label or f"{driver} {prefix}",
            "kind": _KIND_BY_DRIVER.get(driver, "board"),
            "value": round(value, 1),
            "unit": "W",
        }
        cap = _read_num(os.path.join(chip_dir, f"{prefix}_cap"), scale=1e6)
        crit = _read_num(os.path.join(chip_dir, f"{prefix}_crit"), scale=1e6)
        if cap is not None and cap > 0:
            sensor["max"] = round(cap, 1)
        if crit is not None and crit > 0:
            sensor["crit"] = round(crit, 1)
        if "max" in sensor or "crit" in sensor:
            sensor["threshold_source"] = "device"
        sensors.append(sensor)
    return sensors


def _curr_sensors(chip_dir: str, driver: str, chip_id: str) -> list[dict[str, Any]]:
    sensors: list[dict[str, Any]] = []
    for fname in sorted(os.listdir(chip_dir)):
        if not (fname.startswith("curr") and fname.endswith("_input")):
            continue
        prefix = fname[: -len("_input")]
        value = _read_num(os.path.join(chip_dir, fname), scale=1000.0)
        if value is None or value < 0 or value > 1000:
            continue
        label = _read(os.path.join(chip_dir, f"{prefix}_label")) or ""
        sensors.append(
            {
                "id": f"{chip_id}/{prefix}",
                "name": label or f"{driver} {prefix}",
                "kind": "board",
                "value": round(value, 2),
                "unit": "A",
            }
        )
    return sensors


def _pwm_sensors(chip_dir: str, driver: str, chip_id: str) -> list[dict[str, Any]]:
    """Fan duty cycle (pwm* files, 0-255) as a percentage."""
    sensors: list[dict[str, Any]] = []
    for fname in sorted(os.listdir(chip_dir)):
        if not (fname.startswith("pwm") and fname[3:].isdigit()):
            continue
        value = _read_num(os.path.join(chip_dir, fname))
        if value is None or value < 0 or value > 255:
            continue
        label = _read(os.path.join(chip_dir, f"{fname}_label")) or ""
        sensors.append(
            {
                "id": f"{chip_id}/{fname}",
                "name": label or f"{driver} {fname} duty",
                "kind": "fan",
                "value": round(value / 255 * 100),
                "unit": "%",
            }
        )
    return sensors


class HwmonCollector(Collector):
    name = "sensors_hwmon"

    def available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        if not os.path.isdir(_HWMON_ROOT):
            return False
        if not os.listdir(_HWMON_ROOT):
            log.warning(
                "hwmon is empty — sensor kernel modules may be missing "
                "(try: modprobe coretemp / k10temp / drivetemp)"
            )
            return False
        return True

    def collect(self) -> dict[str, Any]:
        sensors: list[dict[str, Any]] = []
        driver_seen: dict[str, int] = {}
        for entry in sorted(os.listdir(_HWMON_ROOT)):
            chip_dir = os.path.join(_HWMON_ROOT, entry)
            driver = _read(os.path.join(chip_dir, "name")) or entry
            # Stable-ish chip id: driver name plus an occurrence index
            # for hosts with several chips of the same driver (two NVMe
            # drives, two DIMM sensors, ...).
            count = driver_seen.get(driver, 0)
            driver_seen[driver] = count + 1
            chip_id = f"hwmon:{driver}" if count == 0 else f"hwmon:{driver}-{count}"
            sensors.extend(_temp_sensors(chip_dir, driver, chip_id))
            sensors.extend(_fan_sensors(chip_dir, driver, chip_id))
            sensors.extend(_volt_sensors(chip_dir, driver, chip_id))
            sensors.extend(_power_sensors(chip_dir, driver, chip_id))
            sensors.extend(_curr_sensors(chip_dir, driver, chip_id))
            sensors.extend(_pwm_sensors(chip_dir, driver, chip_id))
        if not any(s["unit"] == "rpm" for s in sensors) and not getattr(
            self, "_fan_hint_logged", False
        ):
            self._fan_hint_logged = True
            log.info(
                "no fan tachometers found — on desktop boards load the "
                "Super I/O driver (modprobe it87 / nct6775)"
            )
        return {"sensors": sensors}
