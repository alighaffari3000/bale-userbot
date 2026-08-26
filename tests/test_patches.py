"""The wire-fix for MessageContent._check_empty (see bale_userbot/patches.py).

These dicts are exactly what the schema-less protobuf decoder hands pydantic
for received messages — numeric keys. Without the patch, text is nulled and
`empty` is forced True by mere presence of the field.
"""

from baleclient.types import MessageContent

from bale_userbot import MessageKind, describe
from tests import factories as f


def content_from_wire(data: dict) -> MessageContent:
    return MessageContent.model_validate(data)


def test_received_text_survives():
    # Our own encoder writes an explicit empty=0, so both fields are present.
    info = describe(f.message(content_from_wire({"15": {"1": "hello"}, "5": 0})))
    assert info.kind is MessageKind.TEXT
    assert info.text == "hello"
    assert info.is_forward is False


def test_received_text_as_bare_string_survives():
    # The decoder sometimes collapses a one-field submessage into its string.
    info = describe(f.message(content_from_wire({"15": "hello"})))
    assert info.kind is MessageKind.TEXT
    assert info.text == "hello"


def test_forward_stub_still_detected():
    info = describe(f.message(content_from_wire({"5": 1})))
    assert info.kind is MessageKind.FORWARD


def test_explicit_empty_zero_is_not_a_forward():
    info = describe(f.message(content_from_wire({"5": 0})))
    assert info.kind is MessageKind.UNKNOWN
    assert info.is_forward is False


def test_unparseable_text_shape_never_raises():
    info = describe(f.message(content_from_wire({"15": 5})))
    assert info.kind is MessageKind.UNKNOWN
    assert info.text is None


def test_wire_document_with_text_absent_still_classifies():
    info = describe(
        f.message(
            content_from_wire(
                {"4": {"1": 1, "2": 2, "5": "application/pdf", "4": "a.pdf"}, "5": 0}
            )
        )
    )
    assert info.kind is MessageKind.DOCUMENT
