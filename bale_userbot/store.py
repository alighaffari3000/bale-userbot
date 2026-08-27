"""A local mirror of the chats worth searching, in one SQLite file.

Reading an archive off Bale is slow, rate-limited, and destructive to ask
twice: eighteen groups over thirty days is sixteen thousand messages and
several minutes, and the answer is stale the moment it lands in a variable.
Anything that wants to *search* — a person with a question, an agent with
twelve of them — needs the corpus sitting still.

So `sync` writes messages here once and `search` reads them back with no
network at all. The store is a cache, not a source of truth: it holds only
what has been synced, `SyncState` records exactly which window that was, and
nothing in this module ever calls Bale.

Persian text is normalized on the way in and again on the way out, into a
`norm_body` column nobody displays. Without that, none of these match:
`۴۵۰` / `450`, `کیلو‌وات` / `کیلووات`, `ياسوج` / `یاسوج` — and a search for
"پنل 450" over a corpus that writes it "پنل ۴۵۰" quietly returns nothing,
which reads exactly like "nobody is selling one".
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .content import MessageInfo
from .history import TimeLike, to_timestamp

#: Arabic-Indic and Persian digits, in value order, mapped to ASCII.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
#: Letters that differ only by keyboard, plus the joiners that split a word
#: without changing it. ZWNJ becomes nothing, so "می‌شود" == "میشود".
_LETTERS = {
    "ي": "ی",  # Arabic yeh -> Persian yeh
    "ى": "ی",  # alef maksura -> Persian yeh
    "ك": "ک",  # Arabic kaf -> Persian keheh
    "‌": "",  # zero-width non-joiner
    "‏": "",  # right-to-left mark
    "‎": "",  # left-to-right mark
    "ـ": "",  # tatweel
}
_LETTER_TABLE = str.maketrans(_LETTERS)
#: Harakat and other combining marks: decoration, never meaning, in a search.
_MARKS = re.compile("[ً-ْٰ]")
_SPACE = re.compile(r"\s+")

#: Characters of original text kept on each side of a hit in a snippet.
SNIPPET_RADIUS = 90
#: A ceiling no single call can raise: an agent asking for "everything"
#: should get a bounded answer and a `truncated` flag, not the archive.
MAX_LIMIT = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id       INTEGER PRIMARY KEY,
    title         TEXT,
    username      TEXT,
    is_channel    INTEGER NOT NULL DEFAULT 0,
    members_count INTEGER,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    date           INTEGER NOT NULL,
    sender_id      INTEGER,
    kind           TEXT NOT NULL,
    is_forward     INTEGER NOT NULL DEFAULT 0,
    origin_chat_id INTEGER,
    body           TEXT,
    norm_body      TEXT,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS messages_by_date ON messages (chat_id, date);
CREATE INDEX IF NOT EXISTS messages_by_sender ON messages (sender_id);

CREATE TABLE IF NOT EXISTS sync_state (
    chat_id     INTEGER PRIMARY KEY,
    oldest_date INTEGER,
    newest_date INTEGER,
    synced_at   INTEGER NOT NULL,
    note        TEXT
);
"""


def normalize(text: str | None) -> str:
    """Fold a Persian string to the one spelling the index stores.

    Digits to ASCII, Arabic letter variants to Persian, joiners and harakat
    dropped, whitespace collapsed, case folded for the Latin that creeps into
    every price list.
    """
    if not text:
        return ""
    folded = text.translate(_DIGITS).translate(_LETTER_TABLE)
    folded = _MARKS.sub("", folded)
    return _SPACE.sub(" ", folded).strip().casefold()


@dataclass(frozen=True)
class SearchHit:
    """One matching message, trimmed to what a reader needs to triage it."""

    chat_id: int
    chat_title: str | None
    message_id: int
    date: int
    sender_id: int | None
    kind: str
    is_forward: bool
    origin_chat_id: int | None
    snippet: str
    body: str | None

    def as_dict(self) -> dict[str, Any]:
        """The hit without its full body — the shape a result list wants."""
        return {
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "message_id": self.message_id,
            "date": self.date,
            "sender_id": self.sender_id,
            "kind": self.kind,
            "is_forward": self.is_forward,
            "origin_chat_id": self.origin_chat_id,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class SearchResult:
    """Hits plus the honest count behind them.

    `total` is how many messages matched; `hits` is what fit under `limit`.
    They differ often, and a caller that reports only the second number is
    telling its reader the archive is smaller than it is.
    """

    hits: tuple[SearchHit, ...]
    total: int
    truncated: bool

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self):
        return iter(self.hits)


@dataclass(frozen=True)
class SyncState:
    """The window of a chat's archive this store actually holds."""

    chat_id: int
    oldest_date: int | None
    newest_date: int | None
    synced_at: int
    messages: int
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "oldest_date": self.oldest_date,
            "newest_date": self.newest_date,
            "synced_at": self.synced_at,
            "messages": self.messages,
            "note": self.note,
        }


def _escape_like(term: str) -> str:
    """LIKE treats % and _ as wildcards; a price list is full of both."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _regexp(pattern: str, value: str | None) -> bool:
    """The REGEXP operator SQLite leaves to the host language."""
    if value is None:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def _snippet(norm: str, body: str, term: str) -> str:
    """A window of the original text around the first match of `term`.

    The match is located in the normalized copy and cut from the original, so
    the reader gets their own spelling back. The two are the same length only
    by luck — dropping a ZWNJ shortens the normalized one — so the offset is
    treated as approximate and the window is generous.

    Line breaks are flattened. A price list is thirty short lines, and a
    result list of those is unreadable; the full text is one `body` away.
    """
    text = _SPACE.sub(" ", body or "").strip()
    if not text:
        return ""
    position = norm.find(term) if term else -1
    if position < 0 or len(text) <= 2 * SNIPPET_RADIUS:
        return text[: 2 * SNIPPET_RADIUS].strip()
    start = max(0, position - SNIPPET_RADIUS)
    end = min(len(text), position + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


class MessageStore:
    """The synced corpus. Open one, keep it, close it when the process ends."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        in_memory = str(path) == ":memory:"
        if not in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(":memory:" if in_memory else str(self.path))
        self.connection.row_factory = sqlite3.Row
        if not in_memory:
            # A search over a synced corpus is read-heavy and interruptible;
            # WAL keeps a running sync from blocking it.
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.create_function("regexp", 2, _regexp, deterministic=True)
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> MessageStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------
    def put_chat(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        is_channel: bool = False,
        members_count: int | None = None,
    ) -> None:
        """Remember a chat's name so search results can be read without ids."""
        self.connection.execute(
            """
            INSERT INTO chats (chat_id, title, username, is_channel,
                               members_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                is_channel = excluded.is_channel,
                members_count = excluded.members_count,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                title,
                username,
                int(is_channel),
                members_count,
                int(time.time()),
            ),
        )
        self.connection.commit()

    def put_messages(self, messages: Iterable[MessageInfo]) -> int:
        """Insert or refresh messages. Returns how many rows were written.

        An edited message keeps its id, so this upserts rather than ignoring
        conflicts: re-syncing a window is how a correction gets in.
        """
        rows = [
            (
                info.chat_id,
                info.message_id,
                info.date,
                info.sender_id,
                str(info.kind),
                int(info.is_forward),
                info.quoted.chat_id if info.is_forward and info.quoted else None,
                info.searchable_text or None,
                normalize(info.searchable_text) or None,
            )
            for info in messages
        ]
        if not rows:
            return 0
        self.connection.executemany(
            """
            INSERT INTO messages (chat_id, message_id, date, sender_id, kind,
                                  is_forward, origin_chat_id, body, norm_body)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                date = excluded.date,
                kind = excluded.kind,
                is_forward = excluded.is_forward,
                origin_chat_id = excluded.origin_chat_id,
                body = excluded.body,
                norm_body = excluded.norm_body
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def record_sync(self, chat_id: int, note: str | None = None) -> SyncState:
        """Recompute and store which window of `chat_id` the store now holds."""
        row = self.connection.execute(
            "SELECT MIN(date) AS oldest, MAX(date) AS newest, COUNT(*) AS n"
            " FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        synced_at = int(time.time())
        self.connection.execute(
            """
            INSERT INTO sync_state (chat_id, oldest_date, newest_date, synced_at, note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                oldest_date = excluded.oldest_date,
                newest_date = excluded.newest_date,
                synced_at = excluded.synced_at,
                note = excluded.note
            """,
            (chat_id, row["oldest"], row["newest"], synced_at, note),
        )
        self.connection.commit()
        return SyncState(
            chat_id=chat_id,
            oldest_date=row["oldest"],
            newest_date=row["newest"],
            synced_at=synced_at,
            messages=row["n"],
            note=note,
        )

    # -- reading -----------------------------------------------------------
    def newest_date(self, chat_id: int) -> int | None:
        """When the most recent stored message in `chat_id` was sent."""
        row = self.connection.execute(
            "SELECT MAX(date) AS newest FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["newest"] if row else None

    def states(self) -> list[SyncState]:
        """What the store holds, one row per synced chat, newest sync first."""
        rows = self.connection.execute(
            """
            SELECT s.chat_id, s.oldest_date, s.newest_date, s.synced_at, s.note,
                   (SELECT COUNT(*) FROM messages m WHERE m.chat_id = s.chat_id) AS n
            FROM sync_state s ORDER BY s.synced_at DESC
            """
        ).fetchall()
        return [
            SyncState(
                chat_id=row["chat_id"],
                oldest_date=row["oldest_date"],
                newest_date=row["newest_date"],
                synced_at=row["synced_at"],
                messages=row["n"],
                note=row["note"],
            )
            for row in rows
        ]

    def chat_titles(self) -> dict[int, str | None]:
        """Every chat name the store knows, by id."""
        rows = self.connection.execute("SELECT chat_id, title FROM chats").fetchall()
        return {row["chat_id"]: row["title"] for row in rows}

    def search(
        self,
        query: str,
        *,
        chat_ids: Sequence[int] | None = None,
        sender_id: int | None = None,
        since: TimeLike | None = None,
        until: TimeLike | None = None,
        regex: bool = False,
        limit: int = 50,
        newest_first: bool = True,
    ) -> SearchResult:
        """Messages matching `query`, newest first.

        A plain query is whitespace-split and ANDed as substrings of the
        normalized text, which is what someone typing `پنل 450 وات` means.
        `regex=True` hands the pattern to Python's engine instead — the way to
        ask what a keyword cannot, like a wattage range — and it too runs
        against the normalized text, so write patterns in ASCII digits.
        """
        limit = max(1, min(limit, MAX_LIMIT))
        where, params = self._filters(chat_ids, sender_id, since, until)

        terms: list[str] = []
        if regex:
            pattern = normalize(query)
            if not pattern:
                raise ValueError("an empty pattern matches everything")
            where.append("m.norm_body REGEXP ?")
            params.append(pattern)
        else:
            terms = [term for term in normalize(query).split(" ") if term]
            if not terms:
                raise ValueError("an empty query matches everything")
            for term in terms:
                where.append("m.norm_body LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(term)}%")

        clause = " AND ".join(where)
        total = self.connection.execute(
            f"SELECT COUNT(*) AS n FROM messages m WHERE {clause}", params
        ).fetchone()["n"]

        order = "DESC" if newest_first else "ASC"
        rows = self.connection.execute(
            f"""
            SELECT m.*, c.title AS chat_title
            FROM messages m LEFT JOIN chats c ON c.chat_id = m.chat_id
            WHERE {clause} ORDER BY m.date {order} LIMIT ?
            """,
            (*params, limit),
        ).fetchall()

        needle = terms[0] if terms else ""
        hits = tuple(
            SearchHit(
                chat_id=row["chat_id"],
                chat_title=row["chat_title"],
                message_id=row["message_id"],
                date=row["date"],
                sender_id=row["sender_id"],
                kind=row["kind"],
                is_forward=bool(row["is_forward"]),
                origin_chat_id=row["origin_chat_id"],
                snippet=_snippet(row["norm_body"] or "", row["body"] or "", needle),
                body=row["body"],
            )
            for row in rows
        )
        return SearchResult(hits=hits, total=total, truncated=total > len(hits))

    def window(
        self,
        chat_id: int,
        *,
        since: TimeLike | None = None,
        until: TimeLike | None = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        """A slice of one chat's stored archive, plus the honest total.

        Unlike `search`, media-only messages are included: someone reading a
        window wants to know a photo went by, even with nothing to match on.
        """
        limit = max(1, min(limit, MAX_LIMIT))
        where: list[str] = ["chat_id = ?"]
        params: list[Any] = [chat_id]
        start = to_timestamp(since)
        end = to_timestamp(until, end_of_day=True)
        if start is not None:
            where.append("date >= ?")
            params.append(start)
        if end is not None:
            where.append("date <= ?")
            params.append(end)
        clause = " AND ".join(where)
        total = self.connection.execute(
            f"SELECT COUNT(*) AS n FROM messages WHERE {clause}", params
        ).fetchone()["n"]
        order = "DESC" if newest_first else "ASC"
        rows = self.connection.execute(
            f"SELECT * FROM messages WHERE {clause} ORDER BY date {order} LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows], total

    def messages(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Full stored rows for specific messages — the follow-up to a hit."""
        if not message_ids:
            return []
        marks = ",".join("?" for _ in message_ids)
        rows = self.connection.execute(
            f"SELECT * FROM messages WHERE chat_id = ? AND message_id IN ({marks})",
            (chat_id, *message_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _filters(
        chat_ids: Sequence[int] | None,
        sender_id: int | None,
        since: TimeLike | None,
        until: TimeLike | None,
    ) -> tuple[list[str], list[Any]]:
        """The WHERE fragments every query shares, and their parameters."""
        # Every column is qualified: the hit query joins
        # `chats`, which carries a chat_id of its own.
        where: list[str] = ["m.norm_body IS NOT NULL"]
        params: list[Any] = []
        if chat_ids:
            where.append(f"m.chat_id IN ({','.join('?' for _ in chat_ids)})")
            params.extend(chat_ids)
        if sender_id is not None:
            where.append("m.sender_id = ?")
            params.append(sender_id)
        start = to_timestamp(since)
        end = to_timestamp(until, end_of_day=True)
        if start is not None:
            where.append("m.date >= ?")
            params.append(start)
        if end is not None:
            where.append("m.date <= ?")
            params.append(end)
        return where, params
