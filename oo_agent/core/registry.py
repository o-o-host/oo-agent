"""Collector discovery: built-in modules plus drop-in plugin files.

Discovery rules:
- every module in ``oo_agent.collectors`` is imported; all Collector
  subclasses defined there are instantiated;
- every ``*.py`` file in the plugins directory is loaded the same way,
  its collectors are marked as custom (``custom.<name>.*`` prefix);
- a collector is skipped (never fatally) when: config disables it,
  ``available()`` returns False, or its import/instantiation raises.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
from dataclasses import dataclass, field

from oo_agent import collectors as builtin_pkg
from oo_agent.core.config import AgentConfig
from oo_agent.plugin import Collector

log = logging.getLogger("registry")

# pkgutil.iter_modules() cannot enumerate packages inside a PyInstaller
# bundle, so frozen builds fall back to this explicit list.
_BUILTIN_MODULES = (
    "system",
    "cron",
    "hardware",
    "rapl",
    "fs",
    "diskio",
    "net",
    "tcpconn",
    "sensors_hwmon",
    "sensors_lhm",
    "smart",
    "containers",
    "docker_state",
    "gpu_nvidia",
    "gpu_amd",
)


@dataclass
class Entry:
    """A scheduled collector with its runtime state."""

    collector: Collector
    interval: int
    custom: bool = False
    disabled: bool = False
    skipped_reason: str | None = field(default=None)

    @property
    def name(self) -> str:
        return self.collector.name


def _collector_classes(module) -> list[type[Collector]]:
    return [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Collector)
        and obj is not Collector
        and obj.__module__ == module.__name__
    ]


def _instantiate(
    cls: type[Collector], config: AgentConfig, custom: bool
) -> Entry | None:
    options = config.collector(cls.name)
    if options.get("enabled") is False:
        log.info("collector %s: disabled by config", cls.name)
        return None
    try:
        collector = cls(options)
        if not collector.available():
            log.info("collector %s: not available on this host", cls.name)
            return None
    except Exception as exc:  # noqa: BLE001 - isolation by design
        log.warning("collector %s: failed to initialize: %s", cls.name, exc)
        return None
    default = (
        config.agent["inventory_interval"]
        if collector.inventory
        else config.agent["interval"]
    )
    interval = int(options.get("interval") or collector.interval or default)
    return Entry(collector=collector, interval=interval, custom=custom)


def _builtin_module_names() -> list[str]:
    names = [info.name for info in pkgutil.iter_modules(builtin_pkg.__path__)]
    if not names:
        names = list(_BUILTIN_MODULES)
    return names


def _load_builtin(config: AgentConfig) -> list[Entry]:
    entries: list[Entry] = []
    for name in _builtin_module_names():
        try:
            module = importlib.import_module(f"{builtin_pkg.__name__}.{name}")
        except Exception as exc:  # noqa: BLE001
            log.warning("collector module %s: import failed: %s", name, exc)
            continue
        for cls in _collector_classes(module):
            entry = _instantiate(cls, config, custom=False)
            if entry:
                entries.append(entry)
    return entries


def _load_plugins(config: AgentConfig) -> list[Entry]:
    plugins_dir = str(config.agent.get("plugins_dir") or "")
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return []
    entries: list[Entry] = []
    for fname in sorted(os.listdir(plugins_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(plugins_dir, fname)
        mod_name = f"oo_agent_plugin_{fname[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin %s: load failed: %s", fname, exc)
            continue
        for cls in _collector_classes(module):
            entry = _instantiate(cls, config, custom=True)
            if entry:
                entries.append(entry)
                log.info("plugin loaded: %s (%s)", cls.name, fname)
    return entries


def discover(config: AgentConfig) -> list[Entry]:
    """All runnable collectors: built-ins first, then drop-in plugins."""
    entries = _load_builtin(config) + _load_plugins(config)
    names = [e.name for e in entries]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        log.warning("duplicate collector names: %s", ", ".join(sorted(dupes)))
    return entries
