"""Append-only audit log.

Every capability invocation is recorded, allowed or denied.  Denials matter most
-- an agent probing slots it does not hold is the signal you want -- so they are
logged with the same weight as successes rather than being dropped.

SQLite because it is in the standard library, survives a daemon restart, and is
queryable from the web UI without an index of our own.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    actor    TEXT    NOT NULL,
    op       TEXT    NOT NULL,
    target   TEXT,
    slot     INTEGER,
    rights   TEXT,
    allowed  INTEGER NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts    ON audit(ts);
CREATE INDEX IF NOT EXISTS audit_actor ON audit(actor, ts);
CREATE INDEX IF NOT EXISTS audit_denied ON audit(allowed, ts);
"""


class AuditLog:
    """Thread-safe writer for the audit table."""

    def __init__(self, path: Path | str | None = None) -> None:
        # ":memory:" for tests; a real path for the daemon.
        self.path = str(path) if path is not None else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def record(
        self,
        actor: str,
        op: str,
        *,
        allowed: bool,
        target: str | None = None,
        slot: int | None = None,
        rights: str | None = None,
        detail: Any = None,
    ) -> None:
        payload = (
            detail if detail is None or isinstance(detail, str) else json.dumps(detail)
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (ts, actor, op, target, slot, rights, allowed, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), actor, op, target, slot, rights, int(allowed), payload),
            )
            self._db.commit()

    def tail(
        self, limit: int = 100, actor: str | None = None, denied_only: bool = False
    ) -> list[dict]:
        """Most recent entries, newest first."""
        query = "SELECT * FROM audit"
        clauses: list[str] = []
        params: list[Any] = []
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if denied_only:
            clauses.append("allowed = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows: Iterable[sqlite3.Row] = self._db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
