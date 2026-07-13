"""Hardware model inventory: CPU, motherboard, BIOS, RAM modules.

Feeds the per-model views in the admin UI. GPU models come from the
GPU collectors (``gpus`` list) and disk models from the SMART collector
(``disks`` list); this one covers the rest of the machine.

Linux: /proc/cpuinfo + DMI sysfs (no root needed); RAM module details
via ``dmidecode`` when running as root (external tool, optional).
Windows: PowerShell CIM queries (Win32_Processor, Win32_BaseBoard,
Win32_PhysicalMemory).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.hardware")

_DMI_ROOT = "/sys/class/dmi/id"
_DMI_FIELDS = {
    "board_vendor": "boardVendor",
    "board_name": "boardModel",
    "sys_vendor": "systemVendor",
    "product_name": "systemModel",
    "bios_version": "biosVersion",
    "bios_date": "biosDate",
}
_PLACEHOLDERS = {
    "", "system product name", "to be filled by o.e.m.", "default string",
    "none", "not specified", "unknown", "n/a",
}

_PS_QUERY = (
    "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1;"
    "$bb = Get-CimInstance Win32_BaseBoard | Select-Object -First 1;"
    "$cs = Get-CimInstance Win32_ComputerSystem | Select-Object -First 1;"
    "$bios = Get-CimInstance Win32_BIOS | Select-Object -First 1;"
    "$ram = Get-CimInstance Win32_PhysicalMemory | ForEach-Object {"
    " '{0}|{1}|{2}|{3}' -f $_.Manufacturer, $_.PartNumber,"
    " [math]::Round($_.Capacity/1MB), $_.Speed };"
    "Write-Output ('CPU=' + $cpu.Name);"
    "Write-Output ('BOARDVENDOR=' + $bb.Manufacturer);"
    "Write-Output ('BOARDMODEL=' + $bb.Product);"
    "Write-Output ('SYSVENDOR=' + $cs.Manufacturer);"
    "Write-Output ('SYSMODEL=' + $cs.Model);"
    "Write-Output ('BIOS=' + $bios.SMBIOSBIOSVersion);"
    "$ram | ForEach-Object { Write-Output ('DIMM=' + $_) }"
)


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if value.lower() in _PLACEHOLDERS else value


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


_VM_MARKERS = (
    "kvm", "qemu", "vmware", "virtualbox", "vbox", "xen", "hyper-v",
    "virtual machine", "bochs", "parallels", "bhyve", "cloud",
)


def detect_virtualization(
    detect_virt_output: str, dmi_values: list[str], cpuinfo_flags: str = ""
) -> tuple[str, str | None]:
    """(machine type, hypervisor name): ``systemd-detect-virt`` when
    available, DMI strings and the CPUID hypervisor flag as fallback."""
    verdict = detect_virt_output.strip().lower()
    if verdict and verdict != "none":
        kind = "container" if verdict in ("lxc", "lxc-libvirt", "docker",
                                          "podman", "systemd-nspawn") else "vm"
        return kind, verdict
    joined = " ".join(v.lower() for v in dmi_values if v)
    for marker in _VM_MARKERS:
        if marker in joined:
            return "vm", marker
    if " hypervisor " in f" {cpuinfo_flags} ":
        return "vm", None
    return "physical", None


def _cpu_model_linux() -> str | None:
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key.strip() in ("model name", "Hardware", "Model"):
                    return _clean(value)
    except OSError:
        pass
    return None


def parse_dmidecode_memory(text: str) -> list[dict[str, Any]]:
    """RAM module list from ``dmidecode -t memory`` output."""
    modules: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("Memory Device"):
            current = {}
            continue
        if current is None or not line.startswith("\t"):
            continue
        key, _, value = line.strip().partition(": ")
        if key == "Size":
            match = re.match(r"(\d+)\s*(MB|GB)", value)
            if not match:
                current = None  # "No Module Installed" — empty slot
                continue
            size = int(match.group(1))
            current["sizeMb"] = size * 1024 if match.group(2) == "GB" else size
        elif key == "Type" and _clean(value):
            current["type"] = value
        elif key == "Speed" and (match := re.match(r"(\d+)", value)):
            current["speedMt"] = int(match.group(1))
        elif key == "Manufacturer" and _clean(value):
            current["vendor"] = value
        elif key == "Part Number" and _clean(value):
            current["model"] = value.strip()
            modules.append(current)
            current = None
    return modules


class HardwareCollector(Collector):
    name = "hardware"
    inventory = True

    def collect(self) -> dict[str, Any]:
        if sys.platform == "win32":
            hardware = self._collect_windows()
        else:
            hardware = self._collect_linux()

        import psutil

        hardware["cpuCores"] = psutil.cpu_count(logical=False)
        hardware["cpuThreads"] = psutil.cpu_count(logical=True)
        freq = psutil.cpu_freq()
        if freq and freq.max:
            hardware["cpuMaxMhz"] = int(freq.max)
        hardware["ramTotalMb"] = int(psutil.virtual_memory().total / 1024**2)
        return {"inventory": {"hardware": hardware}}

    def _collect_linux(self) -> dict[str, Any]:
        hardware: dict[str, Any] = {"cpuModel": _cpu_model_linux()}
        for fname, key in _DMI_FIELDS.items():
            value = _clean(_read_file(os.path.join(_DMI_ROOT, fname)))
            if value:
                hardware[key] = value
        flags = ""
        try:
            with open("/proc/cpuinfo", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if line.startswith("flags"):
                        flags = line
                        break
        except OSError:
            pass
        machine_type, hypervisor = detect_virtualization(
            _run(["systemd-detect-virt"], timeout=5.0),
            [
                hardware.get("systemVendor", ""),
                hardware.get("systemModel", ""),
                hardware.get("boardVendor", ""),
            ],
            flags,
        )
        hardware["machineType"] = machine_type
        if hypervisor:
            hardware["hypervisor"] = hypervisor
        if os.geteuid() == 0:
            modules = parse_dmidecode_memory(
                _run(["dmidecode", "-t", "memory"])
            )
            if modules:
                hardware["ramModules"] = modules
        return hardware

    def _collect_windows(self) -> dict[str, Any]:
        hardware: dict[str, Any] = {}
        modules: list[dict[str, Any]] = []
        output = _run(
            ["powershell", "-NoProfile", "-Command", _PS_QUERY], timeout=30.0
        )
        keys = {
            "CPU": "cpuModel",
            "BOARDVENDOR": "boardVendor",
            "BOARDMODEL": "boardModel",
            "SYSVENDOR": "systemVendor",
            "SYSMODEL": "systemModel",
            "BIOS": "biosVersion",
        }
        for line in output.splitlines():
            tag, _, value = line.strip().partition("=")
            if tag in keys:
                if cleaned := _clean(value):
                    hardware[keys[tag]] = cleaned
            elif tag == "DIMM":
                parts = value.split("|")
                if len(parts) == 4 and parts[2].isdigit():
                    modules.append(
                        {
                            "vendor": _clean(parts[0]),
                            "model": _clean(parts[1]),
                            "sizeMb": int(parts[2]),
                            "speedMt": int(parts[3]) if parts[3].isdigit() else None,
                        }
                    )
        if modules:
            hardware["ramModules"] = modules
        machine_type, hypervisor = detect_virtualization(
            "",
            [
                hardware.get("systemVendor", ""),
                hardware.get("systemModel", ""),
                hardware.get("boardVendor", ""),
            ],
        )
        hardware["machineType"] = machine_type
        if hypervisor:
            hardware["hypervisor"] = hypervisor
        return hardware


def _read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None
