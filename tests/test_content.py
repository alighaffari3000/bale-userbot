"""`describe()` over every content shape Bale can deliver."""

import pytest
from baleclient.enums import ChatType

from balekit import MessageKind, describe
from balekit.content import kind_of_document
from tests import factories as f


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        (f.text_content(), MessageKind.TEXT),
        (f.photo(), MessageKind.PHOTO),
        (f.video(), MessageKind.VIDEO),
        (f.gif(), MessageKind.GIF),
        (f.voice(), MessageKind.VOICE),
        (f.audio(), MessageKind.AUDIO),
        (f.plain_file(), MessageKind.DOCUMENT),
        (f.gift_content(), MessageKind.GIFT),
        (f.service_content(), MessageKind.SERVICE),
        (f.forward_content(), MessageKind.FORWARD),
    ],
)
def test_every_content_shape_is_classified(content, kind):
    assert describe(f.message(content)).kind is kind


def test_text_body_and_caption():
    text = describe(f.message(f.text_content("hello")))
    assert text.text == "hello"
    assert text.body == "hello"
    assert text.caption is None

    captioned = describe(f.message(f.photo(caption="a caption")))
    assert captioned.caption == "a caption"
    assert captioned.body == "a caption"
    assert captioned.text is None


def test_media_metadata_is_carried_over():
    info = describe(f.message(f.video()))
    assert info.is_media
    media = info.media
    assert (media.width, media.height, media.duration) == (1280, 720, 5000)
    assert media.mime_type == "video/mp4"
    assert media.name == "clip.mp4"
    assert media.size == 1234


def test_audio_tags_survive():
    media = describe(f.message(f.audio())).media
    assert (media.album, media.genre, media.track) == ("Album", "Rock", "Song")
    assert media.duration == 210000


def test_thumbnail_presence_is_reported():
    assert describe(f.message(f.photo(thumb=True))).media.has_thumb is True
    assert describe(f.message(f.photo())).media.has_thumb is False


def test_media_wrapped_in_a_keyboard_is_still_media():
    info = describe(f.message(f.with_keyboard(f.photo(caption="cap"))))
    assert info.kind is MessageKind.PHOTO
    assert info.has_keyboard is True
    assert info.caption == "cap"
    assert info.media.file_id == 9001


def test_text_wrapped_in_a_keyboard_is_still_text():
    info = describe(f.message(f.with_keyboard(f.text_content("press"))))
    assert info.kind is MessageKind.TEXT
    assert info.text == "press"
    assert info.has_keyboard is True


def test_chat_scope_flags():
    private = describe(f.message(f.text_content()))
    assert private.is_private is True

    group = describe(f.message(f.text_content(), chat_type=ChatType.GROUP))
    assert group.is_private is False
    assert group.chat_type is ChatType.GROUP


def test_non_media_kinds_have_no_media():
    for content in (f.text_content(), f.gift_content(), f.service_content()):
        info = describe(f.message(content))
        assert info.media is None
        assert info.is_media is False


# --- senders that omit the ext block --------------------------------------


@pytest.mark.parametrize(
    ("mime", "name", "kind"),
    [
        ("image/jpeg", "x.jpg", MessageKind.PHOTO),
        ("image/gif", "x.gif", MessageKind.GIF),
        ("video/mp4", "x.mp4", MessageKind.VIDEO),
        ("audio/mpeg", "x.mp3", MessageKind.AUDIO),
        ("application/pdf", "x.pdf", MessageKind.DOCUMENT),
        ("application/octet-stream", "x.gif", MessageKind.GIF),
        ("application/octet-stream", "x.bin", MessageKind.DOCUMENT),
    ],
)
def test_mime_fallback_when_ext_missing(mime, name, kind):
    assert kind_of_document(f.document(mime_type=mime, name=name, ext=None)) is kind


def test_dict_filename_does_not_crash():
    # The wire format sometimes puts a dict where the file name should be.
    document = f.document(name={"1": "weird"}, mime_type="image/jpeg", ext=None)
    info = describe(f.message(f.MessageContent(document=document)))
    assert info.kind is MessageKind.PHOTO
    assert info.media.name is None


def test_service_text_is_exposed():
    info = describe(f.message(f.service_content("someone joined")))
    assert info.service_text == "someone joined"
