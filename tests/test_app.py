"""Dispatcher wiring, filters, gating and the client fix — no network."""

import asyncio

import pytest
from baleclient import Dispatcher
from baleclient.enums import ChatType

from balekit import BaleApp, Config, IsMedia, Kind, MessageKind
from balekit.client import MAX_PENDING_IDS, KitClient
from balekit.routing import ChatScope, ChatSerializer, FromUsers, NotSelf, wrap_handler
from tests import factories as f


class FakeClient:
    id = f.SELF_ID


def make_app(tmp_path, **overrides) -> BaleApp:
    config = Config(session_file=tmp_path / "session.bale", **overrides)
    return BaleApp(config, dispatcher=Dispatcher())


async def dispatch(app: BaleApp, message) -> None:
    await app.dispatcher.dispatch("message", message, client=FakeClient())


# --- filters --------------------------------------------------------------


async def test_kind_filter():
    assert await Kind(MessageKind.PHOTO)(f.message(f.photo())) is True
    assert await Kind(MessageKind.PHOTO)(f.message(f.video())) is False
    both = Kind(MessageKind.PHOTO, MessageKind.VIDEO)
    assert await both(f.message(f.video())) is True


async def test_is_media_filter():
    assert await IsMedia()(f.message(f.voice())) is True
    assert await IsMedia()(f.message(f.text_content())) is False


async def test_not_self_filter():
    own = f.message(f.text_content(), sender_id=f.SELF_ID)
    other = f.message(f.text_content(), sender_id=f.PEER_ID)
    assert await NotSelf()(own, client=FakeClient()) is False
    assert await NotSelf()(other, client=FakeClient()) is True


async def test_chat_scope_filter():
    private = f.message(f.text_content())
    group = f.message(f.text_content(), chat_type=ChatType.GROUP)
    assert await ChatScope(private=True, groups=False)(private) is True
    assert await ChatScope(private=True, groups=False)(group) is False
    assert await ChatScope(private=False, groups=True)(group) is True


async def test_from_users_filter():
    message = f.message(f.text_content(), sender_id=5)
    assert await FromUsers(5, 6)(message) is True
    assert await FromUsers(9)(message) is False


def test_kind_filter_needs_a_kind():
    with pytest.raises(ValueError):
        Kind()


# --- handler registration end to end --------------------------------------


async def test_handler_runs_through_the_real_dispatcher(tmp_path):
    app = make_app(tmp_path)
    seen = []

    @app.on_message()
    async def handler(message, client):
        seen.append(app.describe(message).kind)

    await dispatch(app, f.message(f.photo()))
    assert seen == [MessageKind.PHOTO]


async def test_kinds_argument_filters(tmp_path):
    app = make_app(tmp_path)
    seen = []

    @app.on_message(kinds=[MessageKind.VOICE, MessageKind.AUDIO])
    async def handler(message, client):
        seen.append(app.describe(message).kind)

    for content in (f.photo(), f.voice(), f.text_content(), f.audio()):
        await dispatch(app, f.message(content))

    assert seen == [MessageKind.VOICE, MessageKind.AUDIO]


async def test_self_messages_never_reach_a_handler(tmp_path):
    app = make_app(tmp_path)
    seen = []

    @app.on_message()
    async def handler(message, client):
        seen.append(message.message_id)

    await dispatch(app, f.message(f.text_content(), sender_id=f.SELF_ID))
    assert seen == []


async def test_groups_are_off_by_default(tmp_path):
    app = make_app(tmp_path)
    seen = []

    @app.on_message()
    async def handler(message, client):
        seen.append(message.chat.id)

    await dispatch(app, f.message(f.text_content(), chat_type=ChatType.GROUP))
    assert seen == []

    allowed = make_app(tmp_path, handle_groups=True)

    @allowed.on_message()
    async def handler2(message, client):
        seen.append(message.chat.id)

    await dispatch(allowed, f.message(f.text_content(), chat_type=ChatType.GROUP))
    assert seen == [f.PEER_ID]


async def test_allowlist_is_enforced(tmp_path):
    app = make_app(tmp_path, allowed_user_ids=frozenset({999}))
    seen = []

    @app.on_message()
    async def handler(message, client):
        seen.append(message.sender_id)

    await dispatch(app, f.message(f.text_content(), sender_id=f.PEER_ID))
    await dispatch(app, f.message(f.text_content(), sender_id=999))
    assert seen == [999]


async def test_handler_exception_does_not_escape(tmp_path, caplog):
    app = make_app(tmp_path)

    @app.on_message()
    async def handler(message, client):
        raise RuntimeError("boom")

    await dispatch(app, f.message(f.text_content()))  # must not raise
    assert "boom" in caplog.text or "failed" in caplog.text


async def test_callable_object_handlers_are_still_awaited(tmp_path):
    """A handler that is an instance, not a function — the dispatcher's trap."""
    app = make_app(tmp_path)
    seen = []

    class Handler:
        async def __call__(self, message, client):
            seen.append(message.message_id)

    app.on_message()(Handler())
    await dispatch(app, f.message(f.text_content(), message_id=77))
    assert seen == [77]


# --- ordering -------------------------------------------------------------


async def test_one_chat_is_handled_in_order():
    serializer = ChatSerializer()
    order = []
    started = asyncio.Event()

    async def slow(message, client):
        order.append(("start", message.message_id))
        if message.message_id == 1:
            started.set()
            await asyncio.sleep(0.05)
        order.append(("end", message.message_id))

    handler = wrap_handler(slow, serializer=serializer)
    one = f.message(f.text_content(), message_id=1)
    first = asyncio.create_task(handler(one, None))
    await started.wait()
    two = f.message(f.text_content(), message_id=2)
    second = asyncio.create_task(handler(two, None))
    await asyncio.gather(first, second)

    assert order == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]


async def test_different_chats_are_not_blocked_by_each_other():
    serializer = ChatSerializer()
    running = []

    async def slow(message, client):
        running.append(message.chat.id)
        await asyncio.sleep(0.02)

    handler = wrap_handler(slow, serializer=serializer)
    await asyncio.gather(
        handler(f.message(f.text_content(), chat_id=1), None),
        handler(f.message(f.text_content(), chat_id=2), None),
    )
    assert sorted(running) == [1, 2]


# --- the client fix -------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    return KitClient(dispatcher=None, session_file=tmp_path / "session.bale")


def test_own_echo_is_ignored_exactly_once(client):
    client._ignored_messages.targets.append(7)
    message = f.message(f.text_content(), message_id=7)
    assert client._should_ignore("message", message) is True
    assert client._should_ignore("message", message) is False
    assert client._ignored_messages.targets == []


def test_pending_ids_stay_bounded(client):
    targets = client._ignored_messages.targets
    targets.extend(range(1000, 1000 + MAX_PENDING_IDS + 50))
    assert client._should_ignore("message", f.message(f.text_content())) is False
    assert len(targets) == MAX_PENDING_IDS
    assert targets[-1] == 1000 + MAX_PENDING_IDS + 49


def test_other_event_types_are_not_dropped(client):
    assert client._should_ignore("message_edited", object()) is False


def test_upstream_should_ignore_is_still_broken(client):
    """If this starts failing, BaleClient fixed it and KitClient can go away."""
    from baleclient import Client

    client._ignored_messages.targets.append(7)
    with pytest.raises(TypeError):
        echo = f.message(f.text_content(), message_id=7)
        Client._should_ignore(client, "message", echo)
