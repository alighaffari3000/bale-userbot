"""`python -m bale_userbot <command>` — login, whoami, session info."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .app import BaleApp, prepare_session_file
from .config import Config
from .logging_setup import setup_logging


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bale_userbot")
    parser.add_argument("--session", help="path to the session file")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="authenticate with a phone number (OTP)")
    login.add_argument(
        "--replace", action="store_true", help="overwrite an existing session"
    )
    whoami = sub.add_parser(
        "whoami", help="show the account behind the stored session"
    )
    whoami.add_argument(
        "--offline", action="store_true", help="do not connect; id only"
    )

    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.session:
        from pathlib import Path

        config.session_file = Path(args.session).expanduser()
    setup_logging(config.log_level)

    if args.command == "login":
        return asyncio.run(cmd_login(config, args.replace))
    return asyncio.run(cmd_whoami(config, args.offline))


if __name__ == "__main__":
    raise SystemExit(main())
