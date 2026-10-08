"""Small local conversation store used for session-scoped follow-up context."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.data import ROOT

DEFAULT_MEMORY_PATH = ROOT / "storage" / "memory.sqlite3"


def _db_path() -> Path:
    return Path(os.getenv("APP_MEMORY_PATH", str(DEFAULT_MEMORY_PATH)))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_conversation_session ON conversation_turns(session_id, id)")
    return connection


def get_history(session_id: str, limit: int = 8) -> list[dict[str, Any]]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT question, answer, created_at FROM conversation_turns WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def add_turn(session_id: str, question: str, answer: str, keep: int = 12) -> None:
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO conversation_turns(session_id, question, answer, created_at) VALUES (?, ?, ?, ?)",
                (session_id, question[:1000], answer[:12000], datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE session_id=? AND id NOT IN "
                "(SELECT id FROM conversation_turns WHERE session_id=? ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, keep),
            )


def clear_history(session_id: str) -> None:
    with closing(_connect()) as connection:
        with connection:
            connection.execute("DELETE FROM conversation_turns WHERE session_id=?", (session_id,))
