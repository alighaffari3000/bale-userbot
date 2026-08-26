"""Per-user conversation history in SQLite.

Plain `sqlite3` moved off the event loop with `asyncio.to_thread` — no extra
dependency, no connection pool, no ORM. One row per message turn.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'model')),
    text       TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id);
"""


@dataclass(frozen=True)
class Turn:
    role: str
    text: str


class ConversationStore:
    """Append-only message log with a bounded read window."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._lock = asyncio.Lock()

    # -- connection --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    # -- writes ------------------------------------------------------------
    def _append_sync(self, chat_id: int, user_id: int, role: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, user_id, role, text, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, role, text, int(time.time())),
            )

    async def append(self, chat_id: int, user_id: int, role: str, text: str) -> None:
        if role not in ("user", "model"):
            raise ValueError(f"unknown role: {role!r}")
        async with self._lock:
            await asyncio.to_thread(self._append_sync, chat_id, user_id, role, text)

    # -- reads -------------------------------------------------------------
    def _history_sync(self, chat_id: int, limit: int) -> list[Turn]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, text FROM messages WHERE chat_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [Turn(role=r, text=t) for r, t in reversed(rows)]

    async def history(self, chat_id: int, max_turns: int) -> list[Turn]:
        """Return the last `max_turns` exchanges (user+model messages) in order."""
        limit = max(0, max_turns) * 2
        if limit == 0:
            return []
        return await asyncio.to_thread(self._history_sync, chat_id, limit)

    # -- maintenance -------------------------------------------------------
    def _clear_sync(self, chat_id: int) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            return cur.rowcount

    async def clear(self, chat_id: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._clear_sync, chat_id)
