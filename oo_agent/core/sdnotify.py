"""Minimal systemd notification client (no dependency on libsystemd).

Sends readiness and watchdog datagrams to ``$NOTIFY_SOCKET`` when the
agent runs under a ``Type=notify`` unit with ``WatchdogSec=``; a no-op
everywhere else (plain CLI runs, Windows, containers).
"""

from __future__ import annotations

import os
import socket


def notify(state: str) -> None:
    """Best-effort ``sd_notify(3)``: silently does nothing when not
    running under systemd or when the socket write fails."""
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return
    if addr.startswith("@"):  # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), addr)
    except OSError:
        pass


def ready() -> None:
    notify("READY=1")


def watchdog_ping() -> None:
    notify("WATCHDOG=1")
