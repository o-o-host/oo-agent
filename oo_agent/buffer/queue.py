"""SQLite-backed payload queue.

When the endpoint is unreachable the agent keeps collecting; payloads
queue up on disk and are drained in order once connectivity returns.
Age and row caps keep the queue bounded on long outages (oldest rows
are dropped first).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

log = logging.getLogger("buffer")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


class DiskQueue:
    def __init__(
        self,
        path: str,
        max_rows: int = 5000,
        max_age_hours: float = 24.0,
    ) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self.max_rows = max_rows
        self.max_age = max_age_hours * 3600

    def push(self, payload: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO queue (ts, payload) VALUES (?, ?)",
            (payload.get("ts", int(time.time())), json.dumps(payload)),
        )
        self._trim()
        self._db.commit()

    def peek(self, limit: int = 50) -> list[tuple[int, dict[str, Any]]]:
        """Oldest queued payloads as (row_id, payload) pairs."""
        rows = self._db.execute(
            "SELECT id, payload FROM queue ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [(row_id, json.loads(text)) for row_id, text in rows]

    def ack(self, row_ids: list[int]) -> None:
        """Delete rows that were delivered successfully."""
        if not row_ids:
            return
        self._db.executemany(
            "DELETE FROM queue WHERE id = ?", [(i,) for i in row_ids]
        )
        self._db.commit()

    def __len__(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def _trim(self) -> None:
        cutoff = int(time.time() - self.max_age)
        aged = self._db.execute(
            "DELETE FROM queue WHERE ts < ?", (cutoff,)
        ).rowcount
        over = len(self) - self.max_rows
        dropped = 0
        if over > 0:
            dropped = self._db.execute(
                "DELETE FROM queue WHERE id IN "
                "(SELECT id FROM queue ORDER BY id LIMIT ?)",
                (over,),
            ).rowcount
        if aged or dropped:
            log.warning("queue trimmed: %d aged, %d overflow", aged, dropped)

    def close(self) -> None:
        self._db.close()
