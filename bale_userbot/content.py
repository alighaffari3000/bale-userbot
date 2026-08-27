"""One vocabulary for "what kind of message is this?".

In Bale's protocol every attachment — photo, video, voice, music, gif, plain
file — arrives as the *same* `DocumentMessage`; only `document.ext` and the
MIME type tell them apart, and media sent with an inline keyboard is nested one
level deeper inside `content.bot_message`. `describe()` flattens all of that
into a single struct so callers never inspect protobuf field aliases.

A forward is the third shape that hides its payload: it arrives as an *empty*
content stub, and the text an agent is actually looking for rides in the quoted
message beside it. `describe()` surfaces that as `MessageInfo.quoted`, and
`body`/`searchable_text` read through it, so a forwarded advert is as findable
as one typed in place. In a busy channel-to-group relay that is half the
archive: without it a text search over `export_history` silently misses every
forwarded message.

Locations, contact cards and stickers have no model in `BaleClient 1.0.9` at
all: they ride in `MessageContent` protobuf fields the library does not
declare, and survive only because `BaleObject` keeps unknown fields in
`model_extra`. The field numbers below were discovered from live traffic with
`tools/probe_content.py`; `describe()` parses them like any first-class kind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from baleclient.enums import ChatType
from baleclient.types import DocumentMessage, Message, MessageContent

#: `MessageContent` wire fields missing from BaleClient 1.0.9's model.
#: Field 7 is a JSON message — `{"1": "<json>"}` with a `dataType`
#: discriminator; the Bale apps use it for locations and contact cards.
JSON_CONTENT_KEY = "7"
#: Field 12 is a sticker; see `StickerInfo` for its layout.
STICKER_CONTENT_KEY = "12"
#: Refuse to parse absurd JSON bodies: a hostile peer should not be able to
#: spend our CPU. Real locations and contact cards are a few hundred bytes.
MAX_JSON_PAYLOAD_BYTES = 64 * 1024


class MessageKind(StrEnum):
    """What a message actually carries."""

    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    GIF = "gif"
    VOICE = "voice"
    AUDIO = "audio"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACT = "contact"
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
        MessageKind.STICKER,
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
class LocationInfo:
    """A shared map point."""

    latitude: float
    longitude: float


@dataclass(frozen=True)
class ContactInfo:
    """A shared contact card. Duplicate phones/emails are collapsed."""

    name: str | None
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()


@dataclass(frozen=True)
class StickerImage:
    """One downloadable rendition of a sticker."""

    file_id: int
    access_hash: int
    width: int | None = None
    height: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class StickerInfo:
    """A sticker: an id inside a collection, plus up to two renditions."""

    sticker_id: int | None
    collection_id: int | None
    collection_access_hash: int | None
    image512: StickerImage | None = None
    image256: StickerImage | None = None

    @property
    def image(self) -> StickerImage | None:
        """The best rendition to download or re-send."""
        return self.image512 or self.image256


@dataclass(frozen=True)
class QuotedInfo:
    """The message another message hangs off.

    Two different relationships arrive in the same wire slot. A *reply* quotes
    an earlier message in the same chat, and the quote is context. A *forward*
    quotes the original it was copied from, and the quote is the whole point:
    the forwarding message itself carries no content at all.

    `chat_id` is where the quoted message lives, which for a forward is the
    chat it was copied out of — not the chat it landed in.
    """

    message_id: int | None
    sender_id: int | None
    date: int | None
    chat_id: int | None
    kind: MessageKind
    text: str | None = None
    caption: str | None = None
    media: MediaInfo | None = None

    @property
    def body(self) -> str | None:
        """Text of a quoted text message, or the caption of quoted media."""
        return self.text if self.kind is MessageKind.TEXT else self.caption


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
    location: LocationInfo | None = None
    contact: ContactInfo | None = None
    sticker: StickerInfo | None = None
    #: The message this one replies to or was forwarded from, when the wire
    #: carried it. For a forward this holds the content; see `body`.
    quoted: QuotedInfo | None = None
    #: The parsed body of a JSON message (wire field 7), whatever its
    #: dataType. Lets applications handle types bale-userbot does not know yet.
    json_payload: dict | None = None

    @property
    def is_media(self) -> bool:
        return self.kind in MEDIA_KINDS

    @property
    def is_private(self) -> bool:
        return self.chat_type in (ChatType.PRIVATE, ChatType.BOT)

    @property
    def body(self) -> str | None:
        """Text of a text message, or the caption of a media message.

        A forward has neither of its own: its body is the quoted original's,
        which is what a reader — or a search — means by "what does it say".
        """
        if self.kind is MessageKind.FORWARD:
            return self.quoted.body if self.quoted is not None else None
        return self.text if self.kind is MessageKind.TEXT else self.caption

    @property
    def searchable_text(self) -> str:
        """Every human-readable string this message contributes, joined.

        A *reply's* quote is deliberately left out. It is someone else's words
        in a message that merely points at them, and folding it in would make
        every reply match every search that its parent matched.
        """
        parts = (self.text, self.caption, self.service_text)
        joined = [part for part in parts if part]
        if self.kind is MessageKind.FORWARD and self.quoted is not None:
            quote = (self.quoted.text, self.quoted.caption)
            joined.extend(part for part in quote if part)
        return "\n".join(joined)


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


def _extra(content: MessageContent) -> dict:
    """Wire fields the model does not declare; pydantic keeps them here."""
    return content.model_extra or {}


def _wrapped_int(value: object) -> int | None:
    """Bale wraps scalars as `{"1": n}`; an empty dict means "not set".

    The same number arrives as a bare `int` on some models and as an
    `IntValue` on others — `QuotedMessage.message_id` is an `IntValue` while
    `Message.message_id` beside it is plain — so all three shapes are read.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        inner = value.get("1")
        return inner if isinstance(inner, int) else None
    inner = getattr(value, "value", None)
    return inner if isinstance(inner, int) else None


def _sticker_image(block: object) -> StickerImage | None:
    # {"1": {"1": file_id, "2": access_hash, ...}, "2": w, "3": h, "4": size}
    if not isinstance(block, dict):
        return None
    location = block.get("1")
    if not isinstance(location, dict):
        return None
    file_id = location.get("1")
    access_hash = location.get("2")
    if not isinstance(file_id, int) or not isinstance(access_hash, int):
        return None
    return StickerImage(
        file_id=file_id,
        access_hash=access_hash,
        width=block.get("2") if isinstance(block.get("2"), int) else None,
        height=block.get("3") if isinstance(block.get("3"), int) else None,
        size=block.get("4") if isinstance(block.get("4"), int) else None,
    )


def _sticker_of(block: object) -> StickerInfo | None:
    # {"1": {id}, "3": image512, "4": image256, "5": {collection}, "6": {hash}}
    if not isinstance(block, dict):
        return None
    info = StickerInfo(
        sticker_id=_wrapped_int(block.get("1")),
        collection_id=_wrapped_int(block.get("5")),
        collection_access_hash=_wrapped_int(block.get("6")),
        image512=_sticker_image(block.get("3")),
        image256=_sticker_image(block.get("4")),
    )
    return info if info.image is not None else None


def _json_payload_of(block: object) -> dict | None:
    """The parsed body of a JSON message (`{"1": "<json>"}`), or None.

    The schema-less decoder can collapse a one-field submessage into its
    bare string, so both shapes are accepted.
    """
    text = block.get("1") if isinstance(block, dict) else block
    if not isinstance(text, str) or len(text) > MAX_JSON_PAYLOAD_BYTES:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        # Deeply nested JSON raises RecursionError, not ValueError, and any
        # peer can send it; describe() promises never to raise.
        return None
    return payload if isinstance(payload, dict) else None


def _unique_strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(dict.fromkeys(v for v in values if isinstance(v, str) and v))


def _location_of(data: dict) -> LocationInfo | None:
    point = data.get("location")
    if not isinstance(point, dict):
        return None
    latitude, longitude = point.get("latitude"), point.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(
        longitude, (int, float)
    ):
        return None
    return LocationInfo(latitude=float(latitude), longitude=float(longitude))


def _contact_of(data: dict) -> ContactInfo | None:
    card = data.get("contact")
    if not isinstance(card, dict):
        return None
    name = card.get("name")
    return ContactInfo(
        name=name if isinstance(name, str) and name else None,
        phones=_unique_strings(card.get("phones")),
        emails=_unique_strings(card.get("emails")),
    )


def unwrap(content: MessageContent) -> tuple[MessageContent, bool]:
    """Return the payload content and whether it came wrapped in a keyboard."""
    if content.bot_message is not None and content.bot_message.message is not None:
        return content.bot_message.message, True
    return content, False


@dataclass(frozen=True)
class _Parsed:
    """What one `MessageContent` turned out to hold."""

    kind: MessageKind
    has_keyboard: bool = False
    text: str | None = None
    caption: str | None = None
    media: MediaInfo | None = None
    service_text: str | None = None
    location: LocationInfo | None = None
    contact: ContactInfo | None = None
    sticker: StickerInfo | None = None
    json_payload: dict | None = None


def parse_content(raw: MessageContent) -> _Parsed:
    """Classify one content block. Shared by a message and its quote."""
    content, has_keyboard = unwrap(raw)

    kind = MessageKind.UNKNOWN
    text = caption = service_text = None
    media = location = contact = sticker = json_payload = None

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
    elif (
        payload := _json_payload_of(_extra(content).get(JSON_CONTENT_KEY))
    ) is not None:
        json_payload = payload
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        if payload.get("dataType") == "location":
            location = _location_of(data)
            kind = MessageKind.LOCATION if location else MessageKind.UNKNOWN
        elif payload.get("dataType") == "contact":
            contact = _contact_of(data)
            kind = MessageKind.CONTACT if contact else MessageKind.UNKNOWN
    elif (sticker := _sticker_of(_extra(content).get(STICKER_CONTENT_KEY))) is not None:
        kind = MessageKind.STICKER
        image = sticker.image
        media = MediaInfo(
            file_id=image.file_id,
            access_hash=image.access_hash,
            # The wire carries no MIME for stickers; renditions are
            # PNG in practice (verified by downloading one).
            mime_type="image/png",
            size=image.size,
            width=image.width,
            height=image.height,
        )
    elif content.empty:
        # An empty stub with a quoted message is how a forward arrives.
        kind = MessageKind.FORWARD

    return _Parsed(
        kind=kind,
        has_keyboard=has_keyboard,
        text=text,
        caption=caption,
        media=media,
        service_text=service_text,
        location=location,
        contact=contact,
        sticker=sticker,
        json_payload=json_payload,
    )


def _quote_of(message: Message) -> QuotedInfo | None:
    """The quoted message beside this one, whichever slot carried it.

    `replied_to` is a whole `Message` and `quoted_replied_to` a trimmed
    `QuotedMessage`; both hold the same content, so either will do. The quoted
    one is preferred because it names the *origin* chat — for a forward, the
    chat the message was copied out of, which `replied_to` does not record.
    """
    quoted = message.quoted_replied_to
    if quoted is not None:
        peer = getattr(quoted, "peer", None)
        parsed = parse_content(quoted.content)
        chat_id = getattr(peer, "id", None)
    else:
        replied = message.replied_to
        if replied is None:
            return None
        parsed = parse_content(replied.content)
        chat_id = getattr(getattr(replied, "chat", None), "id", None)
        quoted = replied

    return QuotedInfo(
        message_id=_wrapped_int(getattr(quoted, "message_id", None)),
        sender_id=getattr(quoted, "sender_id", None),
        date=getattr(quoted, "date", None),
        chat_id=chat_id if isinstance(chat_id, int) else None,
        kind=parsed.kind,
        text=parsed.text,
        caption=parsed.caption,
        media=parsed.media,
    )


def describe(message: Message) -> MessageInfo:
    """Flatten a `Message` into a `MessageInfo`. Never raises on odd payloads."""
    parsed = parse_content(message.content)
    quoted = _quote_of(message)

    # BaleObject sets use_enum_values, so chat.type arrives as a plain int.
    try:
        chat_type = ChatType(message.chat.type)
    except ValueError:
        chat_type = ChatType.UNKNOWN

    return MessageInfo(
        kind=parsed.kind,
        message_id=message.message_id,
        chat_id=message.chat.id,
        chat_type=chat_type,
        sender_id=message.sender_id,
        date=message.date,
        text=parsed.text,
        caption=parsed.caption,
        media=parsed.media,
        has_keyboard=parsed.has_keyboard,
        is_forward=parsed.kind is MessageKind.FORWARD,
        reply_to_id=quoted.message_id if quoted is not None else None,
        service_text=parsed.service_text,
        location=parsed.location,
        contact=parsed.contact,
        sticker=parsed.sticker,
        quoted=quoted,
        json_payload=parsed.json_payload,
    )
