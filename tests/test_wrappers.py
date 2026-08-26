"""BaleApp's convenience surface, and the extras it delegates to."""

import json

import pytest
from baleclient.enums import ChatType, TypingMode
from baleclient.methods import (
    MessageRemoveReaction,
    MessageSetReaction,
    StopTyping,
    Typing,
)
from baleclient.types import MessageContent

from bale_userbot import BaleApp, Config, MessageKind, describe
from bale_userbot.extras import (
    json_block_content,
    location_content,
    react,
    set_typing,
    sticker_content,
    unreact,
)
from tests import factories as f
from tests.test_extras import FakeSender, content_wire
from tests.test_media import FakeClient


class FakeBoth(FakeSender, FakeClient):
    def __init__(self):
        FakeSender.__init__(self)
        FakeClient.__init__(self)

    async def __call__(self, call):
        self.sent.append(call)
        from types import SimpleNamespace

        return SimpleNamespace(message=f.message(f.text_content()), reactions=["👍"])


def make_app(tmp_path) -> tuple[BaleApp, FakeBoth]:
    app = BaleApp(Config(session_file=tmp_path / "session.bale"))
    client = FakeBoth()
    app._client = client  # bypass the real client, which would need a session
    return app, client


# --- reply_* helpers take chat id and type from the source message ---------


async def test_reply_location_uses_the_source_chat(tmp_path):
    app, client = make_app(tmp_path)
    group = f.message(f.text_content(), chat_id=999, chat_type=ChatType.GROUP)

    await app.reply_location(group, 35.5, 51.25)

    (call,) = client.sent
    assert call.chat.id == 999
    assert call.chat.type == ChatType.GROUP
    payload = json.loads(content_wire(call.content)["7"]["1"])
    assert payload["data"]["location"]["latitude"] == 35.5


async def test_reply_contact_and_sticker_use_the_source_chat(tmp_path):
    app, client = make_app(tmp_path)
    group = f.message(f.text_content(), chat_id=42, chat_type=ChatType.GROUP)

    await app.reply_contact(group, "Ali", ["0912"])
    await app.reply_sticker(group, f.message(f.sticker_content()))

    contact_call, sticker_call = client.sent
    assert contact_call.chat.id == 42 and contact_call.chat.type == ChatType.GROUP
    assert sticker_call.chat.id == 42 and sticker_call.chat.type == ChatType.GROUP
    assert content_wire(sticker_call.content)["12"] == f.STICKER_WIRE


async def test_reply_content_sends_any_raw_content(tmp_path):
    app, client = make_app(tmp_path)
    await app.reply_content(f.message(f.text_content()), location_content(1.0, 2.0))
    assert describe(f.message(client.sent[0].content)).kind is MessageKind.LOCATION


# --- send_* wrappers -------------------------------------------------------


async def test_send_wrappers_default_to_private_but_accept_a_type(tmp_path):
    app, client = make_app(tmp_path)
    await app.send_location(1.0, 2.0, 5)
    await app.send_contact("Ali", ["0912"], 6, ChatType.GROUP)

    private_call, group_call = client.sent
    assert private_call.chat.type == ChatType.PRIVATE
    assert group_call.chat.type == ChatType.GROUP


# --- download: sentinel vs explicit None -----------------------------------


async def test_download_without_destination_writes_a_file(tmp_path):
    app, client = make_app(tmp_path)
    app.config.download_dir = tmp_path / "downloads"

    result = await app.download(f.message(f.photo()))

    assert result.parent == tmp_path / "downloads"
    assert result.read_bytes() == b"payload"


async def test_download_with_explicit_none_returns_bytes(tmp_path):
    # README documents this call as returning bytes; before the sentinel it
    # silently behaved like the no-argument form and returned a Path.
    app, client = make_app(tmp_path)
    assert await app.download(f.message(f.photo()), destination=None) == b"payload"


async def test_download_names_the_file_after_its_kind(tmp_path):
    app, client = make_app(tmp_path)
    app.config.download_dir = tmp_path / "downloads"

    # A sticker has no filename on the wire, so the fallback stem shows.
    path = await app.download(f.message(f.sticker_content()))
    assert path.name.startswith("sticker-")
    assert path.suffix == ".png"


# --- reactions and typing --------------------------------------------------


@pytest.mark.parametrize(
    "chat_type", [ChatType.PRIVATE, ChatType.GROUP, ChatType.BOT, ChatType.CHANNEL]
)
async def test_react_resolves_the_peer_for_every_chat_type(chat_type):
    # Upstream builds Peer(type=chat_type) directly, which cannot validate a
    # BOT or CHANNEL chat: reacting there crashed before any network call.
    client = FakeBoth()
    message = f.message(f.text_content(), chat_id=7, chat_type=chat_type)

    reactions = await react(client, message, "👍")

    (call,) = client.sent
    assert isinstance(call, MessageSetReaction)
    assert call.emojy == "👍"
    assert call.message_id == message.message_id
    assert call.date == message.date
    assert reactions == ["👍"]


async def test_unreact_sends_the_remove_call():
    client = FakeBoth()
    await unreact(client, f.message(f.text_content()), "👍")
    assert isinstance(client.sent[0], MessageRemoveReaction)


async def test_app_react_passthrough(tmp_path):
    app, client = make_app(tmp_path)
    await app.react(f.message(f.text_content()), "❤")
    await app.unreact(f.message(f.text_content()), "❤")
    assert [type(c) for c in client.sent] == [MessageSetReaction, MessageRemoveReaction]


@pytest.mark.parametrize("chat_type", [ChatType.CHANNEL, ChatType.BOT])
async def test_set_typing_works_where_upstream_cannot(chat_type):
    client = FakeBoth()
    await set_typing(client, 3, chat_type)
    await set_typing(client, 3, chat_type, stop=True)

    start, stop = client.sent
    assert isinstance(start, Typing) and isinstance(stop, StopTyping)
    assert start.typing_type == TypingMode.TEXT


async def test_app_typing_accepts_a_mode(tmp_path):
    app, client = make_app(tmp_path)
    await app.typing(9, ChatType.GROUP, mode=TypingMode.SENDINGPHOTO)
    assert client.sent[0].typing_type == TypingMode.SENDINGPHOTO


# --- sticker and JSON block edge cases -------------------------------------


def test_sticker_with_only_the_small_rendition_still_works():
    block = {k: v for k, v in f.STICKER_WIRE.items() if k != "3"}
    info = describe(f.message(MessageContent.model_validate({"12": block})))

    assert info.kind is MessageKind.STICKER
    assert info.sticker.image512 is None
    assert info.sticker.image is info.sticker.image256
    assert info.media.file_id == info.sticker.image256.file_id


def test_sticker_without_any_rendition_is_not_a_sticker():
    block = {k: v for k, v in f.STICKER_WIRE.items() if k not in ("3", "4")}
    info = describe(f.message(MessageContent.model_validate({"12": block})))
    assert info.kind is MessageKind.UNKNOWN
    assert info.sticker is None


def test_sticker_wrapped_in_a_keyboard_is_still_seen():
    inner = f.sticker_content()
    info = describe(f.message(f.with_keyboard(inner)))
    assert info.kind is MessageKind.STICKER
    assert info.has_keyboard is True


def test_location_wrapped_in_a_keyboard_is_still_seen():
    info = describe(f.message(f.with_keyboard(f.json_content(f.location_json()))))
    assert info.kind is MessageKind.LOCATION
    assert info.has_keyboard is True


def test_json_block_content_needs_json_content():
    with pytest.raises(ValueError):
        json_block_content(f.message(f.photo()))


def test_sticker_content_rejects_a_rendition_less_info():
    from bale_userbot import StickerInfo

    bare = StickerInfo(sticker_id=1, collection_id=2, collection_access_hash=3)
    with pytest.raises(ValueError):
        sticker_content(bare)


async def test_send_content_strips_nested_nones_before_the_wire():
    # A None inside an extra dict reaches the schema-less encoder, which
    # cannot type it and raises. Received payloads can carry them.
    client = FakeBoth()
    content = MessageContent.model_validate(
        {"12": {"1": {"1": 5}, "3": {"1": {"1": 1, "2": 2}, "2": None}}}
    )
    from bale_userbot.extras import send_content

    await send_content(client, content, f.PEER_ID, ChatType.PRIVATE)

    wire = content_wire(client.sent[0].content)
    assert "2" not in wire["12"]["3"]


async def test_send_content_accepts_a_reply_to(tmp_path):
    app, client = make_app(tmp_path)
    target = f.message(f.text_content(), message_id=77)
    await app.reply_location(target, 1.0, 2.0, reply_to=target)
    assert client.sent[0].reply_to is not None
