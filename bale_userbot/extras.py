"""What BaleClient 1.0.9 cannot send, or sends wrongly.

Location, contact and sticker live on the wire in `MessageContent` protobuf
fields the library does not declare (see the field keys in `bale_userbot.content`).
Two facts make them workable without forking: `BaleObject` keeps unknown
fields, and the protobuf encoder is schema-less, so a `MessageContent` built
with extra numeric keys serializes exactly like a native one. Incoming
payloads are parsed by `describe()`; this module is the sending side.

Reactions, the typing indicator and read receipts are here for a different
reason: the library's own methods build their peer from the raw chat type and
cannot address a bot chat, super-group or channel at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from baleclient import Client
from baleclient.enums import ChatType, TypingMode
from baleclient.methods import (
    MessageRead,
    MessageRemoveReaction,
    MessageSetReaction,
    SendMessage,
    StopTyping,
    Typing,
)
from baleclient.types import InfoMessage, Message, MessageContent, Reaction
from baleclient.utils import generate_id

from .content import (
    JSON_CONTENT_KEY,
    STICKER_CONTENT_KEY,
    StickerImage,
    StickerInfo,
    unwrap,
)

# --- content builders -------------------------------------------------------


def json_content(payload: dict) -> MessageContent:
    """A JSON message: how Bale apps carry structured non-file content."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return MessageContent.model_validate({JSON_CONTENT_KEY: {"1": text}})


def json_block_content(message: Message) -> MessageContent:
    """Copy a received JSON message (location, contact, …) verbatim.

    Rebuilding from `LocationInfo`/`ContactInfo` would drop anything bale-userbot
    does not model; the raw block keeps every field the sender wrote.
    """
    content, _ = unwrap(message.content)
    block = (content.model_extra or {}).get(JSON_CONTENT_KEY)
    if not isinstance(block, (dict, str)):
        raise ValueError(
            f"message {message.message_id} carries no JSON content to send"
        )
    return MessageContent.model_validate({JSON_CONTENT_KEY: block})


def location_content(latitude: float, longitude: float) -> MessageContent:
    return json_content(
        {
            "dataType": "location",
            "data": {"location": {"latitude": latitude, "longitude": longitude}},
        }
    )


def contact_content(
    name: str,
    phones: Iterable[str] = (),
    emails: Iterable[str] = (),
) -> MessageContent:
    return json_content(
        {
            "dataType": "contact",
            "data": {
                "contact": {
                    "name": name,
                    "emails": list(emails),
                    "phones": list(phones),
                }
            },
        }
    )


def _image_block(image: StickerImage) -> dict[str, Any]:
    block: dict[str, Any] = {
        # {file_id, access_hash, storage version} — as seen on the wire.
        "1": {"1": image.file_id, "2": image.access_hash, "3": {"1": 1}}
    }
    if image.width is not None:
        block["2"] = image.width
    if image.height is not None:
        block["3"] = image.height
    if image.size is not None:
        block["4"] = image.size
    return block


def sticker_content(sticker: Message | StickerInfo) -> MessageContent:
    """Sticker content, from a received message or a parsed `StickerInfo`.

    A `Message` is copied verbatim from its raw wire fields, which keeps
    subfields we do not model. Stickers reference an existing collection on
    the server; there is no way to upload a new one from here.
    """
    if isinstance(sticker, Message):
        content, _ = unwrap(sticker.content)
        block = (content.model_extra or {}).get(STICKER_CONTENT_KEY)
        if not isinstance(block, dict):
            raise ValueError(f"message {sticker.message_id} carries no sticker to send")
        return MessageContent.model_validate({STICKER_CONTENT_KEY: block})

    if sticker.image is None:
        raise ValueError("StickerInfo has no downloadable rendition to send")

    block = {}
    if sticker.sticker_id is not None:
        block["1"] = {"1": sticker.sticker_id}
    if sticker.image512 is not None:
        block["3"] = _image_block(sticker.image512)
    if sticker.image256 is not None:
        block["4"] = _image_block(sticker.image256)
    if sticker.collection_id is not None:
        block["5"] = {"1": sticker.collection_id}
    block["6"] = (
        {"1": sticker.collection_access_hash}
        if sticker.collection_access_hash is not None
        else {}
    )
    return MessageContent.model_validate({STICKER_CONTENT_KEY: block})


# --- sending ----------------------------------------------------------------


def _without_nones(value: Any) -> Any:
    """Drop None values from plain containers.

    `model_dump(exclude_none=True)` only prunes model fields; a None nested
    inside an extra dict reaches the protobuf encoder, which cannot type it
    and raises. Received payloads can carry them, so re-sending a copied
    block would otherwise fail.
    """
    if isinstance(value, dict):
        return {k: _without_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nones(v) for v in value if v is not None]
    return value


def _clean_extras(content: MessageContent) -> MessageContent:
    extra = content.model_extra or {}
    for key, value in list(extra.items()):
        setattr(content, key, _without_nones(value))
    return content


async def send_content(
    client: Client,
    content: MessageContent,
    chat_id: int,
    chat_type: ChatType,
    *,
    reply_to: Message | InfoMessage | None = None,
    message_id: int | None = None,
) -> Message:
    """Send an arbitrary `MessageContent` — the plumbing of `send_message`."""
    content = _clean_extras(content)
    chat = client._build_chat(chat_id, chat_type)
    peer = client._resolve_peer(chat)

    message_id = message_id or generate_id()
    client._ignored_messages.targets.append(message_id)

    if reply_to is not None:
        reply_to = client._ensure_info_message(reply_to)

    call = SendMessage(
        peer=peer,
        message_id=message_id,
        content=content,
        reply_to=reply_to,
        chat=chat,
    )
    result = await client(call)
    return result.message


async def send_location(
    client: Client,
    latitude: float,
    longitude: float,
    chat_id: int,
    chat_type: ChatType,
    *,
    reply_to: Message | InfoMessage | None = None,
) -> Message:
    """Share a map point."""
    return await send_content(
        client,
        location_content(latitude, longitude),
        chat_id,
        chat_type,
        reply_to=reply_to,
    )


async def send_contact(
    client: Client,
    name: str,
    phones: Iterable[str],
    chat_id: int,
    chat_type: ChatType,
    *,
    emails: Iterable[str] = (),
    reply_to: Message | InfoMessage | None = None,
) -> Message:
    """Share a contact card."""
    return await send_content(
        client,
        contact_content(name, phones, emails),
        chat_id,
        chat_type,
        reply_to=reply_to,
    )


async def send_sticker(
    client: Client,
    sticker: Message | StickerInfo,
    chat_id: int,
    chat_type: ChatType,
    *,
    reply_to: Message | InfoMessage | None = None,
) -> Message:
    """Send a sticker taken from a received message (or its `StickerInfo`)."""
    return await send_content(
        client, sticker_content(sticker), chat_id, chat_type, reply_to=reply_to
    )


# --- reactions and typing ---------------------------------------------------


async def react(
    client: Client,
    message: Message,
    emoji: str,
    *,
    remove: bool = False,
) -> list[Reaction]:
    """Add — or with `remove=True`, take back — a reaction on a message.

    BaleClient's own `set_reaction`/`remove_reaction` build
    `Peer(id=chat_id, type=chat_type)` from the raw chat type, but `PeerType`
    only defines UNKNOWN/PRIVATE/GROUP: in a bot chat, super-group or channel
    they raise a validation error before any network call. Resolving the peer
    the way the library does everywhere else makes them work in every chat.
    """
    chat = message.chat
    peer = client._resolve_peer(client._build_chat(chat.id, chat.type))
    call_type = MessageRemoveReaction if remove else MessageSetReaction
    result = await client(
        call_type(
            peer=peer,
            message_id=message.message_id,
            date=message.date,
            emojy=emoji,  # upstream spelling
        )
    )
    return result.reactions


async def unreact(client: Client, message: Message, emoji: str) -> list[Reaction]:
    """Take back a reaction previously added to a message."""
    return await react(client, message, emoji, remove=True)


async def mark_seen(
    client: Client,
    chat_id: int,
    chat_type: ChatType,
    *,
    date: int | None = None,
) -> Any:
    """Mark a chat as read — the other side's ticks turn.

    Same peer-resolution fix as `react()`: upstream `seen_chat` builds
    `Peer(id=chat_id, type=_resolve_peer_type(chat_type))`, and `PeerType` only
    defines UNKNOWN/PRIVATE/GROUP, so it cannot address a bot chat, super-group
    or channel.

    `date` marks everything up to that message's timestamp; without it the
    whole chat is marked read, which is what answering a message means.
    """
    peer = client._resolve_peer(client._build_chat(chat_id, chat_type))
    call = MessageRead(peer=peer, date=date) if date is not None else MessageRead(peer=peer)
    return await client(call)


async def set_typing(
    client: Client,
    chat_id: int,
    chat_type: ChatType,
    *,
    mode: TypingMode = TypingMode.TEXT,
    stop: bool = False,
) -> Any:
    """Show or clear the "typing…" indicator in a chat.

    Same peer-resolution fix as `react()`: upstream `typing`/`stop_typing`
    cannot address a bot chat, super-group or channel.
    """
    peer = client._resolve_peer(client._build_chat(chat_id, chat_type))
    call = (
        StopTyping(peer=peer, typing_type=mode)
        if stop
        else Typing(peer=peer, typing_type=mode)
    )
    return await client(call)
