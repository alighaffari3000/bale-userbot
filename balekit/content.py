"""One vocabulary for "what kind of message is this?".

In Bale's protocol every attachment — photo, video, voice, music, gif, plain
file — arrives as the *same* `DocumentMessage`; only `document.ext` and the
MIME type tell them apart, and media sent with an inline keyboard is nested one
level deeper inside `content.bot_message`. `describe()` flattens all of that
into a single struct so callers never inspect protobuf field aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from baleclient.enums import ChatType
from baleclient.types import DocumentMessage, Message, MessageContent


class MessageKind(StrEnum):
    """What a message actually carries."""

    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    GIF = "gif"
    VOICE = "voice"
    AUDIO = "audio"
    DOCUMENT = "document"
    GIFT = "gift"
    SERVICE = "service"
    FORWARD = "forward"
    UNKNOWN = "unknown"


#: Kinds that carry a downloadable file.
MEDIA_KINDS = frozenset(
    {
        MessageKind.PHOTO,
        MessageKind.VIDEO,
        MessageKind.GIF,
        MessageKind.VOICE,
        MessageKind.AUDIO,
        MessageKind.DOCUMENT,
    }
)


@dataclass(frozen=True)
class MediaInfo:
    """Everything needed to download or re-send an attachment."""

    file_id: int
    access_hash: int
    mime_type: str
    name: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    has_thumb: bool = False
    # Music tags, when the sender's client provided them.
    album: str | None = None
    genre: str | None = None
    track: str | None = None


@dataclass(frozen=True)
class MessageInfo:
    """A flat view of an incoming message."""

    kind: MessageKind
    message_id: int
    chat_id: int
    chat_type: ChatType
    sender_id: int
    date: int
    text: str | None = None
    caption: str | None = None
    media: MediaInfo | None = None
    has_keyboard: bool = False
    is_forward: bool = False
    reply_to_id: int | None = None
    service_text: str | None = None

    @property
    def is_media(self) -> bool:
        return self.kind in MEDIA_KINDS

    @property
    def is_private(self) -> bool:
        return self.chat_type in (ChatType.PRIVATE, ChatType.BOT)

    @property
    def body(self) -> str | None:
        """Text of a text message, or the caption of a media message."""
        return self.text if self.kind is MessageKind.TEXT else self.caption


def _name_of(document: DocumentMessage) -> str | None:
    # The wire format sometimes carries a dict here instead of a string.
    name = document.name
    return name if isinstance(name, str) else None


def kind_of_document(document: DocumentMessage) -> MessageKind:
    """Classify an attachment from its ext block, falling back to MIME type."""
    ext = document.ext
    if ext is not None:
        if ext.voice is not None:
            return MessageKind.VOICE
        if ext.gif is not None:
            return MessageKind.GIF
        if ext.video is not None:
            return MessageKind.VIDEO
        if ext.audio is not None:
            return MessageKind.AUDIO
        if ext.photo is not None:
            return MessageKind.PHOTO

    # Senders on other clients do not always fill ext in.
    mime = (document.mime_type or "").lower()
    name = (_name_of(document) or "").lower()
    if mime == "image/gif" or name.endswith(".gif"):
        return MessageKind.GIF
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    if mime.startswith("audio/"):
        return MessageKind.AUDIO
    return MessageKind.DOCUMENT


def _media_of(document: DocumentMessage, kind: MessageKind) -> MediaInfo:
    ext = document.ext
    width = height = duration = None
    album = genre = track = None

    if ext is not None:
        if kind is MessageKind.PHOTO and ext.photo is not None:
            width, height = ext.photo.w, ext.photo.h
        elif kind is MessageKind.VIDEO and ext.video is not None:
            width, height, duration = ext.video.w, ext.video.h, ext.video.duration
        elif kind is MessageKind.GIF and ext.gif is not None:
            width, height, duration = ext.gif.w, ext.gif.h, ext.gif.duration
        elif kind is MessageKind.VOICE and ext.voice is not None:
            duration = ext.voice.duration
        elif kind is MessageKind.AUDIO and ext.audio is not None:
            duration = ext.audio.duration
            album, genre, track = ext.audio.album, ext.audio.genre, ext.audio.track

    return MediaInfo(
        file_id=document.file_id,
        access_hash=document.access_hash,
        mime_type=document.mime_type,
        name=_name_of(document),
        size=document.size,
        width=width,
        height=height,
        duration=duration,
        has_thumb=document.thumb is not None,
        album=album,
        genre=genre,
        track=track,
    )


def unwrap(content: MessageContent) -> tuple[MessageContent, bool]:
    """Return the payload content and whether it came wrapped in a keyboard."""
    if content.bot_message is not None and content.bot_message.message is not None:
        return content.bot_message.message, True
    return content, False


def describe(message: Message) -> MessageInfo:
    """Flatten a `Message` into a `MessageInfo`. Never raises on odd payloads."""
    content, has_keyboard = unwrap(message.content)

    kind = MessageKind.UNKNOWN
    text = caption = service_text = None
    media = None

    if content.document is not None:
        kind = kind_of_document(content.document)
        media = _media_of(content.document, kind)
        if content.document.caption is not None:
            caption = content.document.caption.content
    elif content.text is not None:
        kind = MessageKind.TEXT
        text = content.text.value
    elif content.gift is not None:
        kind = MessageKind.GIFT
    elif content.service_message is not None:
        kind = MessageKind.SERVICE
        service_text = content.service_message.text
    elif content.empty:
        # An empty stub with a quoted message is how a forward arrives.
        kind = MessageKind.FORWARD

    # BaleObject sets use_enum_values, so chat.type arrives as a plain int.
    try:
        chat_type = ChatType(message.chat.type)
    except ValueError:
        chat_type = ChatType.UNKNOWN

    replied = message.replied_to or message.quoted_replied_to
    return MessageInfo(
        kind=kind,
        message_id=message.message_id,
        chat_id=message.chat.id,
        chat_type=chat_type,
        sender_id=message.sender_id,
        date=message.date,
        text=text,
        caption=caption,
        media=media,
        has_keyboard=has_keyboard,
        is_forward=content.empty or kind is MessageKind.FORWARD,
        reply_to_id=getattr(replied, "message_id", None),
        service_text=service_text,
    )
