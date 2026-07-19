"""Non-blocking re-enrollment for a running daemon.

When the backend starts rejecting the agent token (401/403) or reports
the bound resource gone (410), the daemon wipes the token and switches
to this flow: one cheap HTTP call per push cycle, throttled, until a
fresh claim created in the UI is matched and confirmed. The claim is
matched by source IP, or — for the "deleted the server, added it
again" cycle — by the machine fingerprint the backend already knows.

Unlike :func:`oo_agent.transport.enroll.enroll` this never blocks the
collection loop; metrics keep being gathered (and dropped) while the
operator re-adds the server in the UI.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from oo_agent.core.hostinfo import agent_info
from oo_agent.transport.client import _import_httpx, write_token

log = logging.getLogger("reclaim")

ENROLL_PATH = "/agent/v1/enroll"
_MIN_INTERVAL = 60.0  # seconds between HTTP attempts


class ReclaimFlow:
    """State machine: POST /enroll until a claim matches, then poll
    GET /enroll/<id> until a token is issued. ``step()`` performs at
    most one HTTP request and returns the new token once obtained."""

    def __init__(
        self,
        server: str,
        token_file: str,
        verify_tls: bool = True,
        min_interval: float = _MIN_INTERVAL,
        http_transport: Any | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.token_file = token_file
        self.verify = verify_tls
        self.min_interval = min_interval
        self._http_transport = http_transport
        self._http = None
        self._enroll_id = ""
        self._next_try = 0.0
        self._waiting_logged = False

    def _client(self):
        if self._http is None:
            httpx = _import_httpx()
            self._http = httpx.Client(
                timeout=15, verify=self.verify, transport=self._http_transport
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _json(self, response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def step(self) -> str | None:
        """Advance the flow by at most one HTTP call; returns the new
        token when enrollment completes, None otherwise."""
        now = time.monotonic()
        if now < self._next_try:
            return None
        self._next_try = now + self.min_interval
        try:
            if not self._enroll_id:
                response = self._client().post(
                    self.server + ENROLL_PATH, json={"agent": agent_info()}
                )
                if response.status_code == 404:
                    # No claim for this host yet — the operator has not
                    # re-added the server in the UI. Keep waiting.
                    if not self._waiting_logged:
                        log.info(
                            "re-enroll: no claim for this host yet — "
                            "add the server in the UI to reconnect"
                        )
                        self._waiting_logged = True
                    return None
                if response.status_code >= 400:
                    log.warning(
                        "re-enroll rejected (HTTP %d): %s",
                        response.status_code, response.text[:200],
                    )
                    return None
                self._enroll_id = str(self._json(response).get("enroll_id", ""))
                self._waiting_logged = False
                return None

            response = self._client().get(
                f"{self.server}{ENROLL_PATH}/{self._enroll_id}"
            )
            if response.status_code >= 400:
                self._enroll_id = ""
                return None
            data = self._json(response)
            status = str(data.get("status", "pending"))
            if status in ("rejected", "expired"):
                log.info("re-enroll %s — starting over", status)
                self._enroll_id = ""
                return None
            token = str(data.get("token", ""))
            if not token:
                return None
        except Exception as exc:  # noqa: BLE001 - network errors vary
            log.debug("re-enroll attempt failed: %s", exc)
            return None
        if self.token_file:
            write_token(self.token_file, token)
        log.info("re-enrolled — new agent token stored")
        return token
