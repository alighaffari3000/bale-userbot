#!/usr/bin/env python
"""Discover how Bale encodes message kinds bale-userbot does not model yet.

Read-only. Loads recent history from a chat (your own "Saved Messages" by
default) and dumps the raw numeric protobuf fields of every message whose
content carries something our models did not capture — which is exactly what
a location, contact card or sticker looks like to `BaleClient 1.0.9`.

Usage: send a location, a contact and a sticker to your Saved Messages from
the Bale app, then:

    python tools/probe_content.py                 # dump the unrecognized ones
    python tools/probe_content.py --all           # dump every message
    python tools/probe_content.py --chat 12345    # another chat
    python tools/probe_content.py --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from baleclient.enums import ChatType  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from bale_userbot import BaleApp, Config, MessageKind, describe  # noqa: E402


def collect_extras(obj: Any, path: str = "content") -> list[tuple[str, Any]]:
    """Walk a pydantic tree and return every (path, extra-fields) pair."""
    found: list[tuple[str, Any]] = []
    if isinstance(obj, BaseModel):
        extra = getattr(obj, "model_extra", None)
        if extra:
            found.append((path, extra))
        for name in type(obj).model_fields:
            found.extend(collect_extras(getattr(obj, name), f"{path}.{name}"))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(collect_extras(value, f"{path}[{key!r}]"))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            found.extend(collect_extras(value, f"{path}[{i}]"))
    return found


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return jsonable(value.model_dump(by_alias=True))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    return value


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", type=int, help="chat id (default: yourself)")
    parser.add_argument("--limit", type=int, default=30, help="history depth")
    parser.add_argument("--all", action="store_true", help="dump every message")
    args = parser.parse_args()

    app = BaleApp(Config.from_env())
    client = app.client

    async with client:
        chat_id = args.chat or client.id
        history = await client.load_history(chat_id, ChatType.PRIVATE, limit=args.limit)
        print(f"chat {chat_id}: {len(history)} messages loaded\n")

        hits = 0
        for message in history:
            info = describe(message)
            extras = collect_extras(message, "message")
            interesting = info.kind is MessageKind.UNKNOWN or extras
            if not (args.all or interesting):
                continue
            hits += 1
            print(f"=== message {message.message_id}  kind={info.kind.value} ===")
            if extras:
                for where, extra in extras:
                    print(f"  extra fields at {where}:")
                    print(json.dumps(jsonable(extra), indent=4, ensure_ascii=False))
            dump = message.content if info.kind is not MessageKind.UNKNOWN else message
            label = "content" if dump is message.content else "message"
            print(f"  full {label} (numeric aliases):")
            print(json.dumps(jsonable(dump), indent=4, ensure_ascii=False))
            print()

        if hits == 0:
            print(
                "nothing unrecognized in this range. Send a location, a contact\n"
                "card and a sticker to this chat from the Bale app, then re-run\n"
                "(or use --all / --limit to widen the search)."
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(1) from None
