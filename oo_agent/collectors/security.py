"""Host security posture snapshot.

Inventory-cadence collector that answers the questions an operator asks
first when a box appears on the internet: what is listening and which
process owns it, are legacy remote-access services (telnet/ftp/vnc)
exposed, is a firewall actually on, is someone brute-forcing SSH, and
are security updates piling up.

Everything is best-effort: without root some process names are hidden
and the SSH log is unreadable — those fields are simply omitted.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time

import psutil

from oo_agent.plugin import Collector

log = logging.getLogger("collector.security")

# Ports whose exposure is worth an explicit flag even when the process
# name is unknown.
_RISKY_PORTS = {23: "telnet", 21: "ftp", 3389: "rdp", 5900: "vnc"}

_REMOTE_ACCESS = re.compile(
    r"sshd|dropbear|telnetd|vsftpd|proftpd|pure-ftpd|xrdp|"
    r"vnc|x11vnc|tigervnc|teamviewer|anydesk|rustdesk",
    re.IGNORECASE,
)

_FAILED_SSH = re.compile(r"Failed password|Invalid user")


def _cmd_output(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def listening_ports() -> list[dict]:
    """Deduplicated LISTEN sockets with owning process names."""
    procs: dict[int, str] = {}
    entries: dict[tuple[str, int], dict] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.Error as exc:
        log.debug("net_connections failed: %s", exc)
        return []
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        proto = "tcp"
        port = conn.laddr.port
        name = ""
        if conn.pid:
            if conn.pid not in procs:
                try:
                    procs[conn.pid] = psutil.Process(conn.pid).name()
                except psutil.Error:
                    procs[conn.pid] = ""
            name = procs[conn.pid]
        key = (proto, port)
        entry = entries.get(key)
        addr = conn.laddr.ip
        public = addr in ("0.0.0.0", "::", "")
        if entry is None:
            entries[key] = {
                "proto": proto, "port": port, "process": name,
                "addr": addr, "public": public,
            }
        else:
            entry["public"] = entry["public"] or public
            if name and not entry["process"]:
                entry["process"] = name
    result = sorted(entries.values(), key=lambda e: e["port"])
    for entry in result:
        risky = _RISKY_PORTS.get(entry["port"])
        if risky:
            entry["service_hint"] = risky
    return result


def remote_access_services(ports: list[dict]) -> list[dict]:
    """Remote-access daemons exposed on this host."""
    services = []
    for entry in ports:
        name = entry.get("process") or ""
        hint = entry.get("service_hint") or ""
        if _REMOTE_ACCESS.search(name) or hint in ("telnet", "rdp", "vnc", "ftp"):
            services.append({
                "port": entry["port"],
                "process": name or hint,
                "public": entry.get("public", False),
            })
    return services


def firewall_state() -> str:
    """'on' / 'off' / 'unknown'."""
    if sys.platform == "win32":
        out = _cmd_output(["netsh", "advfirewall", "show", "allprofiles"])
        if not out:
            return "unknown"
        states = re.findall(r"State\s+(\w+)", out)
        if not states:
            return "unknown"
        return "on" if any(s.lower() == "on" for s in states) else "off"
    out = _cmd_output(["ufw", "status"])
    if out:
        return "on" if "Status: active" in out else "off"
    out = _cmd_output(["firewall-cmd", "--state"])
    if out:
        return "on" if out.strip() == "running" else "off"
    out = _cmd_output(["nft", "list", "ruleset"], timeout=15)
    if out:
        # An empty ruleset prints nothing but table/chain headers.
        return "on" if "chain" in out else "off"
    out = _cmd_output(["iptables", "-S"])
    if out:
        rules = [ln for ln in out.splitlines() if not ln.startswith("-P")]
        return "on" if rules else "off"
    return "unknown"


def failed_ssh_logins_24h() -> int | None:
    """Count of failed SSH authentications in the last 24 h."""
    if sys.platform == "win32":
        return None
    out = _cmd_output(
        ["journalctl", "_COMM=sshd", "--since", "-24h",
         "--no-pager", "-o", "cat"],
        timeout=20,
    )
    if out:
        return sum(1 for line in out.splitlines() if _FAILED_SSH.search(line))
    for path in ("/var/log/auth.log", "/var/log/secure"):
        try:
            cutoff = time.time() - 86400
            if os.stat(path).st_mtime < cutoff:
                continue
            count = 0
            with open(path, errors="replace") as fh:
                for line in fh:
                    if _FAILED_SSH.search(line):
                        count += 1
            return count
        except OSError:
            continue
    return None


def pending_security_updates() -> int | None:
    """Number of pending updates (security when the distro separates
    them, total otherwise)."""
    if sys.platform == "win32":
        return None
    # Debian/Ubuntu: update-notifier writes "N updates; M security".
    out = _cmd_output(["/usr/lib/update-notifier/apt-check"], timeout=30)
    if not out:
        # apt-check prints to stderr by design; try again capturing it.
        try:
            result = subprocess.run(
                ["/usr/lib/update-notifier/apt-check"],
                capture_output=True, text=True, timeout=30,
            )
            out = result.stderr
        except (OSError, subprocess.TimeoutExpired):
            out = ""
    if out and ";" in out.strip():
        try:
            total, security = out.strip().split(";")[:2]
            return int(security) or int(total)
        except ValueError:
            pass
    out = _cmd_output(["apt", "list", "--upgradable"], timeout=30)
    if out:
        return max(0, len([ln for ln in out.splitlines() if "/" in ln]))
    try:
        result = subprocess.run(
            ["dnf", "-q", "updateinfo", "list", "security"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return len([ln for ln in result.stdout.splitlines() if ln.strip()])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


class SecurityCollector(Collector):
    name = "security"
    inventory = True

    def collect(self) -> dict:
        ports = listening_ports()
        remote = remote_access_services(ports)
        fw = firewall_state()
        failed = failed_ssh_logins_24h()
        updates = pending_security_updates()

        info: dict = {
            "ports": ports,
            "remote_access": remote,
            "firewall": fw,
        }
        if failed is not None:
            info["ssh_failed_24h"] = failed
        if updates is not None:
            info["updates_pending"] = updates

        metrics: dict = {
            "security.ports.listen": len(ports),
            "security.firewall": 1 if fw == "on" else 0,
        }
        if failed is not None:
            metrics["security.ssh_failed_24h"] = failed
        if updates is not None:
            metrics["security.updates.pending"] = updates
        return {"metrics": metrics, "inventory": {"security": info}}
