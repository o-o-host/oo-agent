"""HTTPS client: gzip-compressed metric pushes with bounded retries.

The agent is strictly outbound: one endpoint, one bearer token. A
payload that cannot be delivered because of a transient problem
(connection error, timeout, 5xx) is reported back to the caller,
which parks it in the on-disk queue; :func:`drain` re-sends queued
payloads oldest-first once connectivity returns. Permanent rejections
(4xx) are logged and dropped — re-sending the same body cannot
succeed and would jam the queue.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import time
from typing import Any

from oo_agent import __version__

log = logging.getLogger("transport")

METRICS_PATH = "/agent/v1/metrics"

_ATTEMPTS = 2  # tries per payload before it is considered undelivered
_RETRY_DELAY = 2.0  # seconds between the tries

if sys.platform == "win32":
    _STATE_DIR = os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"), "oo-agent"
    )
    DEFAULT_TOKEN_FILE = os.path.join(_STATE_DIR, "agent.token")
    DEFAULT_QUEUE_PATH = os.path.join(_STATE_DIR, "queue.db")
else:
    DEFAULT_TOKEN_FILE = "/etc/oo-agent/agent.token"
    DEFAULT_QUEUE_PATH = "/var/lib/oo-agent/queue.db"


class TransportError(RuntimeError):
    """Setup problem (missing dependency, bad config) — not a delivery
    failure; delivery failures are ordinary return values."""


def _import_httpx():
    try:
        import httpx
    except ImportError as exc:
        raise TransportError(
            "httpx is required for transport: pip install oo-agent[transport]"
        ) from exc
    return httpx


def read_token(token_file: str) -> str:
    try:
        with open(token_file, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_token(token_file: str, token: str) -> None:
    """Store the agent token with owner-only permissions."""
    directory = os.path.dirname(token_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(token + "\n")


class TransportClient:
    """Pushes payloads to ``POST <server>/agent/v1/metrics``."""

    def __init__(
        self, options: dict[str, Any], http_transport: Any | None = None
    ) -> None:
        self.server = str(options.get("server", "")).rstrip("/")
        self.token_file = str(options.get("token_file", "") or DEFAULT_TOKEN_FILE)
        self.token = str(options.get("token", "") or read_token(self.token_file))
        self.timeout = float(options.get("timeout", 10))
        self.verify = bool(options.get("verify_tls", True))
        self._http_transport = http_transport  # test seam (httpx.MockTransport)
        self._http = None
        #: HTTP status of the last authorization rejection (401/403/410),
        #: None after any accepted push. The daemon watches this to switch
        #: into re-enrollment when the token has been invalidated.
        self.auth_status: int | None = None
        #: Backend instructions from the last accepted push response
        #: (e.g. ``{"update": true}``); {} when the body carried none.
        self.server_commands: dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        return bool(self.server and self.token)

    def _client(self):
        if self._http is None:
            httpx = _import_httpx()
            self._http = httpx.Client(
                timeout=self.timeout,
                verify=self.verify,
                transport=self._http_transport,
            )
        return self._http

    def push(self, payload: dict[str, Any]) -> bool:
        """Deliver one payload.

        Returns True when the caller is done with the payload — it was
        accepted, or permanently rejected (logged and dropped). Returns
        False on transient failures: the caller should queue the
        payload and retry later via :func:`drain`.
        """
        body = gzip.compress(json.dumps(payload).encode("utf-8"))
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "User-Agent": f"oo-agent/{__version__}",
        }
        error = "not attempted"
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                response = self._client().post(
                    self.server + METRICS_PATH, content=body, headers=headers
                )
            except TransportError:
                raise
            except Exception as exc:  # noqa: BLE001 - network errors vary
                error = str(exc) or exc.__class__.__name__
            else:
                if response.status_code < 300:
                    self.auth_status = None
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    self.server_commands = data if isinstance(data, dict) else {}
                    return True
                if 400 <= response.status_code < 500:
                    # The same body cannot succeed later — drop it. An
                    # invalidated token (401/403) or a deleted resource
                    # (410) is remembered so the daemon can re-enroll.
                    if response.status_code in (401, 403, 410):
                        self.auth_status = response.status_code
                    log.warning(
                        "metrics rejected (HTTP %d): %s",
                        response.status_code,
                        response.text[:200],
                    )
                    return True
                error = f"HTTP {response.status_code}"
            if attempt < _ATTEMPTS:
                time.sleep(_RETRY_DELAY)
        log.info("delivery failed (%s)", error)
        return False

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


def drain(queue, client: TransportClient, batch: int = 50) -> int:
    """Re-send queued payloads oldest-first; stops at the first
    transient failure so ordering is preserved. Returns the number of
    payloads taken off the queue."""
    done_total = 0
    while True:
        rows = queue.peek(batch)
        if not rows:
            return done_total
        delivered: list[int] = []
        for row_id, payload in rows:
            if not client.push(payload):
                break
            delivered.append(row_id)
        queue.ack(delivered)
        done_total += len(delivered)
        if len(delivered) < len(rows):
            return done_total
