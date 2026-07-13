"""Windows sensors via LibreHardwareMonitor (pythonnet bindings).

psutil exposes no temperatures on Windows, so this collector loads
LibreHardwareMonitorLib through the ``HardwareMonitor`` PyPI package
(MIT bindings over the MPL-2.0 library, used as a prebuilt DLL) and
walks its sensor tree: CPU, motherboard, memory, storage and AMD /
Intel GPUs.

NVIDIA GPUs are skipped by default — the gpu_nvidia collector already
covers them through NVML with passport thresholds; set ``nvidia =
true`` in ``[collector:sensors_lhm]`` to include them anyway.

Full coverage (motherboard voltages/fans) requires the service to run
elevated and the PawnIO driver (both handled by the installer); when
running unprivileged the collector simply reports fewer sensors.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.sensors_lhm")

# LibreHardwareMonitor SensorType name -> our (unit, value rounding).
_UNITS = {
    "Temperature": "C",
    "Fan": "rpm",
    "Voltage": "V",
    "Power": "W",
    "Current": "A",
    "Control": "%",  # fan duty cycle
}

# HardwareType name -> default sensor kind.
_KIND_BY_HARDWARE = {
    "Cpu": "cpu",
    "Motherboard": "board",
    "SuperIO": "board",
    "Memory": "ram",
    "Storage": "disk",
    "GpuNvidia": "gpu",
    "GpuAmd": "gpu",
    "GpuIntel": "gpu",
}


def _kind(hardware_type: str, sensor_name: str) -> str:
    kind = _KIND_BY_HARDWARE.get(hardware_type, "board")
    lowered = sensor_name.lower()
    if kind == "cpu" and "core" in lowered and "package" not in lowered:
        return "core"
    if kind == "gpu" and ("memory" in lowered or "hot spot" in lowered):
        # GPU Memory Junction reads map to the VRAM scale in the UI.
        return "vram" if "memory" in lowered else "gpu"
    if kind == "board" and ("pch" in lowered or "chipset" in lowered):
        return "chipset"
    return kind


class LhmSensorsCollector(Collector):
    name = "sensors_lhm"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._computer = None
        self._include_nvidia = bool(self.config.get("nvidia", False))

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from HardwareMonitor.Hardware import Computer
        except ImportError:
            log.info(
                "HardwareMonitor package not installed — Windows sensors off "
                "(pip install oo-agent[windows])"
            )
            return False
        try:
            computer = Computer()
            computer.IsCpuEnabled = True
            computer.IsMotherboardEnabled = True
            computer.IsMemoryEnabled = True
            computer.IsStorageEnabled = True
            computer.IsGpuEnabled = True
            computer.Open()
        except Exception as exc:  # noqa: BLE001 - .NET load errors vary
            log.info("LibreHardwareMonitor failed to open: %s", exc)
            return False
        self._computer = computer
        return True

    def _walk(self, hardware, sensors: list[dict[str, Any]]) -> None:
        hardware.Update()
        hardware_type = str(hardware.HardwareType)
        if hardware_type == "GpuNvidia" and not self._include_nvidia:
            return
        for sensor in hardware.Sensors:
            unit = _UNITS.get(str(sensor.SensorType))
            if unit is None or sensor.Value is None:
                continue
            value = float(sensor.Value)
            if unit == "C" and (value < -50 or value > 200):
                continue  # discard obviously bogus readings
            sensors.append(
                {
                    "id": f"lhm:{sensor.Identifier}",
                    "name": f"{hardware.Name} · {sensor.Name}",
                    "kind": _kind(hardware_type, str(sensor.Name)),
                    "value": round(value, 1),
                    "unit": unit,
                }
            )
        for sub in hardware.SubHardware:
            self._walk(sub, sensors)

    def collect(self) -> dict[str, Any]:
        assert self._computer is not None
        sensors: list[dict[str, Any]] = []
        for hardware in self._computer.Hardware:
            self._walk(hardware, sensors)
        if not sensors:
            log.warning(
                "no sensors from LibreHardwareMonitor — run elevated and "
                "install the PawnIO driver for motherboard sensors"
            )
        return {"sensors": sensors}
