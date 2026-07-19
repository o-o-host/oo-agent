"""Agent lifecycle commands: ``oo-agent uninstall`` and ``oo-agent update``.

Both commands operate on the standard install layout produced by the
shipped installers (Linux: /opt/oo-agent venv + /etc/oo-agent config +
systemd unit; Windows: a venv under Program Files plus config in
ProgramData and a Windows service). They degrade gracefully when a
piece is missing — a manual pip install without a service is cleaned
up just as well.

Self-update contract: ``<site>/dl/agent.json`` is a manifest
``{"agent_version": "...", "url": "...", "sha256": "..."}``. The
tarball is downloaded, checksum-verified, pip-installed into the
current venv and the service is restarted detached (the restart must
outlive this process).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

from oo_agent import __version__
from oo_agent.transport.client import DEFAULT_TOKEN_FILE

log = logging.getLogger("lifecycle")

MANIFEST_PATH = "/dl/agent.json"
SERVICE_NAME = "oo-agent"

_LINUX_UNIT = "/etc/systemd/system/oo-agent.service"
_LINUX_DIRS = ("/etc/oo-agent", "/var/lib/oo-agent")


def _run(cmd: list[str]) -> int:
    """Run a command, tolerate absence of the binary; returns exit code."""
    try:
        return subprocess.run(
            cmd, check=False, capture_output=True, timeout=60
        ).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("%s failed: %s", cmd[0], exc)
        return -1


def _install_prefix() -> str | None:
    """The venv the agent runs from — only if it looks like our own
    dedicated install dir (never remove a shared interpreter)."""
    prefix = sys.prefix
    if os.path.basename(os.path.dirname(prefix)) == "oo-agent" or \
            os.path.basename(prefix) == "oo-agent":
        return os.path.dirname(prefix) if \
            os.path.basename(prefix) == "venv" else prefix
    return None


def _detached(shell_cmd: str) -> None:
    """Fire and forget: survives this process exiting."""
    if sys.platform == "win32":
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", shell_cmd],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    else:
        subprocess.Popen(["sh", "-c", shell_cmd], start_new_session=True)


# ── uninstall ──────────────────────────────────────────────────────────

def uninstall(assume_yes: bool = False) -> int:
    """Stop the service and remove the agent, its config, token and
    state from this machine."""
    if not assume_yes:
        answer = input(
            "This removes the oo-agent service, config and token "
            "from this machine. Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    prefix = _install_prefix()
    if sys.platform == "win32":
        _run(["sc", "stop", SERVICE_NAME])
        _run(["sc", "delete", SERVICE_NAME])
        state_dir = os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"), "oo-agent"
        )
        shutil.rmtree(state_dir, ignore_errors=True)
        print(f"removed service and {state_dir}")
        if prefix:
            # The running interpreter lives inside prefix — delete it
            # after we exit.
            _detached(
                f"Start-Sleep 2; Remove-Item -Recurse -Force '{prefix}'"
            )
            print(f"scheduled removal of {prefix}")
        return 0

    _run(["systemctl", "disable", "--now", SERVICE_NAME])
    try:
        os.remove(_LINUX_UNIT)
    except OSError:
        pass
    _run(["systemctl", "daemon-reload"])
    for path in _LINUX_DIRS:
        shutil.rmtree(path, ignore_errors=True)
    try:
        os.remove("/usr/local/bin/oo-agent")
    except OSError:
        pass
    print("removed service unit, /etc/oo-agent and /var/lib/oo-agent")
    if prefix:
        _detached(f"sleep 2; rm -rf '{prefix}'")
        print(f"scheduled removal of {prefix}")
    return 0


# ── self-update ────────────────────────────────────────────────────────

def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _site_base(server: str) -> str:
    """Manifest lives at the site root: strip a trailing /api from the
    backend URL the agent is configured with."""
    base = server.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def check_manifest(server: str, verify_tls: bool = True,
                   http_transport=None) -> dict | None:
    """Fetch and sanity-check the version manifest; None when
    unavailable."""
    from oo_agent.transport.client import _import_httpx

    httpx = _import_httpx()
    url = _site_base(server) + MANIFEST_PATH
    try:
        with httpx.Client(
            timeout=15, verify=verify_tls, transport=http_transport
        ) as client:
            response = client.get(url)
            if response.status_code != 200:
                log.warning("manifest %s: HTTP %d", url, response.status_code)
                return None
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("manifest fetch failed: %s", exc)
        return None
    if not isinstance(data, dict) or not data.get("agent_version"):
        log.warning("manifest %s: malformed", url)
        return None
    return data


def self_update(server: str, verify_tls: bool = True,
                restart: bool = True, http_transport=None) -> int:
    """Update to the manifest version if it is newer. Returns 0 when
    already current or updated, 1 on failure."""
    manifest = check_manifest(server, verify_tls, http_transport)
    if manifest is None:
        return 1
    latest = str(manifest["agent_version"])
    if _version_tuple(latest) <= _version_tuple(__version__):
        log.info("already up to date (%s)", __version__)
        return 0

    url = str(manifest.get("url") or _site_base(server) + "/dl/oo-agent.tar.gz")
    expected_sha = str(manifest.get("sha256") or "").lower()
    from oo_agent.transport.client import _import_httpx

    httpx = _import_httpx()
    log.info("updating %s -> %s from %s", __version__, latest, url)
    try:
        with httpx.Client(timeout=120, verify=verify_tls,
                          transport=http_transport) as client:
            response = client.get(url, follow_redirects=True)
            if response.status_code != 200:
                log.warning("download failed: HTTP %d", response.status_code)
                return 1
            blob = response.content
    except Exception as exc:  # noqa: BLE001
        log.warning("download failed: %s", exc)
        return 1
    if expected_sha:
        actual = hashlib.sha256(blob).hexdigest()
        if actual != expected_sha:
            log.error("checksum mismatch: expected %s got %s",
                      expected_sha, actual)
            return 1

    with tempfile.NamedTemporaryFile(
        suffix=".tar.gz", delete=False
    ) as tmp:
        tmp.write(blob)
        tarball = tmp.name
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--upgrade", tarball],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            log.error("pip install failed: %s", result.stderr[-500:])
            return 1
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("pip install failed: %s", exc)
        return 1
    finally:
        try:
            os.remove(tarball)
        except OSError:
            pass

    log.info("updated to %s", latest)
    if restart:
        # Config and token stay in place; only the code changed. The
        # restart is detached so it works both from the CLI and from
        # inside the running daemon itself.
        if sys.platform == "win32":
            _detached(f"Restart-Service {SERVICE_NAME}")
        else:
            _detached(f"sleep 1; systemctl restart {SERVICE_NAME}")
        log.info("service restart scheduled")
    return 0


def token_present() -> bool:
    return os.path.isfile(DEFAULT_TOKEN_FILE)


def print_update_state(server: str, verify_tls: bool = True) -> int:
    """CLI helper for ``oo-agent update --check``."""
    manifest = check_manifest(server, verify_tls)
    if manifest is None:
        print("manifest unavailable")
        return 1
    latest = str(manifest["agent_version"])
    if _version_tuple(latest) <= _version_tuple(__version__):
        print(f"up to date ({__version__})")
    else:
        print(f"update available: {__version__} -> {latest}")
    return 0


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_manifest(tarball: str, version: str, base_url: str) -> str:
    """Build the /dl/agent.json content for a release tarball (used by
    the release tooling, kept here so the format lives in one place)."""
    return json.dumps({
        "agent_version": version,
        "url": base_url.rstrip("/") + "/dl/oo-agent.tar.gz",
        "sha256": _sha256_file(tarball),
    }, indent=2)
