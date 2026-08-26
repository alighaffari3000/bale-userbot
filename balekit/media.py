"""Sending and downloading every attachment kind through one door.

BaleClient has a separate `send_photo`/`send_video`/`send_voice`/`send_audio`/
`send_gif`/`send_document`, each wanting different metadata. `send_media()`
picks the right one from the file's MIME type (or an explicit kind) and fills
in the metadata that method expects.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, BinaryIO

from baleclient import Client
from baleclient.enums import ChatType, SendType
from baleclient.types import (
    AudioExt,
    DocumentMessage,
    DocumentsExt,
    FileInput,
    InlineKeyboardMarkup,
    Message,
)

from .content import MediaInfo, MessageInfo, MessageKind, describe, unwrap

FileLike = str | Path | bytes | FileInput

#: Bale rejects cover thumbnails above this size.
MAX_THUMB_BYTES = 2 * 1024

_EXTENSION_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
}


def as_file_input(file: FileLike, name: str | None = None) -> FileInput:
    if isinstance(file, FileInput):
        return file
    if isinstance(file, (str, Path)):
        return FileInput(str(file), name=name)
    if isinstance(file, bytes):
        return FileInput(file, name=name)
    raise TypeError(f"unsupported file input: {type(file).__name__}")


def detect_kind(mime_type: str | None, name: str | None = None) -> MessageKind:
    """Pick a media kind from MIME type, falling back to the file extension."""
    mime = (mime_type or "").lower()
    if not mime or mime == "application/octet-stream":
        guessed = mimetypes.guess_type(name or "")[0]
        mime = (guessed or mime).lower()

    lowered = (name or "").lower()
    if mime == "image/gif" or lowered.endswith(".gif"):
        return MessageKind.GIF
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    if mime.startswith("audio/"):
        return MessageKind.AUDIO
    return MessageKind.DOCUMENT


def image_size(data: bytes) -> tuple[int, int] | None:
    """Image dimensions, if Pillow is installed. Returns None otherwise."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except Exception:
        return None


def make_thumbnail(data: bytes, width: int = 50) -> bytes | None:
    """A <=2KB cover thumbnail, if Pillow is installed. Returns None otherwise."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or "JPEG"
            height = max(1, int((img.height / img.width) * width))
            resized = img.convert("RGB") if fmt == "JPEG" else img
            resized = resized.resize((width, height))
            buffer = io.BytesIO()
            resized.save(buffer, format=fmt)
            out = buffer.getvalue()
    except Exception:
        return None
    return out if len(out) <= MAX_THUMB_BYTES else None


def audio_ext(
    duration: int | None = None,
    album: str | None = None,
    genre: str | None = None,
    track: str | None = None,
) -> DocumentsExt:
    """Build an audio ext block that actually keeps its music tags.

    `AudioExt`'s own before-validator writes `None` into the aliased keys
    "2"/"3"/"4" whenever they are absent from the input, and the alias wins
    over the field name — so `AudioExt(album=...)`, which is exactly what
    BaleClient's `send_audio` does internally, silently drops album, genre and
    track. Feeding the aliases directly is the way around it.
    """
    return DocumentsExt(
        audio=AudioExt.model_validate(
            {"1": duration, "2": album, "3": genre, "4": track}
        )
    )


async def _send_audio(
    client: Client,
    file: Any,
    common: dict,
    *,
    duration: int | None,
    album: str | None,
    genre: str | None,
    track: str | None,
) -> Message:
    if not any((album, genre, track)):
        return await client.send_audio(audio=file, duration=duration, **common)

    # Tags present: bypass send_audio, whose ext block would lose them.
    return await client._send_file_message(
        file=file,
        send_type=SendType.AUDIO,
        ext=audio_ext(duration, album, genre, track),
        chat_id=common["chat_id"],
        chat_type=common["chat_type"],
        caption=common.get("caption"),
        reply_to=common.get("reply_to"),
        reply_markup=common.get("reply_markup"),
    )


async def send_media(
    client: Client,
    file: FileLike,
    chat_id: int,
    chat_type: ChatType,
    *,
    kind: MessageKind | None = None,
    caption: str | None = None,
    reply_to: Message | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    name: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    thumbnail: bool = True,
    **extra: Any,
) -> Message:
    """Send any file, routed to the send_* method that matches its kind.

    `duration` is in milliseconds, as BaleClient's own send methods expect.
    Dimensions and cover thumbnails are filled in automatically for images when
    Pillow is available; pass `width`/`height` explicitly for video, where
    inspecting the file would need ffprobe.
    """
    file_input = as_file_input(file, name=name)
    kind = kind or detect_kind(file_input.info.mime_type, file_input.info.name)

    common: dict[str, Any] = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "caption": caption,
        "reply_to": reply_to,
        "reply_markup": reply_markup,
    }

    if kind is MessageKind.DOCUMENT:
        return await client.send_document(file=file_input, **common, **extra)

    if kind is MessageKind.VOICE:
        return await client.send_voice(
            voice=file_input, duration=duration, **common, **extra
        )

    if kind is MessageKind.AUDIO:
        return await _send_audio(
            client,
            file_input,
            common,
            duration=duration,
            album=extra.pop("album", None),
            genre=extra.pop("genre", None),
            track=extra.pop("track", None),
        )

    # photo / video / gif all take cover metadata
    cover: dict[str, Any] = {}
    if width is None or height is None:
        if kind in (MessageKind.PHOTO, MessageKind.GIF):
            measured = image_size(await file_input.get_content())
            if measured:
                width, height = measured
    if width is not None:
        cover["cover_width"] = width
    if height is not None:
        cover["cover_height"] = height

    if thumbnail and kind is MessageKind.PHOTO:
        thumb = make_thumbnail(await file_input.get_content())
        if thumb is not None:
            cover["cover_thumb"] = FileInput(thumb, name="thumb.jpg")

    if kind is MessageKind.PHOTO:
        return await client.send_photo(photo=file_input, **common, **cover, **extra)
    if kind is MessageKind.VIDEO:
        return await client.send_video(
            video=file_input, duration=duration, **common, **cover, **extra
        )
    if kind is MessageKind.GIF:
        return await client.send_gif(
            gif=file_input, duration=duration, **common, **cover, **extra
        )

    raise ValueError(f"{kind} is not a sendable media kind")


async def resend(
    client: Client,
    message: Message,
    chat_id: int,
    chat_type: ChatType,
    *,
    caption: str | None = None,
    reply_to: Message | None = None,
) -> Message:
    """Re-send a received attachment without downloading and re-uploading it.

    Bale accepts the original file id and access hash, so this is a pointer
    copy: no bytes move, and it works for any media kind. Dimensions, duration,
    music tags and the cover thumbnail are carried over; `caption` overrides
    the original caption, which is otherwise kept.
    """
    info = describe(message)
    if info.media is None:
        raise ValueError(f"message {info.message_id} carries no file to resend")

    document = _document_of(message)
    media = info.media
    caption = caption if caption is not None else info.caption
    common: dict[str, Any] = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "caption": caption,
        "reply_to": reply_to,
    }

    cover: dict[str, Any] = {}
    if media.width:
        cover["cover_width"] = media.width
    if media.height:
        cover["cover_height"] = media.height
    if document.thumb is not None and document.thumb.image:
        cover["cover_thumb"] = FileInput(document.thumb.image, name="thumb.jpg")

    if info.kind is MessageKind.PHOTO:
        return await client.send_photo(photo=document, **common, **cover)
    if info.kind is MessageKind.VIDEO:
        return await client.send_video(
            video=document, duration=media.duration, **common, **cover
        )
    if info.kind is MessageKind.GIF:
        return await client.send_gif(
            gif=document, duration=media.duration, **common, **cover
        )
    if info.kind is MessageKind.VOICE:
        return await client.send_voice(
            voice=document, duration=media.duration, **common
        )
    if info.kind is MessageKind.AUDIO:
        return await _send_audio(
            client,
            document,
            common,
            duration=media.duration,
            album=media.album,
            genre=media.genre,
            track=media.track,
        )
    # A plain file keeps every original field when it is sent back as-is.
    return await client.send_document(
        file=document, **common, use_own_content=caption is None
    )


def _document_of(message: Message) -> DocumentMessage:
    content, _ = unwrap(message.content)
    if content.document is None:
        raise ValueError("message carries no document")
    return content.document


def suggest_filename(media: MediaInfo, fallback: str = "file") -> str:
    """A usable filename for a download, even when the sender provided none."""
    if media.name:
        return Path(media.name).name
    suffix = _EXTENSION_BY_MIME.get(
        (media.mime_type or "").lower()
    ) or mimetypes.guess_extension((media.mime_type or "").split(";")[0].strip())
    return f"{fallback}-{media.file_id}{suffix or '.bin'}"


async def download(
    client: Client,
    source: Message | MessageInfo | MediaInfo,
    destination: str | Path | BinaryIO | None = None,
) -> bytes | Path:
    """Download an attachment.

    With no destination the bytes are returned. With a directory, the file is
    written inside it under the sender's filename (or a generated one) and the
    path is returned.
    """
    media = _media_of_source(source)

    if destination is None:
        buffer = await client.download_file(media.file_id, media.access_hash)
        return buffer.read()

    if isinstance(destination, (str, Path)):
        path = Path(destination)
        if path.is_dir():
            path = path / suggest_filename(media)
        path.parent.mkdir(parents=True, exist_ok=True)
        await client.download_file(media.file_id, media.access_hash, destination=path)
        return path

    await client.download_file(
        media.file_id, media.access_hash, destination=destination
    )
    return destination


def _media_of_source(source: Message | MessageInfo | MediaInfo) -> MediaInfo:
    if isinstance(source, MediaInfo):
        return source
    info = source if isinstance(source, MessageInfo) else describe(source)
    if info.media is None:
        raise ValueError(f"message {info.message_id} carries no file to download")
    return info.media
