"""`describe()` over every content shape Bale can deliver."""

import pytest
from baleclient.enums import ChatType

from bale_userbot import MessageKind, describe
from bale_userbot.content import kind_of_document
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
        (f.json_content(f.location_json()), MessageKind.LOCATION),
        (f.json_content(f.contact_json()), MessageKind.CONTACT),
        (f.sticker_content(), MessageKind.STICKER),
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


# --- location / contact / sticker (wire fields BaleClient does not model) --


def test_location_coordinates_are_parsed():
    info = describe(f.message(f.json_content(f.location_json(35.5, 51.25))))
    assert info.kind is MessageKind.LOCATION
    assert (info.location.latitude, info.location.longitude) == (35.5, 51.25)
    assert not info.is_media


def test_contact_card_is_parsed_and_deduplicated():
    info = describe(f.message(f.json_content(f.contact_json())))
    assert info.kind is MessageKind.CONTACT
    assert info.contact.name == "Azadeh"
    assert info.contact.phones == ("0939", "0912")
    assert info.contact.emails == ()


def test_unknown_json_data_type_stays_unknown():
    import json

    payload = json.dumps({"dataType": "poll", "data": {}})
    info = describe(f.message(f.json_content(payload)))
    assert info.kind is MessageKind.UNKNOWN


def test_json_content_collapsed_to_bare_string_still_parses():
    # The schema-less decoder may hand field 7 as a string, not {"1": str}.
    from baleclient.types import MessageContent

    content = MessageContent.model_validate({"7": f.location_json(1.5, 2.5)})
    info = describe(f.message(content))
    assert info.kind is MessageKind.LOCATION
    assert info.location.latitude == 1.5


def test_unknown_data_type_exposes_json_payload():
    import json

    payload = json.dumps({"dataType": "poll", "data": {"q": "?"}})
    info = describe(f.message(f.json_content(payload)))
    assert info.kind is MessageKind.UNKNOWN
    assert info.json_payload == {"dataType": "poll", "data": {"q": "?"}}


def test_invalid_json_payload_never_raises():
    info = describe(f.message(f.json_content("{not json")))
    assert info.kind is MessageKind.UNKNOWN


def test_sticker_fields_and_media_mapping():
    info = describe(f.message(f.sticker_content()))
    assert info.kind is MessageKind.STICKER
    sticker = info.sticker
    assert sticker.sticker_id == 2086508713
    assert sticker.collection_id == 772269187
    assert sticker.collection_access_hash is None  # "6": {} means unset
    assert (sticker.image512.width, sticker.image512.height) == (500, 500)
    assert (sticker.image256.width, sticker.image256.height) == (250, 250)
    assert sticker.image is sticker.image512

    # Stickers are downloadable media: the 512px rendition backs `media`.
    assert info.is_media
    assert info.media.file_id == sticker.image512.file_id
    assert info.media.access_hash == sticker.image512.access_hash
    assert info.media.mime_type == "image/png"
    assert info.media.size == 250629
