"""NVIDIA GPU telemetry via NVML (nvidia-ml-py, the official bindings).

Works identically on Linux and Windows. All calls are read-only
queries — zero impact on running CUDA workloads.

Shipped per GPU: utilization, VRAM, core temperature (VRAM junction
where the hardware exposes it), power draw, fan speed, passport
thresholds (slowdown / shutdown / board max / VRAM max) and the
hardware's own thermal-throttle verdict as ``gpu.throttle[i]``.

VRAM temperature fallback: consumer GeForce boards (e.g. RTX 3090)
do not expose GDDR6/GDDR6X memory temperature through NVML. When the
agent runs as root on Linux and the external ``gddr6`` tool is
installed (https://github.com/olealgoritme/gddr6, reads the sensor
from PCI BAR space), it is invoked as a subprocess and its readings
are matched to NVML devices by PCI bus number. Configure with
``gddr6 = /path/to/gddr6`` or ``gddr6 = off`` in
``[collector:gpu_nvidia]``; default is auto-detect.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.gpu_nvidia")

# Thermal bits of nvmlDeviceGetCurrentClocksThrottleReasons.
_THROTTLE_SW_THERMAL = 0x0000000000000020
_THROTTLE_HW_THERMAL = 0x0000000000000040
_THERMAL_MASK = _THROTTLE_SW_THERMAL | _THROTTLE_HW_THERMAL

_GDDR6_DEFAULT_PATHS = ("/usr/local/bin/gddr6", "/usr/bin/gddr6")
# "Device: RTX 3090 GDDR6X (GA102 / 0x2204) pci=84:0:0"
_GDDR6_DEVICE_RE = re.compile(r"^Device: .+ pci=([0-9a-fA-F]+):", re.MULTILINE)
# "VRAM Temps: |  40°C |  40°C |" (repeated once per poll)
_GDDR6_TEMP_RE = re.compile(r"(\d+)\s*°C")


def parse_gddr6_output(text: str) -> tuple[dict[int, float], list[float]]:
    """Parse ``gddr6`` tool output into VRAM temperatures.

    Returns (bus number -> temp, temps in device order). Only the first
    sample row is used; the tool streams one row per poll interval.
    """
    buses = [int(b, 16) for b in _GDDR6_DEVICE_RE.findall(text)]
    temps = [float(t) for t in _GDDR6_TEMP_RE.findall(text)][: len(buses)]
    temps = [t for t in temps if 0 < t <= 150]
    by_bus = {bus: temp for bus, temp in zip(buses, temps)}
    return by_bus, temps


def _run_gddr6(binary: str) -> str:
    """Run the streaming ``gddr6`` tool for ~2s and return its output."""
    proc = subprocess.Popen(  # noqa: S603 - fixed binary path, no shell
        [binary], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        out, _ = proc.communicate(timeout=2.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return (out or b"").decode("utf-8", errors="replace")


def _try(func, *args):
    """NVML query wrapper: unsupported-on-this-GPU returns None."""
    try:
        return func(*args)
    except Exception:  # noqa: BLE001 - NVMLError covers many subtypes
        return None


class NvidiaGpuCollector(Collector):
    name = "gpu_nvidia"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._nvml = None
        self._gddr6_bin: str | None = None
        self._gddr6_resolved = False

    def available(self) -> bool:
        try:
            import pynvml
        except ImportError:
            log.info("nvidia-ml-py not installed — NVIDIA telemetry off")
            return False
        try:
            pynvml.nvmlInit()
        except Exception as exc:  # noqa: BLE001 - no driver / no GPU
            log.info("NVML init failed: %s", exc)
            return False
        if pynvml.nvmlDeviceGetCount() == 0:
            pynvml.nvmlShutdown()
            return False
        self._nvml = pynvml
        return True

    def _thresholds(self, handle) -> dict[str, float]:
        nv = self._nvml
        out: dict[str, float] = {}
        pairs = (
            ("slowdown", nv.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN),
            ("shutdown", nv.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN),
            ("gpu_max", getattr(nv, "NVML_TEMPERATURE_THRESHOLD_GPU_MAX", None)),
            ("mem_max", getattr(nv, "NVML_TEMPERATURE_THRESHOLD_MEM_MAX", None)),
        )
        for key, const in pairs:
            if const is None:
                continue
            value = _try(nv.nvmlDeviceGetTemperatureThreshold, handle, const)
            if isinstance(value, int) and 0 < value <= 250:
                out[key] = float(value)
        return out

    def _gddr6_path(self) -> str | None:
        """Resolve the gddr6 binary once; None when unusable here."""
        if self._gddr6_resolved:
            return self._gddr6_bin
        self._gddr6_resolved = True
        setting = str(self.config.get("gddr6", "auto")).strip()
        if setting.lower() in ("off", "false", "no", "0"):
            return None
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            # The tool maps PCI BAR space through /dev/mem: root only.
            return None
        if setting.lower() not in ("auto", ""):
            candidates: tuple[str, ...] = (setting,)
        else:
            found = shutil.which("gddr6")
            candidates = ((found,) if found else ()) + _GDDR6_DEFAULT_PATHS
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                self._gddr6_bin = path
                log.info("VRAM temperature fallback: using %s", path)
                return path
        return None

    def _gddr6_temps(self) -> tuple[dict[int, float], list[float]]:
        """VRAM temps from the external gddr6 tool (empty when absent)."""
        binary = self._gddr6_path()
        if binary is None:
            return {}, []
        try:
            return parse_gddr6_output(_run_gddr6(binary))
        except OSError as exc:
            log.warning("gddr6 fallback failed, disabled: %s", exc)
            self._gddr6_bin = None
            return {}, []

    def _mem_temp(self, handle) -> float | None:
        nv = self._nvml
        field = getattr(nv, "NVML_FI_DEV_MEMORY_TEMP", None)
        if field is None:
            return None
        values = _try(nv.nvmlDeviceGetFieldValues, handle, [field])
        if not values:
            return None
        value = values[0]
        if getattr(value, "nvmlReturn", 1) != 0:
            return None  # not supported on this GPU (e.g. GTX 16xx)
        raw = value.value.uiVal or value.value.ullVal
        return float(raw) if 0 < raw <= 200 else None

    def collect(self) -> dict[str, Any]:
        nv = self._nvml
        assert nv is not None
        metrics: dict[str, Any] = {}
        sensors: list[dict[str, Any]] = []
        gpus: list[dict[str, Any]] = []
        gddr6: tuple[dict[int, float], list[float]] | None = None
        for index in range(nv.nvmlDeviceGetCount()):
            handle = nv.nvmlDeviceGetHandleByIndex(index)
            name = nv.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()

            util = _try(nv.nvmlDeviceGetUtilizationRates, handle)
            memory = _try(nv.nvmlDeviceGetMemoryInfo, handle)
            core_temp = _try(
                nv.nvmlDeviceGetTemperature, handle, nv.NVML_TEMPERATURE_GPU
            )
            vram_temp = self._mem_temp(handle)
            if vram_temp is None:
                if gddr6 is None:
                    # One external tool run covers every GPU this pass.
                    gddr6 = self._gddr6_temps()
                by_bus, ordered = gddr6
                pci = _try(nv.nvmlDeviceGetPciInfo, handle)
                bus = getattr(pci, "bus", None) if pci else None
                if bus is not None and bus in by_bus:
                    vram_temp = by_bus[bus]
                elif index < len(ordered):
                    vram_temp = ordered[index]
            power_mw = _try(nv.nvmlDeviceGetPowerUsage, handle)
            limit_mw = _try(nv.nvmlDeviceGetEnforcedPowerLimit, handle)
            fan_pct = _try(nv.nvmlDeviceGetFanSpeed, handle)
            thresholds = self._thresholds(handle)
            reasons = _try(nv.nvmlDeviceGetCurrentClocksThrottleReasons, handle)
            throttling = int(bool(reasons and reasons & _THERMAL_MASK))

            gpus.append(
                {
                    "vendor": "nvidia",
                    "name": name,
                    "load": util.gpu if util else None,
                    "memUsedMb": round(memory.used / 1024**2) if memory else None,
                    "memTotalMb": round(memory.total / 1024**2) if memory else None,
                    "coreTempC": core_temp,
                    "vramTempC": vram_temp,
                    "powerW": round(power_mw / 1000, 1) if power_mw else None,
                    "powerLimitW": round(limit_mw / 1000, 1) if limit_mw else None,
                    "fanPct": fan_pct,
                    "thresholds": thresholds or None,
                }
            )
            if util:
                metrics[f"gpu.util[{index}]"] = util.gpu
            if memory:
                metrics[f"gpu.mem.used[{index}]"] = memory.used
                metrics[f"gpu.mem.total[{index}]"] = memory.total
                metrics[f"gpu.mem.pused[{index}]"] = round(
                    memory.used / memory.total * 100, 1
                )
            if power_mw:
                metrics[f"gpu.power[{index}]"] = round(power_mw / 1000, 1)
            if fan_pct is not None:
                metrics[f"gpu.fan[{index}]"] = fan_pct
            metrics[f"gpu.throttle[{index}]"] = throttling

            if core_temp is not None:
                sensor: dict[str, Any] = {
                    "id": f"gpu:nv{index}/core",
                    "name": f"{name} · core",
                    "kind": "gpu",
                    "value": float(core_temp),
                    "unit": "C",
                }
                if "slowdown" in thresholds:
                    sensor["max"] = thresholds["slowdown"]
                if "shutdown" in thresholds:
                    sensor["crit"] = thresholds["shutdown"]
                if "max" in sensor or "crit" in sensor:
                    sensor["threshold_source"] = "device"
                sensors.append(sensor)
            if vram_temp is not None:
                sensor = {
                    "id": f"gpu:nv{index}/vram",
                    "name": f"{name} · VRAM",
                    "kind": "vram",
                    "value": vram_temp,
                    "unit": "C",
                }
                if "mem_max" in thresholds:
                    sensor["max"] = thresholds["mem_max"]
                    sensor["threshold_source"] = "device"
                sensors.append(sensor)
        return {"metrics": metrics, "sensors": sensors, "inventory": {"gpus": gpus}}
