"""`python -m bale_userbot <command>` — login, whoami, groups, members, history.

The read-only commands exist so the common questions — who is in this group,
what did it say last week — can be answered without writing a script. They all
print JSON with `--json`, which is the point: the CLI is a way to get the same
records `groups.py` and `history.py` return into a file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from baleclient.enums import ChatType

from .app import BaleApp, prepare_session_file
from .config import Config
from .groups import member_records
from .history import message_record
from .logging_setup import setup_logging

#: Chat types a user can name on the command line.
CHAT_TYPES = {
    "private": ChatType.PRIVATE,
    "bot": ChatType.BOT,
    "group": ChatType.GROUP,
    "supergroup": ChatType.SUPER_GROUP,
    "channel": ChatType.CHANNEL,
}


def _emit(rows: Any, as_json: bool, lines: list[str]) -> int:
    """Print machine-readable JSON, or the human lines the caller prepared."""
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return 0


async def cmd_login(config: Config, replace: bool) -> int:
    prepare_session_file(config.session_file)

    if config.session_file.exists():
        if not replace:
            answer = input(
                f"a session already exists at {config.session_file}. Replace it? [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("keeping the existing session.")
                return 0
        config.session_file.unlink()

    app = BaleApp(config)
    # With no session file this walks BaleClient's phone/OTP CLI and writes it.
    await app.client._ensure_token_exists()
    prepare_session_file(config.session_file)

    print(f"\nlogged in as user id {app.client.id}")
    print(f"session saved to {config.session_file}")
    print("keep that file secret — it is the credential for the account.")
    return 0


async def cmd_whoami(config: Config, offline: bool = False) -> int:
    if not config.session_file.exists():
        print(f"no session at {config.session_file}", file=sys.stderr)
        return 2

    app = BaleApp(config)
    client = app.client
    print(f"user id:      {client.id}")
    print(f"session file: {config.session_file}")

    if offline:
        return 0

    # The stored credential carries only the id; the display name needs a
    # round trip. --offline skips it.
    try:
        async with client:
            me = await client.get_me()
            print(f"name:         {me.name or '-'}")
            if me.username:
                print(f"username:     @{me.username}")
    except Exception as exc:
        print(f"name:         (could not fetch: {type(exc).__name__})")
    return 0


async def _connected(config: Config) -> BaleApp:
    if not config.session_file.exists():
        raise SystemExit(f"no session at {config.session_file}. Run `login` first.")
    app = BaleApp(config)
    await app.start(background=True)
    return app


def _handle(username: str | None) -> str:
    return f"@{username}" if username else "-"


def _kind(group: Any) -> str:
    return "channel" if group.is_channel else "group"


async def cmd_groups(config: Config, args: argparse.Namespace) -> int:
    app = await _connected(config)
    try:
        groups = await app.groups(limit=args.limit)
    finally:
        await app.stop()

    return _emit(
        [g.as_dict() for g in groups],
        args.json,
        [
            f"{g.id:>14}  {g.members_count:>7}  {_kind(g):<8}"
            f"  {_handle(g.username):<20}  {g.title}"
            for g in groups
        ]
        or ["no groups"],
    )


def _role(record: dict[str, Any]) -> str:
    if record["is_owner"]:
        return "owner"
    return "admin" if record["is_admin"] else ""


async def cmd_members(config: Config, args: argparse.Namespace) -> int:
    profiles = not args.no_profiles
    app = await _connected(config)
    try:
        group = await app.group(args.chat_id)
        if args.admins:
            members = await app.admins(args.chat_id, profiles=profiles)
            rows = member_records(members, group)
        else:
            _, rows = await app.export_members(
                args.chat_id, profiles=profiles, limit=args.limit
            )
    finally:
        await app.stop()

    header = (
        f"{group.title}: {group.members_count} members, {len(rows)} "
        f"{'admins' if args.admins else 'listed'}"
    )
    return _emit(
        rows,
        args.json,
        [header]
        + [
            f"{r['user_id']:>14}  {_role(r):<6}  {_handle(r['username']):<20}"
            f"  {r['name'] or '-'}"
            for r in rows
        ],
    )


async def cmd_history(config: Config, args: argparse.Namespace) -> int:
    app = await _connected(config)
    try:
        rows = await app.export_history(
            args.chat_id,
            CHAT_TYPES[args.chat_type],
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    finally:
        await app.stop()

    return _emit(
        rows,
        args.json,
        [
            f"[{r['kind']:<8}] {r['message_id']:>14} from={r['sender_id']:<14} "
            f"{(r['text'] or r['caption'] or '')[:60]!r}"
            for r in rows
        ]
        or ["no messages in that range"],
    )


async def cmd_pins(config: Config, args: argparse.Namespace) -> int:
    app = await _connected(config)
    try:
        pinned = await app.pins(args.chat_id)
        rows = [app.describe(m) for m in pinned]
    finally:
        await app.stop()

    return _emit(
        [message_record(info) for info in rows],
        args.json,
        [
            f"{info.message_id:>14}  [{info.kind}]  {(info.body or '')[:60]!r}"
            for info in rows
        ]
        or ["no pinned messages"],
    )


async def cmd_link(config: Config, args: argparse.Namespace) -> int:
    app = await _connected(config)
    try:
        link = await app.invite_link(args.chat_id, revoke=args.revoke)
    finally:
        await app.stop()
    return _emit(link.as_dict(), args.json, [link.url])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bale_userbot")
    parser.add_argument("--session", help="path to the session file")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="authenticate with a phone number (OTP)")
    login.add_argument(
        "--replace", action="store_true", help="overwrite an existing session"
    )

    whoami = sub.add_parser("whoami", help="show the account behind the stored session")
    whoami.add_argument(
        "--offline", action="store_true", help="do not connect; id only"
    )

    groups = sub.add_parser("groups", help="list the groups and channels you are in")
    groups.add_argument("--limit", type=int, default=200)
    groups.add_argument("--json", action="store_true", help="print JSON records")

    members = sub.add_parser("members", help="list a group's members")
    members.add_argument("chat_id", type=int)
    members.add_argument("--limit", type=int, default=None)
    members.add_argument("--admins", action="store_true", help="admins and owner only")
    members.add_argument(
        "--no-profiles", action="store_true", help="skip names and usernames (faster)"
    )
    members.add_argument("--json", action="store_true", help="print JSON records")

    history = sub.add_parser("history", help="read a chat's messages")
    history.add_argument("chat_id", type=int)
    history.add_argument("--chat-type", choices=sorted(CHAT_TYPES), default="group")
    history.add_argument(
        "--since", help="date or datetime, e.g. 2026-04-26 or 2026-04-26T09:00"
    )
    history.add_argument(
        "--until", help="a plain date covers the whole day, e.g. 2026-04-28"
    )
    history.add_argument("--limit", type=int, default=None)
    history.add_argument("--json", action="store_true", help="print JSON records")

    pins = sub.add_parser("pins", help="list a group's pinned messages")
    pins.add_argument("chat_id", type=int)
    pins.add_argument("--json", action="store_true", help="print JSON records")

    link = sub.add_parser("link", help="show a group's invite link")
    link.add_argument("chat_id", type=int)
    link.add_argument(
        "--revoke", action="store_true", help="kill the current link, print the new one"
    )
    link.add_argument("--json", action="store_true", help="print JSON records")

    return parser


#: Subcommands that take the parsed namespace. `login`/`whoami` predate them.
_COMMANDS = {
    "groups": cmd_groups,
    "members": cmd_members,
    "history": cmd_history,
    "pins": cmd_pins,
    "link": cmd_link,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = Config.from_env()
    if args.session:
        from pathlib import Path

        config.session_file = Path(args.session).expanduser()
    # Data commands print records on stdout; their log lines must not.
    setup_logging(config.log_level, sys.stderr if args.command in _COMMANDS else None)

    if args.command == "login":
        return asyncio.run(cmd_login(config, args.replace))
    if args.command == "whoami":
        return asyncio.run(cmd_whoami(config, args.offline))
    return asyncio.run(_COMMANDS[args.command](config, args))


if __name__ == "__main__":
    raise SystemExit(main())
