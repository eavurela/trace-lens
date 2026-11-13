"""SQLite: los hallazgos, abiertos y cerrados. Uno abierto por (tipo, servicio, endpoint)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    span_kind TEXT NOT NULL DEFAULT 'server',
    service TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL,
    value REAL NOT NULL,
    baseline REAL NOT NULL,
    new INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    detected_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    resolved_at REAL,
    explained_at REAL,
    causes TEXT NOT NULL DEFAULT '[]',
    deploy TEXT,
    codes TEXT NOT NULL DEFAULT '{}',
    focus TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS insights_state ON insights (state, last_seen);
CREATE INDEX IF NOT EXISTS insights_key ON insights (key, state);
"""

JSON_FIELDS = ("causes", "deploy", "codes", "focus")
#: Columnas agregadas después de la primera versión (bases ya creadas).
MIGRATIONS = {
    "codes": "ALTER TABLE insights ADD COLUMN codes TEXT NOT NULL DEFAULT '{}'",
    "focus": "ALTER TABLE insights ADD COLUMN focus TEXT NOT NULL DEFAULT '[]'",
    "deploy_change": "ALTER TABLE insights ADD COLUMN deploy_change TEXT NOT NULL DEFAULT ''",
}


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(insights)")}
            for col, sql in MIGRATIONS.items():
                if col not in cols:
                    self._conn.execute(sql)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def active(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM insights WHERE key=? AND state='active' ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
        return _row(row) if row else None

    def save(self, data: dict[str, Any]) -> None:
        """Inserta o actualiza el hallazgo abierto de esa clave."""
        values = {k: (json.dumps(v) if k in JSON_FIELDS else v) for k, v in data.items()}
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM insights WHERE key=? AND state='active'", (values["key"],)
            ).fetchone()
            if row:
                sets = ", ".join(f"{k}=?" for k in values if k not in ("key", "detected_at"))
                params = [v for k, v in values.items() if k not in ("key", "detected_at")]
                self._conn.execute(f"UPDATE insights SET {sets} WHERE id=?", (*params, row["id"]))
            else:
                cols = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                self._conn.execute(
                    f"INSERT INTO insights ({cols}, state) VALUES ({marks}, 'active')", tuple(values.values())
                )

    def resolve_stale(self, seen: set[str], now: float, after: float) -> int:
        """Cierra los abiertos que no aparecieron en esta corrida y hace ``after`` que no se ven."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, key, last_seen FROM insights WHERE state='active'"
            ).fetchall()
            stale = [r["id"] for r in rows if r["key"] not in seen and now - r["last_seen"] >= after]
            for i in stale:
                self._conn.execute("UPDATE insights SET state='resolved', resolved_at=? WHERE id=?", (now, i))
        return len(stale)

    def insights(
        self, state: str = "active", service: str | None = None, since: float | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if state != "all":
            where.append("state=?")
            params.append(state)
        if service:
            where.append("service=?")
            params.append(service)
        if since is not None:
            where.append("last_seen>=?")
            params.append(since)
        sql = "SELECT * FROM insights"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY state='active' DESC, CASE severity WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,"
            " detected_at DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) n FROM insights GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def prune(self, before: float) -> int:
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM insights WHERE state='resolved' AND resolved_at < ?", (before,)
            ).rowcount


def _row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["causes"] = json.loads(d["causes"] or "[]")
    d["deploy"] = json.loads(d["deploy"]) if d.get("deploy") else None
    d["codes"] = json.loads(d.get("codes") or "{}")
    d["focus"] = json.loads(d.get("focus") or "[]")
    d["new"] = bool(d["new"])
    # Lo que el front necesita para abrir la búsqueda de trazas de este hallazgo.
    d["search"] = {
        "service": d["service"],
        "endpoint": d["endpoint"],
        "errorsOnly": d["kind"] == "error_rate",
        #: Para abrir la búsqueda filtrada por código (4xx).
        "status": "4xx" if d["kind"] == "http_4xx" else "",
        "since": d["started_at"] or d["detected_at"],
    }
    return d
