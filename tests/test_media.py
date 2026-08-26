"""send_media / resend / download, against a client that records calls."""

from pathlib import Path

import pytest
from baleclient.enums import ChatType
from baleclient.types import DocumentMessage, FileInput

from balekit import MessageKind, detect_kind, suggest_filename
from balekit.content import MediaInfo, describe
from balekit.media import audio_ext, download, resend, send_media
from tests import factories as f


class FakeClient:
    """Records which send_* method was called and with what."""

    id = f.SELF_ID

    def __init__(self):
        self.calls = []
        self.downloads = []

    def _record(self, name):
        async def call(**kwargs):
            self.calls.append((name, kwargs))
            return f.message(f.text_content("sent"))

        return call

    def __getattr__(self, name):
        if name.startswith("send_") or name == "_send_file_message":
            return self._record(name)
        raise AttributeError(name)

    async def download_file(self, file_id, access_hash, destination=None, seek=True):
        self.downloads.append((file_id, access_hash, destination))
        if destination is None:
            import io

            return io.BytesIO(b"payload")
        if isinstance(destination, (str, Path)):
            Path(destination).write_bytes(b"payload")
            return None
        destination.write(b"payload")
        return destination


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


# --- kind detection -------------------------------------------------------


@pytest.mark.parametrize(
    ("mime", "name", "kind"),
    [
        ("image/jpeg", "a.jpg", MessageKind.PHOTO),
        ("image/gif", "a.gif", MessageKind.GIF),
        ("video/mp4", "a.mp4", MessageKind.VIDEO),
        ("audio/ogg", "a.ogg", MessageKind.AUDIO),
        ("application/pdf", "a.pdf", MessageKind.DOCUMENT),
        (None, "a.png", MessageKind.PHOTO),
        (None, "a.unknown", MessageKind.DOCUMENT),
    ],
)
def test_detect_kind(mime, name, kind):
    assert detect_kind(mime, name) is kind


# --- send_media routing ---------------------------------------------------


async def test_each_kind_reaches_its_send_method(tmp_path):
    client = FakeClient()
    files = {
        "a.png": ("send_photo", PNG),
        "a.gif": ("send_gif", b"GIF89a"),
        "a.mp4": ("send_video", b"\x00\x00\x00\x18ftypisom"),
        "a.mp3": ("send_audio", b"ID3\x03\x00"),
        "a.pdf": ("send_document", b"%PDF-1.4"),
    }
    for name, (expected, payload) in files.items():
        path = tmp_path / name
        path.write_bytes(payload)
        await send_media(client, path, 5, ChatType.PRIVATE)
        assert client.calls[-1][0] == expected, name


async def test_explicit_kind_overrides_detection(tmp_path):
    client = FakeClient()
    path = tmp_path / "note.ogg"
    path.write_bytes(b"OggS")
    # Same file, once as music and once as a voice note.
    await send_media(client, path, 5, ChatType.PRIVATE)
    await send_media(client, path, 5, ChatType.PRIVATE, kind=MessageKind.VOICE)
    assert [c[0] for c in client.calls] == ["send_audio", "send_voice"]


async def test_photo_dimensions_and_caption_are_passed(tmp_path):
    client = FakeClient()
    path = tmp_path / "one.png"
    path.write_bytes(PNG)
    await send_media(client, path, 7, ChatType.PRIVATE, caption="hi")
    name, kwargs = client.calls[-1]
    assert name == "send_photo"
    assert kwargs["caption"] == "hi"
    assert kwargs["chat_id"] == 7
    # Pillow is optional; dimensions only appear when it is installed.
    if "cover_width" in kwargs:
        assert kwargs["cover_width"] == 1


async def test_bytes_input_is_accepted():
    client = FakeClient()
    await send_media(client, PNG, 1, ChatType.PRIVATE, name="x.png")
    assert client.calls[-1][0] == "send_photo"


async def test_voice_duration_is_forwarded(tmp_path):
    client = FakeClient()
    path = tmp_path / "v.ogg"
    path.write_bytes(b"OggS")
    await send_media(
        client, path, 1, ChatType.PRIVATE, kind=MessageKind.VOICE, duration=4200
    )
    assert client.calls[-1][1]["duration"] == 4200


# --- the AudioExt tag bug -------------------------------------------------


def test_audio_ext_keeps_tags_through_aliases():
    ext = audio_ext(1000, "Album", "Rock", "Song")
    assert (ext.audio.album, ext.audio.genre, ext.audio.track) == (
        "Album",
        "Rock",
        "Song",
    )


def test_upstream_audioext_still_drops_tags_by_field_name():
    # Guards the workaround: if this ever fails, BaleClient fixed the bug and
    # `audio_ext()` can go away.
    from baleclient.types import AudioExt

    assert AudioExt(duration=1, album="Album").album is None


async def test_tagged_audio_bypasses_send_audio(tmp_path):
    client = FakeClient()
    path = tmp_path / "s.mp3"
    path.write_bytes(b"ID3\x03\x00")
    await send_media(client, path, 1, ChatType.PRIVATE, album="Album", track="Song")
    name, kwargs = client.calls[-1]
    assert name == "_send_file_message"
    assert kwargs["ext"].audio.album == "Album"


async def test_untagged_audio_uses_the_public_method(tmp_path):
    client = FakeClient()
    path = tmp_path / "s.mp3"
    path.write_bytes(b"ID3\x03\x00")
    await send_media(client, path, 1, ChatType.PRIVATE)
    assert client.calls[-1][0] == "send_audio"


# --- resend ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "method"),
    [
        (f.photo(), "send_photo"),
        (f.video(), "send_video"),
        (f.gif(), "send_gif"),
        (f.voice(), "send_voice"),
        (f.plain_file(), "send_document"),
    ],
)
async def test_resend_uses_the_matching_method(content, method):
    client = FakeClient()
    await resend(client, f.message(content), 42, ChatType.PRIVATE)
    name, kwargs = client.calls[-1]
    assert name == method
    # A pointer copy: the original document object is passed straight through.
    assert isinstance(next(iter(kwargs.values())), DocumentMessage)


async def test_resend_carries_metadata_over():
    client = FakeClient()
    await resend(client, f.message(f.video()), 42, ChatType.PRIVATE)
    kwargs = client.calls[-1][1]
    assert (kwargs["cover_width"], kwargs["cover_height"]) == (1280, 720)
    assert kwargs["duration"] == 5000


async def test_resend_keeps_the_original_caption_and_thumb():
    client = FakeClient()
    message = f.message(f.photo(caption="cap", thumb=True))
    await resend(client, message, 1, ChatType.PRIVATE)
    kwargs = client.calls[-1][1]
    assert kwargs["caption"] == "cap"
    assert isinstance(kwargs["cover_thumb"], FileInput)


async def test_resend_caption_can_be_overridden():
    client = FakeClient()
    message = f.message(f.photo(caption="old"))
    await resend(client, message, 1, ChatType.PRIVATE, caption="new")
    assert client.calls[-1][1]["caption"] == "new"


async def test_resend_rejects_a_text_message():
    client = FakeClient()
    with pytest.raises(ValueError, match="no file"):
        await resend(client, f.message(f.text_content()), 1, ChatType.PRIVATE)


# --- download -------------------------------------------------------------


async def test_download_returns_bytes_without_destination():
    client = FakeClient()
    assert await download(client, f.message(f.photo())) == b"payload"


async def test_download_into_a_directory_names_the_file(tmp_path):
    client = FakeClient()
    path = await download(client, f.message(f.photo()), tmp_path)
    assert path.name == "pic.jpg"
    assert path.read_bytes() == b"payload"


async def test_download_creates_a_name_when_the_sender_gave_none(tmp_path):
    client = FakeClient()
    content = f.MessageContent(document=f.document(name=None, mime_type="image/png"))
    path = await download(client, f.message(content), tmp_path)
    assert path.suffix == ".png"


async def test_download_accepts_media_info_directly():
    client = FakeClient()
    media = describe(f.message(f.photo())).media
    assert isinstance(media, MediaInfo)
    assert await download(client, media) == b"payload"


def test_suggest_filename_falls_back_to_mime():
    media = MediaInfo(file_id=7, access_hash=8, mime_type="application/pdf", name=None)
    assert suggest_filename(media) == "file-7.pdf"
