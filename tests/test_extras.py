"""Sending location / contact / sticker through the raw-content door."""

import json
from types import SimpleNamespace

import pytest
from baleclient.enums import ChatType, PeerType
from baleclient.methods import SendMessage
from baleclient.types import InfoMessage, IntValue, Peer

from bale_userbot import MessageKind, describe
from bale_userbot.extras import (
    contact_content,
    location_content,
    send_contact,
    send_location,
    send_sticker,
    sticker_content,
)
from bale_userbot.media import resend
from tests import factories as f


class FakeSender:
    """Implements exactly the client surface `send_content` touches."""

    id = f.SELF_ID

    def __init__(self):
        self.sent: list[SendMessage] = []
        self._ignored_messages = SimpleNamespace(targets=[])

    def _build_chat(self, chat_id, chat_type):
        return f.chat(chat_id, chat_type)

    def _resolve_peer(self, chat):
        return Peer(id=chat.id, type=PeerType.PRIVATE)

    def _ensure_info_message(self, message, rewrite_date=False):
        # Faithful to Client._ensure_info_message: SendMessage.reply_to only
        # accepts an InfoMessage, so a fake that passes a Message straight
        # through would hide a real crash.
        if isinstance(message, InfoMessage):
            return message
        return InfoMessage(
            peer=self._resolve_peer(message.chat),
            message_id=message.message_id,
            date=message.date if rewrite_date else IntValue(value=message.date),
        )

    async def __call__(self, call):
        self.sent.append(call)
        return SimpleNamespace(message=f.message(call.content))


def content_wire(content) -> dict:
    """What the protobuf encoder will actually see."""
    return content.model_dump(exclude_none=True, by_alias=True)


# --- builders --------------------------------------------------------------


def test_location_content_matches_the_wire_shape():
    wire = content_wire(location_content(35.5, 51.25))
    payload = json.loads(wire["7"]["1"])
    assert payload == {
        "dataType": "location",
        "data": {"location": {"latitude": 35.5, "longitude": 51.25}},
    }


def test_contact_content_matches_the_wire_shape():
    wire = content_wire(contact_content("Ali", ["0912"], emails=["a@b.c"]))
    payload = json.loads(wire["7"]["1"])
    assert payload == {
        "dataType": "contact",
        "data": {"contact": {"name": "Ali", "emails": ["a@b.c"], "phones": ["0912"]}},
    }


def test_sticker_content_from_message_is_verbatim():
    message = f.message(f.sticker_content())
    wire = content_wire(sticker_content(message))
    assert wire["12"] == f.STICKER_WIRE


def test_sticker_content_from_sticker_info_round_trips():
    sticker = describe(f.message(f.sticker_content())).sticker
    rebuilt = describe(f.message(sticker_content(sticker))).sticker
    assert rebuilt == sticker


def test_sticker_content_needs_a_sticker():
    with pytest.raises(ValueError):
        sticker_content(f.message(f.text_content()))


def test_built_content_classifies_like_a_received_one():
    info = describe(f.message(location_content(1.0, 2.0)))
    assert info.kind is MessageKind.LOCATION

    info = describe(f.message(contact_content("Ali", ["0912"])))
    assert info.kind is MessageKind.CONTACT


# --- sending ---------------------------------------------------------------


async def test_send_location_builds_a_send_message_call():
    client = FakeSender()
    await send_location(client, 35.5, 51.25, f.PEER_ID, ChatType.PRIVATE)

    (call,) = client.sent
    assert isinstance(call, SendMessage)
    assert call.peer.id == f.PEER_ID
    assert json.loads(content_wire(call.content)["7"]["1"])["dataType"] == "location"
    # The echo of our own message must be recognised and dropped later.
    assert call.message_id in client._ignored_messages.targets


async def test_send_contact_and_sticker_reach_the_wire():
    client = FakeSender()
    await send_contact(client, "Ali", ["0912"], f.PEER_ID, ChatType.PRIVATE)
    sticker = f.message(f.sticker_content())
    await send_sticker(client, sticker, f.PEER_ID, ChatType.PRIVATE)

    contact_call, sticker_call = client.sent
    contact_payload = json.loads(content_wire(contact_call.content)["7"]["1"])
    assert contact_payload["dataType"] == "contact"
    assert content_wire(sticker_call.content)["12"] == f.STICKER_WIRE


async def test_resend_routes_stickers_through_raw_content():
    client = FakeSender()
    message = f.message(f.sticker_content())
    await resend(client, message, f.PEER_ID, ChatType.PRIVATE)

    (call,) = client.sent
    assert content_wire(call.content)["12"] == f.STICKER_WIRE
