"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys

from oo_agent import __version__
from oo_agent.core.config import AgentConfig
from oo_agent.core.log import setup_logging
from oo_agent.core.registry import discover
from oo_agent.core.scheduler import Scheduler

log = logging.getLogger("agent")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oo-agent",
        description="Lightweight metrics collection agent.",
    )
    parser.add_argument("--config", metavar="PATH", help="path to agent.ini")
    parser.add_argument(
        "--once", action="store_true", help="single collection pass, then exit"
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="print the collected payload as JSON to stdout (implies --once)",
    )
    parser.add_argument(
        "--list-collectors",
        action="store_true",
        help="show discovered collectors and exit",
    )
    parser.add_argument(
        "--enroll",
        nargs="?",
        const="",
        metavar="CODE",
        help="enroll this agent against the backend and store the token; "
        "pass the one-time code shown in the UI, or omit it for the "
        "IP-claim flow",
    )
    parser.add_argument(
        "--server",
        metavar="URL",
        help="backend URL (overrides 'server' in the [transport] section)",
    )
    parser.add_argument("--log-level", metavar="LEVEL", help="override log level")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = AgentConfig(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    setup_logging(args.log_level or str(config.agent["log_level"]))
    if config.path:
        log.info("config: %s", config.path)

    transport_options = dict(config.transport)
    if args.server:
        transport_options["server"] = args.server

    if args.enroll is not None:
        return _do_enroll(transport_options, args.enroll)

    entries = discover(config)
    scheduler = Scheduler(entries)

    if args.list_collectors:
        for entry in entries:
            kind = "plugin" if entry.custom else "built-in"
            print(f"{entry.name:<16} {kind:<9} interval={entry.interval}s")
        if not entries:
            print("no collectors discovered")
        return 0

    if args.dump or args.once:
        payload = scheduler.run_once()
        if args.dump:
            json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
            print()
        return 0

    sink = _build_sink(transport_options)
    log.info("oo-agent %s starting (%d collectors)", __version__, len(entries))
    try:
        scheduler.run_forever(sink)
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


def run_daemon(config_path: str | None = None, should_stop=None) -> None:
    """Full daemon lifecycle for service wrappers (systemd is fine with
    main(), the Windows service needs a stop callback)."""
    config = AgentConfig(config_path)
    setup_logging(str(config.agent["log_level"]))
    entries = discover(config)
    scheduler = Scheduler(entries)
    sink = _build_sink(dict(config.transport))
    log.info("oo-agent %s starting (%d collectors)", __version__, len(entries))
    scheduler.run_forever(sink, should_stop)
    log.info("stopped")


def _do_enroll(transport_options: dict, code: str) -> int:
    from oo_agent.transport import TransportError, enroll
    from oo_agent.transport.client import DEFAULT_TOKEN_FILE

    server = str(transport_options.get("server", ""))
    if not server:
        print(
            "error: no backend URL — pass --server or set 'server' in the "
            "[transport] config section",
            file=sys.stderr,
        )
        return 2
    token_file = str(transport_options.get("token_file", "") or DEFAULT_TOKEN_FILE)
    try:
        enroll(
            server,
            code=code,
            token_file=token_file,
            verify_tls=bool(transport_options.get("verify_tls", True)),
        )
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"enrolled — token stored in {token_file}")
    return 0


def _build_sink(transport_options: dict):
    """Daemon sink: push to the backend when transport is configured,
    buffering through the on-disk queue; otherwise log payload sizes."""

    def log_sink(payload: dict) -> None:
        log.info(
            "collected: %d metrics, %d sensors",
            len(payload["metrics"]),
            len(payload.get("sensors", [])),
        )

    if not transport_options.get("server"):
        log.info("no [transport] server configured — running in log-only mode")
        return log_sink

    from oo_agent.buffer.queue import DiskQueue
    from oo_agent.transport import TransportClient, drain
    from oo_agent.transport.client import DEFAULT_QUEUE_PATH

    client = TransportClient(transport_options)
    if not client.configured:
        log.warning(
            "transport: no agent token (run 'oo-agent --enroll' first) — "
            "running in log-only mode"
        )
        return log_sink

    queue = None
    queue_path = str(transport_options.get("queue_path", "") or DEFAULT_QUEUE_PATH)
    try:
        queue = DiskQueue(
            queue_path,
            max_rows=int(transport_options.get("queue_max_rows", 5000)),
            max_age_hours=float(transport_options.get("queue_max_age_hours", 24)),
        )
        backlog = len(queue)
        if backlog:
            log.info("queue: %d buffered payloads pending at %s", backlog, queue_path)
    except (OSError, sqlite3.Error) as exc:
        log.warning("queue unavailable (%s) — offline buffering off", exc)

    def push_sink(payload: dict) -> None:
        if client.push(payload):
            if queue is not None and len(queue):
                sent = drain(queue, client)
                if sent:
                    log.info("queue: %d buffered payloads delivered", sent)
        elif queue is not None:
            queue.push(payload)

    return push_sink
