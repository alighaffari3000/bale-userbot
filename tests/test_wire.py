"""Whole messages parsed from numeric-alias dicts — the real receive path.

Every other test builds models by field name. The wire never does: the
schema-less decoder hands pydantic nested dicts keyed by protobuf field
number, which is the path where BaleClient's validators do their damage.
These payloads are shaped exactly as captured from a live account.
"""

import json

from baleclient.enums import ChatType
from baleclient.types import Message

from bale_userbot import MessageKind, describe

CHAT = {"1": 1, "2": 1252665523}  # type=PRIVATE, id


def wire_message(content: dict, message_id: int = 123) -> Message:
    """A full Message exactly as the decoder produces it."""
    return Message.model_validate(
        {
            "1": CHAT,
            "2": 1252665523,
            "3": 1787755207104,
            "4": message_id,
            "5": content,
        }
    )


def test_text_message_from_the_wire():
    info = describe(wire_message({"15": {"1": "سلام"}, "5": 0}))
    assert info.kind is MessageKind.TEXT
    assert info.text == "سلام"
    assert info.chat_type is ChatType.PRIVATE
    assert info.chat_id == 1252665523
    assert info.message_id == 123


def test_photo_message_from_the_wire():
    info = describe(
        wire_message(
            {
                "4": {
                    "1": -7033344833870356733,
                    "2": 1252665523,
                    "3": 21876,
                    "4": "Screenshot.jpg",
                    "5": "image/jpeg",
                    "7": {"1": {"1": 540, "2": 1200}},
                    "8": {"1": "a caption"},
                    # Unmodelled fields real photos carry (checksum and co.).
                    "9": {"1": "checksum"},
                    "10": {"1": {"12": 7883679143952344940}},
                    "11": {"1": 1},
                },
                "5": 0,
            }
        )
    )
    assert info.kind is MessageKind.PHOTO
    assert info.caption == "a caption"
    assert (info.media.width, info.media.height) == (540, 1200)
    assert info.media.size == 21876
    assert info.media.name == "Screenshot.jpg"


def test_location_message_from_the_wire():
    payload = json.dumps(
        {
            "dataType": "location",
            "data": {
                "location": {
                    "latitude": 35.72580936520509,
                    "longitude": 51.440315432846546,
                }
            },
        }
    )
    info = describe(wire_message({"7": {"1": payload}, "5": 0}))
    assert info.kind is MessageKind.LOCATION
    assert round(info.location.latitude, 6) == 35.725809


def test_contact_message_from_the_wire():
    payload = json.dumps(
        {
            "dataType": "contact",
            "data": {
                "contact": {
                    "name": "Azadeh Mnch",
                    "emails": [],
                    "phones": ["09394790847"] * 5,
                }
            },
        }
    )
    info = describe(wire_message({"7": {"1": payload}, "5": 0}))
    assert info.kind is MessageKind.CONTACT
    assert info.contact.name == "Azadeh Mnch"
    assert info.contact.phones == ("09394790847",)  # deduplicated


def test_sticker_message_from_the_wire():
    info = describe(
        wire_message(
            {
                "12": {
                    "1": {"1": 2086508713},
                    "3": {
                        "1": {
                            "1": 5519507489348001536,
                            "2": 445871464,
                            "3": {"1": 1},
                        },
                        "2": 500,
                        "3": 500,
                        "4": 250629,
                    },
                    "4": {
                        "1": {
                            "1": 4466064368626310913,
                            "2": 445871464,
                            "3": {"1": 1},
                        },
                        "2": 250,
                        "3": 250,
                        "4": 87786,
                    },
                    "5": {"1": 772269187},
                    "6": {},
                },
                "5": 0,
            }
        )
    )
    assert info.kind is MessageKind.STICKER
    assert info.sticker.sticker_id == 2086508713
    assert info.sticker.collection_id == 772269187
    assert info.media.size == 250629


def test_forward_stub_from_the_wire():
    info = describe(wire_message({"5": 1}))
    assert info.kind is MessageKind.FORWARD
    assert info.is_forward is True


def test_a_message_inside_a_history_response_keeps_its_text():
    # HistoryResponse nests MessageData, whose own validator runs before
    # MessageContent's — the shape that first exposed the text loss.
    from baleclient.types.responses import HistoryResponse

    response = HistoryResponse.model_validate(
        {
            "1": [
                {
                    "1": 1252665523,
                    "2": 555,
                    "3": 1787755207104,
                    "4": {"15": {"1": "from history"}, "5": 0},
                }
            ]
        }
    )
    data = response.data[0]
    assert data.content.text is not None
    assert data.content.text.value == "from history"
