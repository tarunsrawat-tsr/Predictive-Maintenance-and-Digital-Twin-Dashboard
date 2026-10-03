"""SQLite adapter used by unit tests and the single-process demo mode.

It implements the exact same interface as the DynamoDB adapter so the scorer and dashboard
code paths are identical in both environments.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from pdm.storage.base import Alert, MachineState, TelemetryItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    machine_id TEXT NOT NULL, ts INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (machine_id, ts)
);
CREATE TABLE IF NOT EXISTS machine_state (
    machine_id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    machine_id TEXT NOT NULL, ts INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (machine_id, ts)
);
CREATE INDEX IF NOT EXISTS alerts_status_ts ON alerts(status, ts);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ telemetry
    def put_telemetry_batch(self, items: list[TelemetryItem]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO telemetry(machine_id, ts, body) VALUES (?, ?, ?)",
                [(i["machine_id"], int(i["ts"]), json.dumps(i)) for i in items],
            )
            self._conn.commit()

    def get_telemetry(self, machine_id: str, limit: int = 300) -> list[TelemetryItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM telemetry WHERE machine_id=? ORDER BY ts DESC LIMIT ?", (machine_id, limit)
            ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    # ------------------------------------------------------------------ machine state
    def upsert_machine_state(self, state: MachineState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO machine_state(machine_id, body) VALUES (?, ?)",
                (state["machine_id"], json.dumps(state)),
            )
            self._conn.commit()

    def get_machine_state(self, machine_id: str) -> MachineState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM machine_state WHERE machine_id=?", (machine_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_machine_states(self) -> list[MachineState]:
        with self._lock:
            rows = self._conn.execute("SELECT body FROM machine_state ORDER BY machine_id").fetchall()
        return [json.loads(r[0]) for r in rows]

    # ------------------------------------------------------------------ alerts
    def put_alert(self, alert: Alert) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alerts(machine_id, ts, status, body) VALUES (?, ?, ?, ?)",
                (alert["machine_id"], int(alert["ts"]), alert.get("status", "open"), json.dumps(alert)),
            )
            self._conn.commit()

    def list_alerts(self, status: str | None = None, limit: int = 200) -> list[Alert]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT body FROM alerts WHERE status=? ORDER BY ts DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT body FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def ack_alert(self, machine_id: str, ts: int, user: str = "operator") -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM alerts WHERE machine_id=? AND ts=?", (machine_id, int(ts))
            ).fetchone()
            if not row:
                return
            body = json.loads(row[0])
            body.update(status="acknowledged", acknowledged_by=user, acknowledged_at=int(time.time() * 1000))
            self._conn.execute(
                "UPDATE alerts SET status=?, body=? WHERE machine_id=? AND ts=?",
                ("acknowledged", json.dumps(body), machine_id, int(ts)),
            )
            self._conn.commit()

    def reset(self) -> None:
        with self._lock:
            for t in ("telemetry", "machine_state", "alerts"):
                self._conn.execute(f"DELETE FROM {t}")
            self._conn.commit()
