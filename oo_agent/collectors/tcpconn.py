"""TCP connection state counters (DDoS heuristic feed).

Linux: parsed directly from /proc/net/tcp{,6} — no root needed and far
cheaper than enumerating sockets through psutil. Other platforms fall
back to ``psutil.net_connections``.
"""

from __future__ import annotations

import sys
from typing import Any

from oo_agent.plugin import Collector

# Kernel TCP state codes (include/net/tcp_states.h).
_ESTABLISHED = "01"
_SYN_RECV = "03"
_PROC_FILES = ("/proc/net/tcp", "/proc/net/tcp6")


def _count_proc() -> tuple[int, int]:
    established = syn_recv = 0
    for path in _PROC_FILES:
        try:
            with open(path, encoding="ascii") as fh:
                next(fh, None)  # header line
                for line in fh:
                    fields = line.split()
                    if len(fields) < 4:
                        continue
                    state = fields[3]
                    if state == _ESTABLISHED:
                        established += 1
                    elif state == _SYN_RECV:
                        syn_recv += 1
        except OSError:
            continue
    return established, syn_recv


def _count_psutil() -> tuple[int, int]:
    import psutil

    established = syn_recv = 0
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == psutil.CONN_ESTABLISHED:
            established += 1
        elif conn.status == psutil.CONN_SYN_RECV:
            syn_recv += 1
    return established, syn_recv


class TcpConnCollector(Collector):
    name = "tcpconn"

    def collect(self) -> dict[str, Any]:
        if sys.platform.startswith("linux"):
            established, syn_recv = _count_proc()
        else:
            established, syn_recv = _count_psutil()
        return {
            "metrics": {
                "net.tcp.established": established,
                "net.tcp.synrecv": syn_recv,
            }
        }
