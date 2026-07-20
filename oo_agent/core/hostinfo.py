"""Host identity: hostname, OS, architecture, stable fingerprint."""

from __future__ import annotations

import hashlib
import platform
import socket
import sys

from oo_agent import __version__

_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def _machine_id() -> str:
    """Stable per-host identifier.

    Linux: hashed /etc/machine-id. Windows: hashed MachineGuid from the
    registry. Fallback: hashed hostname (weak, but never empty).
    """
    raw = ""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            raw = ""
    else:
        for path in _MACHINE_ID_PATHS:
            try:
                with open(path, encoding="ascii") as fh:
                    raw = fh.read().strip()
                if raw:
                    break
            except OSError:
                continue
    if not raw:
        raw = socket.gethostname()
    # Mix in the hostname: VM clones from one image share an IDENTICAL
    # /etc/machine-id, so without this two different servers would yield the
    # same fingerprint and the second one could not enroll (409 "already bound").
    raw = f"{raw}|{socket.gethostname()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def agent_info() -> dict[str, str]:
    """The ``agent`` block of every push payload."""
    return {
        "version": __version__,
        "fingerprint": _machine_id(),
        "hostname": socket.gethostname(),
        "os": "windows" if sys.platform == "win32" else sys.platform,
        "os_version": platform.platform(),
        "arch": platform.machine(),
    }
