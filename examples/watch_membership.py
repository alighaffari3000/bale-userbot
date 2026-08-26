"""Report who joined, left, or became an admin since the last run.

Run it once to record a baseline, then again later to see the difference. The
snapshot lives in a JSON file here — again, that choice is the application's;
`diff_members()` itself never touches the disk, so a cron job could just as
easily keep its snapshots in a table.

    python examples/watch_membership.py <chat_id>
"""

import asyncio
import json
import sys
from pathlib import Path

from bale_userbot import BaleApp, Config, MemberInfo


def read_snapshot(path: Path) -> list[MemberInfo]:
    if not path.exists():
        return []
    return [MemberInfo(**row) for row in json.loads(path.read_text(encoding="utf-8"))]


def write_snapshot(path: Path, members: list[MemberInfo]) -> None:
    rows = [m.as_dict() for m in members]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def label(member: MemberInfo) -> str:
    handle = f" @{member.username}" if member.username else ""
    return f"{member.name or member.user_id}{handle}"


async def main(chat_id: int) -> None:
    snapshot_file = Path(f"members-{chat_id}.snapshot.json")
    previous = read_snapshot(snapshot_file)

    app = BaleApp(Config.from_env())
    await app.start(background=True)
    try:
        changes, current = await app.watch_membership(chat_id, previous)
    finally:
        await app.stop()

    if not previous:
        print(f"baseline recorded: {len(current)} members")
    elif not changes:
        print("nothing changed")
    else:
        for member in changes.joined:
            print(f"+ joined    {label(member)}")
        for member in changes.left:
            print(f"- left      {label(member)}")
        for member in changes.promoted:
            print(f"^ promoted  {label(member)}")
        for member in changes.demoted:
            print(f"v demoted   {label(member)}")
        for was, now in changes.renamed:
            print(f"~ renamed   {label(was)} -> {label(now)}")

    write_snapshot(snapshot_file, current)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
