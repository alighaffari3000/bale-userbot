"""Defensive behaviour: odd wire shapes, hostile payloads, bad config.

Every case here was a real crash or silent data loss before the fix.
"""

import json
import logging

import pytest
from baleclient.enums import ChatType
from baleclient.types import DocumentMessage, MessageContent, Thumbnail

from bale_userbot import Config, MessageKind, describe
from bale_userbot.logging_setup import setup_logging
from bale_userbot.media import _thumb_bytes, resend
from tests import factories as f
from tests.test_extras import FakeSender, content_wire
from tests.test_media import FakeClient


class FakeBoth(FakeSender, FakeClient):
    """Records both the send_* methods and raw send_content calls."""

    def __init__(self):
        FakeSender.__init__(self)
        FakeClient.__init__(self)


def wire(data: dict) -> MessageContent:
    return MessageContent.model_validate(data)


# --- the patched validator must never make a message unparseable -----------


@pytest.mark.parametrize(
    "text_field",
    [
        {},               # empty submessage
        {"2": 3},         # submessage without the value field
        {"1": 123},       # value present but not a string
        5,                # not a submessage at all
        [],
    ],
)
def test_unbuildable_text_shapes_degrade_instead_of_raising(text_field):
    # TextMessage.value is a required str: leaving these shapes in place made
    # the whole Message fail validation, and on the websocket path the entire
    # update was silently dropped.
    info = describe(f.message(wire({"15": text_field})))
    assert info.kind is MessageKind.UNKNOWN
    assert info.text is None


def test_empty_submessage_flag_still_means_forward():
    # blackboxprotobuf renders an empty length-delimited field as {}. Upstream
    # treated any presence of field 5 as "empty stub"; so must the patch.
    assert describe(f.message(wire({"5": {}}))).kind is MessageKind.FORWARD


def test_service_message_without_ext_does_not_kill_the_message():
    # ServiceMessage requires both text and ext; a missing ext used to raise
    # out of MessageContent and take the whole update with it.
    info = describe(f.message(wire({"11": {"1": "joined the group"}})))
    assert info.kind is MessageKind.UNKNOWN
    assert info.service_text is None


def test_incomplete_thumbnail_does_not_kill_the_document():
    # Thumbnail requires w and h, but normalize_thumb keeps a thumb dict that
    # only carries an image — the document then failed validation entirely.
    info = describe(
        f.message(
            wire(
                {
                    "4": {
                        "1": 7,
                        "2": 8,
                        "4": "a.pdf",
                        "5": "application/pdf",
                        "6": {"3": {"10": "https://cdn.example/thumb.jpg"}},
                    }
                }
            )
        )
    )
    assert info.kind is MessageKind.DOCUMENT
    assert info.media.file_id == 7
    assert info.media.has_thumb is False


# --- hostile JSON payloads -------------------------------------------------


def test_deeply_nested_json_never_raises():
    # json.loads raises RecursionError (not ValueError) on this; describe()
    # promises never to raise, and filters run outside the handler's guard.
    payload = "[" * 5000 + "]" * 5000
    info = describe(f.message(f.json_content(payload)))
    assert info.kind is MessageKind.UNKNOWN


def test_oversized_json_payload_is_refused_without_parsing():
    payload = json.dumps({"dataType": "location", "pad": "x" * 70_000})
    info = describe(f.message(f.json_content(payload)))
    assert info.kind is MessageKind.UNKNOWN
    assert info.json_payload is None


# --- thumbnails that are URLs, not bytes -----------------------------------


def _document_with_thumb(image) -> DocumentMessage:
    return DocumentMessage(
        file_id=1,
        access_hash=2,
        size=3,
        name="pic.jpg",
        mime_type="image/jpeg",
        thumb=Thumbnail(w=50, h=50, image=image),
    )


def test_url_thumbnail_is_skipped_not_passed_to_fileinput():
    # FileInput reads any str as a filesystem path and raises on a URL.
    assert _thumb_bytes(_document_with_thumb("https://cdn.example/t.jpg")) is None
    assert _thumb_bytes(_document_with_thumb(b"")) is None
    assert _thumb_bytes(_document_with_thumb(b"\x00\x01")) == b"\x00\x01"


async def test_resend_of_a_photo_with_a_url_thumbnail_does_not_crash():
    client = FakeBoth()
    content = MessageContent(
        document=_document_with_thumb("https://cdn.example/t.jpg")
    )
    await resend(client, f.message(content), f.PEER_ID, ChatType.PRIVATE)
    assert client.calls[0][0] == "send_photo"
    assert "cover_thumb" not in client.calls[0][1]


# --- resend of the JSON kinds ---------------------------------------------


async def test_resend_copies_a_location_verbatim():
    client = FakeSender()
    message = f.message(f.json_content(f.location_json(10.5, 20.5)))
    await resend(client, message, f.PEER_ID, ChatType.PRIVATE)

    (call,) = client.sent
    payload = json.loads(content_wire(call.content)["7"]["1"])
    assert payload["data"]["location"] == {"latitude": 10.5, "longitude": 20.5}


async def test_resend_copies_a_contact_verbatim_keeping_unmodelled_fields():
    client = FakeSender()
    raw = json.dumps(
        {
            "dataType": "contact",
            "data": {"contact": {"name": "Ali", "phones": ["0912"], "note": "vip"}},
        }
    )
    await resend(client, f.message(f.json_content(raw)), f.PEER_ID, ChatType.PRIVATE)

    (call,) = client.sent
    payload = json.loads(content_wire(call.content)["7"]["1"])
    # "note" is not in ContactInfo; a verbatim copy must still carry it.
    assert payload["data"]["contact"]["note"] == "vip"


# --- config and logging ----------------------------------------------------


def test_session_path_is_normalized_to_the_file_baleclient_uses(tmp_path):
    # BaleClient rewrites any non-.bale suffix; bale-userbot must check the same
    # file or login writes one path and start() looks for another.
    assert Config(session_file=tmp_path / "mysession").session_file.name == (
        "mysession.bale"
    )
    assert Config(session_file=tmp_path / "s.dat").session_file.name == "s.bale"
    kept = tmp_path / "already.bale"
    assert Config(session_file=kept).session_file == kept


@pytest.mark.parametrize("level", ["debug", "Info", "WARNING", "nonsense"])
def test_setup_logging_accepts_any_case_and_bad_names(level):
    # getattr(logging, "debug") finds the *function*, which basicConfig
    # rejects with TypeError.
    setup_logging(level)
    assert isinstance(logging.getLogger().level, int)
