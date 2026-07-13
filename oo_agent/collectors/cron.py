"""Scheduled job inventory and status tracking (Linux).

Sources, most to least authoritative:

- systemd timers — the only source with exact results: last/next run,
  success/failure and the exit code of the activated service;
- ``/etc/crontab`` and ``/etc/cron.d/*`` (system format, user column);
- per-user spool crontabs (root only); without root the agent still
  reports the invoking user's own crontab via ``crontab -l``;
- ``/etc/cron.{hourly,daily,weekly,monthly}/`` drop-in scripts.

Classic cron records only that a job *started* (journal/syslog ``CMD``
lines), never its exit status, so crontab entries get ``last_status``
of ``started``/``unknown``. Jobs that need real success tracking should
run as systemd timers.

Everything lands in the inventory snapshot (``cron_jobs`` list) plus
two aggregate metrics: ``cron.jobs`` and ``cron.failed``.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import os
import pwd
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from oo_agent.plugin import Collector

log = logging.getLogger("collector.cron")

_SPOOL_DIRS = ("/var/spool/cron/crontabs", "/var/spool/cron")
_CRON_PERIODS = ("hourly", "daily", "weekly", "monthly")
_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
# journalctl -o short-iso:  2026-07-12T16:45:01+0300 host CRON[1]: (root) CMD (cmd)
_JOURNAL_CMD = re.compile(
    r"^(\S+)\s+\S+\s+CRON\w*\[\d+\]:\s+\((\S+)\)\s+CMD\s+\((.*)\)\s*$"
)
_TIMER_SPEC = re.compile(
    r"(OnCalendar|OnUnitActiveSec|OnUnitInactiveSec|OnBootSec|OnStartupSec|"
    r"OnActiveSec)=([^;}]+)"
)

_REDIRECT = re.compile(r"(?:^|\s)\d?>>?\s*(\S+)")
# Tokens that wrap the actual job: interpreters, schedulers, env tweaks.
_WRAPPERS = frozenset(
    {
        "env", "nice", "ionice", "flock", "timeout", "chronic", "cd",
        "bash", "sh", "zsh", "dash", "node", "php", "perl", "ruby",
        "test", "command", "[",
    }
)
_KEYWORD_SPECS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTH_NAMES = {
    name: i + 1
    for i, name in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split()
    )
}
_DOW_NAMES = {
    name: i for i, name in enumerate("sun mon tue wed thu fri sat".split())
}

_TIMER_PROPS = (
    "Id,Unit,UnitFileState,NextElapseUSecRealtime,LastTriggerUSec,"
    "TimersCalendar,TimersMonotonic"
)
_SERVICE_PROPS = "Id,Result,ExecMainStatus,ExecMainStartTimestamp"


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("%s failed: %s", cmd[0], exc)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def parse_crontab(text: str, default_user: str | None = None) -> list[dict]:
    """Parse crontab text; ``default_user=None`` means the system format
    with a user column (``/etc/crontab``, ``/etc/cron.d``)."""
    jobs: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _ENV_LINE.match(line):
            continue
        if line.startswith("@"):
            parts = line.split(None, 2 if default_user is None else 1)
            if default_user is None:
                if len(parts) < 3:
                    continue
                schedule, user, command = parts
            else:
                if len(parts) < 2:
                    continue
                schedule, command = parts
                user = default_user
        else:
            fields = 6 if default_user is None else 5
            parts = line.split(None, fields)
            if len(parts) <= fields:
                continue
            schedule = " ".join(parts[:5])
            user = parts[5] if default_user is None else default_user
            command = parts[fields]
        jobs.append(
            {"schedule": schedule, "user": user, "command": command.strip()}
        )
    return jobs


def parse_show_blocks(text: str) -> list[dict[str, str]]:
    """Split multi-unit ``systemctl show`` output into per-unit dicts."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition("=")
        if sep:
            current[key] = value
    if current:
        blocks.append(current)
    return blocks


def parse_systemd_timestamp(value: str) -> float | None:
    """Epoch seconds from a ``systemctl show`` timestamp string
    (``Sun 2026-07-12 16:50:04 UTC``, ``@1752332004`` or empty/n-a)."""
    value = (value or "").strip()
    if not value or value in ("n/a", "0"):
        return None
    if value.startswith("@"):
        try:
            return float(value[1:])
        except ValueError:
            return None
    fields = value.split()
    if len(fields) < 3:
        return None
    try:
        st = time.strptime(f"{fields[1]} {fields[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    tz = fields[3] if len(fields) > 3 else ""
    if tz == "UTC":
        return float(calendar.timegm(st))
    return time.mktime(st)  # systemctl prints local time otherwise


def parse_journal_starts(text: str) -> dict[tuple[str, str], float]:
    """Last start time per (user, command) from ``journalctl -t CRON``."""
    starts: dict[tuple[str, str], float] = {}
    for line in text.splitlines():
        match = _JOURNAL_CMD.match(line)
        if not match:
            continue
        ts_raw, user, command = match.groups()
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            continue
        key = (user, command.strip())
        if ts > starts.get(key, 0.0):
            starts[key] = ts
    return starts


def job_name(command: str) -> str:
    """Best-effort display name: the script/binary the job actually runs,
    skipping interpreters and wrapper commands."""
    stripped = _REDIRECT.sub(" ", command)
    for segment in re.split(r"&&|\|\||[;|]", stripped):
        for token in segment.split():
            if "=" in token and not token.startswith("/"):
                continue  # env assignment
            if token.startswith("-"):
                continue  # option of a wrapper/interpreter
            base = os.path.basename(token.rstrip("&"))
            if not base or base in _WRAPPERS:
                continue
            if re.fullmatch(r"python[\d.]*", base):
                continue
            return base
    return command.strip()[:60]


def log_file(command: str) -> str | None:
    """Output redirect target of the command, when it logs to a file."""
    for match in _REDIRECT.finditer(command):
        target = match.group(1).rstrip(";&|")
        if not target or target.startswith("&") or target == "/dev/null":
            continue
        return target
    return None


def _parse_field(
    spec: str, lo: int, hi: int, names: dict[str, int] | None = None
) -> set[int] | None:
    """One crontab field into a value set; ``None`` on a parse error."""
    values: set[int] = set()

    def resolve(token: str) -> int:
        if names and token.lower() in names:
            return names[token.lower()]
        return int(token)

    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_raw = part.split("/", 1)
            step = int(step_raw) if step_raw.isdigit() and int(step_raw) else -1
        if step < 0:
            return None
        try:
            if part in ("*", ""):
                rng = range(lo, hi + 1)
            elif "-" in part:
                first, last = part.split("-", 1)
                rng = range(resolve(first), resolve(last) + 1)
            elif step > 1:
                rng = range(resolve(part), hi + 1)
            else:
                values.add(resolve(part))
                continue
        except (ValueError, KeyError):
            return None
        values.update(range(rng.start, rng.stop, step))
    if not values or min(values) < lo or max(values) > hi:
        return None
    return values


def cron_next_run(schedule: str, now: float) -> float | None:
    """Next trigger time (epoch, local timezone) of a 5-field cron
    schedule or an ``@keyword``; ``None`` when it cannot be computed."""
    schedule = _KEYWORD_SPECS.get(schedule, schedule)
    fields = schedule.split()
    if len(fields) != 5:
        return None  # @reboot and anything unparseable
    minutes = _parse_field(fields[0], 0, 59)
    hours = _parse_field(fields[1], 0, 23)
    doms = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12, _MONTH_NAMES)
    dows = _parse_field(fields[4], 0, 7, _DOW_NAMES)
    if None in (minutes, hours, doms, months, dows):
        return None
    dows = {d % 7 for d in dows}  # both 0 and 7 mean Sunday
    # Vixie rule: when both day fields are restricted, either matches.
    dom_star = fields[2].startswith("*")
    dow_star = fields[4].startswith("*")

    from datetime import datetime, timedelta

    start = datetime.fromtimestamp(now + 60).replace(second=0, microsecond=0)
    day = start.date()
    for _ in range(366 * 2):
        if day.month in months:
            dom_ok = day.day in doms
            dow_ok = (day.weekday() + 1) % 7 in dows
            if (
                (dom_star and dow_star)
                or (dom_star and dow_ok)
                or (dow_star and dom_ok)
                or (not dom_star and not dow_star and (dom_ok or dow_ok))
            ):
                for hour in sorted(hours):
                    for minute in sorted(minutes):
                        candidate = datetime(
                            day.year, day.month, day.day, hour, minute
                        )
                        if candidate >= start:
                            return candidate.timestamp()
        day = day + timedelta(days=1)
    return None


def _timer_schedule(info: dict[str, str]) -> str:
    specs = []
    for prop in ("TimersCalendar", "TimersMonotonic"):
        for match in _TIMER_SPEC.finditer(info.get(prop, "")):
            specs.append(f"{match.group(1)}={match.group(2).strip()}")
    return "; ".join(specs)


def _job_id(source: str, user: str, schedule: str, command: str) -> str:
    raw = f"{source}|{user}|{schedule}|{command}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


class CronCollector(Collector):
    name = "cron"
    inventory = True

    def available(self) -> bool:
        return sys.platform.startswith("linux")

    # -- classic cron ---------------------------------------------------

    def _system_jobs(self) -> list[dict]:
        jobs: list[dict] = []
        try:
            with open("/etc/crontab", encoding="utf-8", errors="replace") as fh:
                for job in parse_crontab(fh.read()):
                    jobs.append({**job, "source": "system_crontab"})
        except OSError:
            pass
        try:
            names = sorted(os.listdir("/etc/cron.d"))
        except OSError:
            names = []
        for fname in names:
            if "." in fname:  # cron skips file names containing dots
                continue
            try:
                with open(
                    os.path.join("/etc/cron.d", fname),
                    encoding="utf-8",
                    errors="replace",
                ) as fh:
                    text = fh.read()
            except OSError:
                continue
            for job in parse_crontab(text):
                jobs.append({**job, "source": "cron_d", "file": fname})
        return jobs

    def _user_jobs(self) -> list[dict]:
        jobs: list[dict] = []
        if os.geteuid() == 0:
            # Root sees every user's spool crontab directly.
            for spool in _SPOOL_DIRS:
                try:
                    names = sorted(os.listdir(spool))
                except OSError:
                    continue
                for user in names:
                    path = os.path.join(spool, user)
                    if not os.path.isfile(path):
                        continue
                    try:
                        with open(
                            path, encoding="utf-8", errors="replace"
                        ) as fh:
                            text = fh.read()
                    except OSError:
                        continue
                    for job in parse_crontab(text, default_user=user):
                        jobs.append({**job, "source": "user_crontab"})
                if jobs:
                    break
            return jobs
        # Without root the spool is unreadable; report at least our own.
        text = _run(["crontab", "-l"])
        if text:
            user = pwd.getpwuid(os.getuid()).pw_name
            for job in parse_crontab(text, default_user=user):
                jobs.append({**job, "source": "user_crontab"})
        return jobs

    def _dir_jobs(self) -> list[dict]:
        jobs: list[dict] = []
        for period in _CRON_PERIODS:
            directory = f"/etc/cron.{period}"
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for fname in names:
                if fname.startswith(".") or "." in fname:
                    continue
                path = os.path.join(directory, fname)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    jobs.append(
                        {
                            "schedule": f"@{period}",
                            "user": "root",
                            "command": path,
                            "source": "cron_dir",
                        }
                    )
        return jobs

    def _journal_starts(self) -> dict[tuple[str, str], float]:
        window = float(self.config.get("journal_window_hours", 26))
        text = _run(
            [
                "journalctl",
                "-t", "CRON",
                "-t", "CROND",
                "-o", "short-iso",
                "--since", f"-{int(window)}h",
                "--no-pager",
                "-q",
            ],
            timeout=15.0,
        )
        return parse_journal_starts(text)

    # -- systemd timers -------------------------------------------------

    def _timer_jobs(self) -> list[dict]:
        listing = _run(
            [
                "systemctl", "list-units", "--type=timer", "--all",
                "--no-legend", "--plain", "--no-pager",
            ]
        )
        names = [
            line.split()[0]
            for line in listing.splitlines()
            if line.split() and line.split()[0].endswith(".timer")
        ]
        if not names:
            return []
        timers = parse_show_blocks(
            _run(["systemctl", "show", *names, "-p", _TIMER_PROPS])
        )
        services = {}
        service_names = [
            info.get("Unit") or info.get("Id", "").replace(".timer", ".service")
            for info in timers
        ]
        for info in parse_show_blocks(
            _run(["systemctl", "show", *service_names, "-p", _SERVICE_PROPS])
        ):
            services[info.get("Id", "")] = info

        jobs: list[dict] = []
        for info in timers:
            timer_id = info.get("Id", "")
            unit = info.get("Unit") or timer_id.replace(".timer", ".service")
            service = services.get(unit, {})
            last_run = parse_systemd_timestamp(
                service.get("ExecMainStartTimestamp", "")
            ) or parse_systemd_timestamp(info.get("LastTriggerUSec", ""))
            exit_status = service.get("ExecMainStatus", "")
            if last_run is None:
                status = "unknown"
            elif service.get("Result") == "success":
                status = "ok"
            else:
                status = "failed"
            jobs.append(
                {
                    "source": "systemd_timer",
                    "user": "root",
                    "schedule": _timer_schedule(info),
                    "command": unit,
                    "name": timer_id,
                    "enabled": info.get("UnitFileState", "")
                    not in ("disabled", "masked"),
                    "next_run": parse_systemd_timestamp(
                        info.get("NextElapseUSecRealtime", "")
                    ),
                    "last_run": last_run,
                    "last_status": status,
                    "exit_code": int(exit_status)
                    if exit_status.lstrip("-").isdigit() and last_run is not None
                    else None,
                }
            )
        return jobs

    # -- collection pass ------------------------------------------------

    def collect(self) -> dict[str, Any]:
        classic = self._system_jobs() + self._user_jobs() + self._dir_jobs()
        starts = self._journal_starts() if classic else {}
        now = time.time()
        for job in classic:
            last = starts.get((job["user"], job["command"]))
            job["last_run"] = last
            job["last_status"] = "started" if last else "unknown"
            job["name"] = job_name(job["command"])
            job["log_file"] = log_file(job["command"])
            job["next_run"] = cron_next_run(job["schedule"], now)
            job.setdefault("enabled", True)
            job.setdefault("exit_code", None)

        jobs = classic
        if self.config.get("timers", True) is not False:
            jobs = jobs + self._timer_jobs()
        for job in jobs:
            job["id"] = _job_id(
                job["source"], job["user"], job["schedule"], job["command"]
            )
        jobs.sort(key=lambda j: (j["source"], j["user"], j["command"]))

        failed = sum(1 for j in jobs if j.get("last_status") == "failed")
        return {
            "metrics": {"cron.jobs": len(jobs), "cron.failed": failed},
            "inventory": {"cron_jobs": jobs},
        }
