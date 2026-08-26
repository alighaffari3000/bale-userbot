"""Dump a group's members to JSON and to SQLite.

The point of this example is the seam: `export_members()` returns rows, and
*this file* — the application — decides where they go. bale-userbot never opens
a file or a database of its own.

    python examples/export_members.py <chat_id>
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from bale_userbot import BaleApp, Config

COLUMNS = ("group_id", "user_id", "name", "username", "is_admin", "is_owner", "is_bot")


def to_json(rows: list[dict], path: Path) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def to_sqlite(rows: list[dict], path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS members ("
            "group_id INTEGER, user_id INTEGER, name TEXT, username TEXT,"
            "is_admin INTEGER, is_owner INTEGER, is_bot INTEGER,"
            "PRIMARY KEY (group_id, user_id))"
        )
        db.executemany(
            f"INSERT OR REPLACE INTO members VALUES ({','.join('?' * len(COLUMNS))})",
            [tuple(row[c] for c in COLUMNS) for row in rows],
        )


async def main(chat_id: int) -> None:
    app = BaleApp(Config.from_env())
    await app.start(background=True)
    try:
        group, rows = await app.export_members(chat_id)
        print(f"{group.title}: {group.members_count} members, {len(rows)} listed")

        to_json(rows, Path(f"members-{group.id}.json"))
        to_sqlite(rows, Path("members.db"))
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
