"""In-memory registry of live calls, with a SQLite record of finished ones."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import CallState

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    label TEXT,
    destination TEXT,
    goal TEXT,
    mode TEXT,
    phase TEXT,
    call_sid TEXT,
    started_at REAL,
    ended_at REAL,
    hold_seconds REAL,
    error TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS calls_started_at ON calls (started_at DESC);
"""


class CallStore:
    def __init__(self, database_path: str = "phone_agent.db"):
        self.path = Path(database_path)
        self._lock = threading.Lock()
        self._live: dict[str, CallState] = {}
        self._by_sid: dict[str, str] = {}
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- live calls --------------------------------------------------------

    def add(self, state: CallState) -> None:
        with self._lock:
            self._live[state.id] = state

    def get(self, call_id: str) -> CallState | None:
        with self._lock:
            return self._live.get(call_id)

    def get_by_sid(self, call_sid: str) -> CallState | None:
        with self._lock:
            call_id = self._by_sid.get(call_sid)
            return self._live.get(call_id) if call_id else None

    def index_sid(self, call_sid: str, call_id: str) -> None:
        with self._lock:
            self._by_sid[call_sid] = call_id

    def live(self) -> list[CallState]:
        with self._lock:
            return list(self._live.values())

    def active_count(self) -> int:
        from .models import TERMINAL_PHASES

        with self._lock:
            return sum(1 for s in self._live.values() if s.phase not in TERMINAL_PHASES)

    def retire(self, call_id: str) -> None:
        with self._lock:
            state = self._live.pop(call_id, None)
            if state and state.call_sid:
                self._by_sid.pop(state.call_sid, None)
        if state:
            self.persist(state)

    # -- history -----------------------------------------------------------

    def persist(self, state: CallState) -> None:
        request = state.request
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO calls
                   (id, label, destination, goal, mode, phase, call_sid,
                    started_at, ended_at, hold_seconds, error, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    state.id,
                    state.label,
                    request.to if request else "",
                    request.goal if request else "",
                    request.mode.value if request else "",
                    state.phase.value,
                    state.call_sid,
                    state.started_at,
                    state.ended_at or time.time(),
                    state.hold_seconds,
                    state.error,
                    json.dumps(state.summary()),
                ),
            )

    def history(self, limit: int = 25) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
