"""Builders for real `baleclient` message objects, one per content shape."""

from __future__ import annotations

import json

from baleclient.enums import ChatType
from baleclient.types import (
    AudioExt,
    Chat,
    DocumentMessage,
    DocumentsExt,
    GiftPacket,
    Message,
    MessageCaption,
    MessageContent,
    PhotoExt,
    StringValue,
    TemplateMessage,
    TextMessage,
    Thumbnail,
    VideoExt,
    VoiceExt,
)
from baleclient.types.service_ext import ServiceExt
from baleclient.types.service_message import ServiceMessage

SELF_ID = 111
PEER_ID = 222


def chat(chat_id: int = PEER_ID, chat_type: ChatType = ChatType.PRIVATE) -> Chat:
    return Chat(type=chat_type, id=chat_id)


def message(
    content: MessageContent,
    *,
    sender_id: int = PEER_ID,
    chat_id: int = PEER_ID,
    chat_type: ChatType = ChatType.PRIVATE,
    message_id: int = 1,
) -> Message:
    return Message(
        chat=chat(chat_id, chat_type),
        sender_id=sender_id,
        date=1_700_000_000_000,
        message_id=message_id,
        content=content,
    )


def text_content(value: str = "سلام") -> MessageContent:
    return MessageContent(text=TextMessage(value=value))


def document(
    *,
    mime_type: str = "application/pdf",
    name: str | None = "report.pdf",
    ext: DocumentsExt | None = None,
    caption: str | None = None,
    thumb: bool = False,
    size: int = 1234,
) -> DocumentMessage:
    return DocumentMessage(
        file_id=9001,
        access_hash=4242,
        size=size,
        name=name,
        mime_type=mime_type,
        ext=ext,
        caption=MessageCaption(content=caption) if caption is not None else None,
        thumb=Thumbnail(w=50, h=50, image=b"\x00" * 16) if thumb else None,
    )


def photo(caption: str | None = None, thumb: bool = False) -> MessageContent:
    return MessageContent(
        document=document(
            mime_type="image/jpeg",
            name="pic.jpg",
            ext=DocumentsExt(photo=PhotoExt(w=800, h=600)),
            caption=caption,
            thumb=thumb,
        )
    )


def video(duration: int = 5000) -> MessageContent:
    return MessageContent(
        document=document(
            mime_type="video/mp4",
            name="clip.mp4",
            ext=DocumentsExt(video=VideoExt(w=1280, h=720, duration=duration)),
        )
    )


def gif() -> MessageContent:
    return MessageContent(
        document=document(
            mime_type="image/gif",
            name="loop.gif",
            ext=DocumentsExt(gif=VideoExt(w=320, h=240, duration=2000)),
        )
    )


def voice(duration: int = 3000) -> MessageContent:
    return MessageContent(
        document=document(
            mime_type="audio/ogg",
            name="voice.ogg",
            ext=DocumentsExt(voice=VoiceExt(duration=duration)),
        )
    )


def audio() -> MessageContent:
    return MessageContent(
        document=document(
            mime_type="audio/mpeg",
            name="song.mp3",
            # Through aliases: AudioExt drops tags passed by field name.
            ext=DocumentsExt(
                audio=AudioExt.model_validate(
                    {"1": 210000, "2": "Album", "3": "Rock", "4": "Song"}
                )
            ),
        )
    )


def plain_file() -> MessageContent:
    return MessageContent(document=document())


def gift_content() -> MessageContent:
    return MessageContent(
        gift=GiftPacket(
            count=1,
            total_amount=1000,
            message=StringValue(value="happy"),
            owner_id=PEER_ID,
        )
    )


def service_content(text: str = "user joined") -> MessageContent:
    return MessageContent(service_message=ServiceMessage(text=text, ext=ServiceExt()))


def forward_content() -> MessageContent:
    # A forward arrives as an empty stub; the payload rides in the quoted message.
    return MessageContent.model_validate({"5": True})


def location_json(latitude: float = 35.7258, longitude: float = 51.4403) -> str:
    return json.dumps(
        {
            "dataType": "location",
            "data": {"location": {"latitude": latitude, "longitude": longitude}},
        }
    )


def contact_json() -> str:
    # Phones arrive duplicated from the app, exactly as captured on the wire.
    return json.dumps(
        {
            "dataType": "contact",
            "data": {
                "contact": {
                    "name": "Azadeh",
                    "emails": [],
                    "phones": ["0939", "0939", "0912"],
                }
            },
        }
    )


def json_content(text: str) -> MessageContent:
    """A raw JSON message, as decoded off the wire (content field 7)."""
    return MessageContent.model_validate({"7": {"1": text}})


#: A sticker exactly as captured from live traffic (content field 12).
STICKER_WIRE = {
    "1": {"1": 2086508713},
    "3": {
        "1": {"1": 5519507489348001536, "2": 445871464, "3": {"1": 1}},
        "2": 500,
        "3": 500,
        "4": 250629,
    },
    "4": {
        "1": {"1": 4466064368626310913, "2": 445871464, "3": {"1": 1}},
        "2": 250,
        "3": 250,
        "4": 87786,
    },
    "5": {"1": 772269187},
    "6": {},
}


def sticker_content() -> MessageContent:
    return MessageContent.model_validate({"12": STICKER_WIRE})


def with_keyboard(inner: MessageContent) -> MessageContent:
    return MessageContent(bot_message=TemplateMessage(message=inner))
