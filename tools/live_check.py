#!/usr/bin/env python
"""Round-trip every message kind against a real Bale account.

Sends one message of each kind to a chat you choose (your own account by
default — Bale's "Saved Messages"), reads the history back, and reports what
`describe()` made of each one. This is the test that needs a live account and
therefore cannot run in CI.

    python tools/live_check.py                # send to yourself
    python tools/live_check.py --chat 12345   # send to another chat
    python tools/live_check.py --keep         # do not delete the test messages

Nothing here is destructive beyond deleting the messages it sent itself.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from baleclient.enums import ChatType  # noqa: E402

from balekit import BaleApp, Config, describe  # noqa: E402
from balekit.media import send_media  # noqa: E402

# --- tiny synthetic files, so the tool has no asset directory ---------------


def _png(width: int = 16, height: int = 16) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    row = lambda x: b"\x00" + bytes([(x * 16) % 256, 80, 160] * width)  # noqa: E731
    raw = b"".join(row(x) for x in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _gif() -> bytes:
    return (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
        b"\x00\x02\x02D\x01\x00;"
    )


def _mp4() -> bytes:
    # A minimal, structurally valid MP4 header; players may refuse to play it,
    # but Bale stores and returns it, which is what we are checking.
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    mdat = b"\x00\x00\x00\x10mdat" + b"\x00" * 8
    return ftyp + mdat


def _mp3() -> bytes:
    # ID3v2 header + one silent MPEG frame.
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 100


def _ogg() -> bytes:
    return b"OggS\x00\x02" + b"\x00" * 20 + b"\x01vorbis" + b"\x00" * 20


CASES = [
    ("text", None, None),
    ("photo", _png(), "probe.png"),
    ("gif", _gif(), "probe.gif"),
    ("video", _mp4(), "probe.mp4"),
    ("audio", _mp3(), "probe.mp3"),
    ("voice", _ogg(), "probe.ogg"),
    ("document", b"balekit live check\n", "probe.txt"),
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", type=int, help="target chat id (default: yourself)")
    parser.add_argument("--keep", action="store_true", help="keep the sent messages")
    parser.add_argument(
        "--skip", default="", help="comma separated kinds to skip, e.g. video,voice"
    )
    args = parser.parse_args()

    app = BaleApp(Config.from_env())
    client = app.client
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    async with client:  # connects, handshakes, listens in the background
        chat_id = args.chat or client.id
        chat_type = ChatType.PRIVATE
        print(f"target chat: {chat_id} (self id: {client.id})\n")

        sent = []
        results = []
        for label, payload, name in CASES:
            if label in skip:
                results.append((label, "skipped", ""))
                continue
            try:
                if payload is None:
                    message = await client.send_message(
                        f"balekit live check: {label}", chat_id, chat_type
                    )
                else:
                    message = await send_media(
                        client,
                        payload,
                        chat_id,
                        chat_type,
                        name=name,
                        caption=f"balekit live check: {label}",
                    )
                sent.append(message)
                info = describe(message)
                detail = ""
                if info.media:
                    detail = (
                        f"{info.media.mime_type} {info.media.size}B "
                        f"{info.media.width}x{info.media.height} "
                        f"dur={info.media.duration}"
                    )
                match = (
                    "OK"
                    if info.kind.value == label
                    else f"MISMATCH -> {info.kind.value}"
                )
                results.append((label, match, detail))
            except Exception as exc:  # keep going; one kind failing is a result
                results.append((label, f"FAILED: {type(exc).__name__}: {exc}", ""))

            await asyncio.sleep(1)  # stay well under any rate limit

        print("\n--- send + classify -------------------------------------")
        for label, status, detail in results:
            print(f"{label:<9} {status:<28} {detail}")

        # Read the same messages back from history and re-classify them: this
        # is what proves the *received* wire format parses, not just our own.
        print("\n--- read back from history ------------------------------")
        history = await client.load_history(chat_id, chat_type, limit=len(sent) + 5)
        by_id = {m.message_id: m for m in history}
        for message in sent:
            fetched = by_id.get(message.message_id)
            if fetched is None:
                print(f"{message.message_id}: not found in history")
                continue
            info = describe(fetched)
            extra = ""
            if info.media:
                extra = f" name={info.media.name} mime={info.media.mime_type}"
            print(
                f"{message.message_id}: kind={info.kind.value} "
                f"caption={info.caption!r}{extra}"
            )

        # Download one attachment end to end.
        print("\n--- download --------------------------------------------")
        for message in sent:
            info = describe(message)
            if info.is_media:
                data = await app.download(message, destination=None)
                print(f"downloaded {info.kind.value}: {len(data)} bytes")
                break

        if not args.keep:
            print("\ncleaning up…")
            for message in sent:
                try:
                    await message.delete()
                except Exception as exc:
                    print(f"  could not delete {message.message_id}: {exc}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(1) from None
