"""INI configuration with defaults.

The agent runs fine with no config file at all; every option has a
sane default. Per-collector sections are named ``[collector:<name>]``.
"""

from __future__ import annotations

import configparser
import os
import sys
from typing import Any

DEFAULT_PATHS = (
    "/etc/oo-agent/agent.ini",
    os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"), "oo-agent", "agent.ini"
    )
    if sys.platform == "win32"
    else None,
)

AGENT_DEFAULTS: dict[str, Any] = {
    "interval": 60,
    "inventory_interval": 600,
    "log_level": "INFO",
    "plugins_dir": "/etc/oo-agent/plugins"
    if sys.platform != "win32"
    else os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"), "oo-agent", "plugins"
    ),
}


class AgentConfig:
    """Parsed configuration: [agent], [transport] and collector sections."""

    def __init__(self, path: str | None = None) -> None:
        self.path = self._resolve_path(path)
        parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        if self.path:
            parser.read(self.path, encoding="utf-8")
        self._parser = parser

        self.agent: dict[str, Any] = dict(AGENT_DEFAULTS)
        if parser.has_section("agent"):
            for key, value in parser.items("agent"):
                self.agent[key] = self._coerce(value)

        self.transport: dict[str, Any] = (
            {k: self._coerce(v) for k, v in parser.items("transport")}
            if parser.has_section("transport")
            else {}
        )

    @staticmethod
    def _resolve_path(path: str | None) -> str | None:
        if path:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"config file not found: {path}")
            return path
        for candidate in DEFAULT_PATHS:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    @staticmethod
    def _coerce(value: str) -> Any:
        text = value.strip()
        lowered = text.lower()
        if lowered in ("true", "yes", "on"):
            return True
        if lowered in ("false", "no", "off"):
            return False
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        return text

    def collector(self, name: str) -> dict[str, Any]:
        """Options for one collector; empty dict when not configured."""
        section = f"collector:{name}"
        if not self._parser.has_section(section):
            return {}
        return {k: self._coerce(v) for k, v in self._parser.items(section)}
