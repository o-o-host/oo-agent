"""Agent enrollment: obtain the one-time agent token from the backend.

Two equal flows, both finishing with a single token hand-off:

* IP claim — the operator declares the server's public IP in the UI
  first; the agent then calls ``POST /agent/v1/enroll`` with no code
  and the backend matches the request by its source IP.
* Enroll code — for NAT / shared egress where source-IP matching is
  impossible, the UI issues a short-lived one-time code that is passed
  on the command line (``oo-agent --enroll AB7-KQ2-9FD``).

Either way the operator confirms the new agent in the UI (hostname and
machine fingerprint are shown), the agent polls
``GET /agent/v1/enroll/<id>`` until the request is resolved, receives
the token exactly once and stores it with 0600 permissions.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from oo_agent.core.hostinfo import agent_info
from oo_agent.transport.client import TransportError, _import_httpx, write_token

log = logging.getLogger("enroll")

ENROLL_PATH = "/agent/v1/enroll"


def _request_json(response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def enroll(
    server: str,
    code: str = "",
    token_file: str = "",
    poll_seconds: float = 5.0,
    timeout_minutes: float = 20.0,
    verify_tls: bool = True,
    http_transport: Any | None = None,
) -> str:
    """Run the enroll flow and return the agent token.

    Blocks until the operator confirms the agent in the UI, the
    request is rejected, or ``timeout_minutes`` passes. The token is
    also written to ``token_file`` when a path is given.
    """
    httpx = _import_httpx()
    server = server.rstrip("/")
    body: dict[str, Any] = {"agent": agent_info()}
    if code:
        body["enroll_code"] = code

    with httpx.Client(
        timeout=15, verify=verify_tls, transport=http_transport
    ) as client:
        response = client.post(server + ENROLL_PATH, json=body)
        if response.status_code >= 400:
            raise TransportError(
                f"enroll rejected (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        data = _request_json(response)
        token = str(data.get("token", ""))
        enroll_id = str(data.get("enroll_id", "") or data.get("id", ""))
        if not token and not enroll_id:
            raise TransportError("enroll response carries no token or id")

        deadline = time.monotonic() + timeout_minutes * 60
        while not token:
            if time.monotonic() > deadline:
                raise TransportError("enroll timed out — not confirmed in time")
            log.info("waiting for confirmation in the UI...")
            time.sleep(poll_seconds)
            response = client.get(f"{server}{ENROLL_PATH}/{enroll_id}")
            if response.status_code >= 400:
                raise TransportError(
                    f"enroll poll failed (HTTP {response.status_code})"
                )
            data = _request_json(response)
            status = str(data.get("status", "pending"))
            if status in ("rejected", "expired"):
                raise TransportError(f"enroll {status}")
            token = str(data.get("token", ""))

    if token_file:
        write_token(token_file, token)
        log.info("agent token stored in %s", token_file)
    return token
