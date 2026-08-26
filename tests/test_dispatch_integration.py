"""Wiring checks against the real BaleClient Dispatcher/Router/Message objects.

These catch the two ways this integration silently breaks: a handler the
dispatcher does not recognise as a coroutine (it would be pushed to a thread
executor and never awaited), and the self-echo bookkeeping in Client.
"""

import pytest
from baleclient import Dispatcher
from baleclient.enums import ChatType
from baleclient.types import Chat, Message, MessageContent, TextMessage

from balebot.client import MAX_PENDING_IDS, ChatClient
from balebot.handlers import build_router
from balebot.storage import ConversationStore
from tests.test_handlers import FakeClient, FakeLLM, make_config


def make_message(text: str, sender_id: int = 222, chat_id: int = 222) -> Message:
    return Message(
        chat=Chat(type=ChatType.PRIVATE, id=chat_id),
        sender_id=sender_id,
        date=1,
        message_id=7,
        content=MessageContent(text=TextMessage(value=text)),
    )


async def build_dispatcher(tmp_path, llm):
    config = make_config(tmp_path)
    store = ConversationStore(config.db_path)
    await store.init()
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(config, store, llm))
    return dispatcher


async def test_dispatch_reaches_handler_and_answers(tmp_path, monkeypatch):
    sent: list[str] = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer, raising=True)

    llm = FakeLLM(answer="پاسخ مدل")
    dispatcher = await build_dispatcher(tmp_path, llm)

    await dispatcher.dispatch("message", make_message("سلام"), client=FakeClient())

    assert sent == ["پاسخ مدل"]
    assert llm.calls[0][0] == "سلام"


async def test_dispatch_skips_non_text_messages(tmp_path, monkeypatch):
    async def fail_answer(self, text, **kwargs):  # pragma: no cover
        raise AssertionError("should not answer a message without text")

    monkeypatch.setattr(Message, "answer", fail_answer, raising=True)

    dispatcher = await build_dispatcher(tmp_path, FakeLLM())
    empty = Message(
        chat=Chat(type=ChatType.PRIVATE, id=222),
        sender_id=222,
        date=1,
        message_id=8,
        content=MessageContent(),
    )
    await dispatcher.dispatch("message", empty, client=FakeClient())


@pytest.fixture
def client(tmp_path):
    # No session file: the token stays None, which is fine for _should_ignore.
    return ChatClient(dispatcher=None, session_file=tmp_path / "session.bale")


def test_own_echo_is_ignored_once(client):
    client._ignored_messages.targets.append(7)
    message = make_message("echo")

    assert client._should_ignore("message", message) is True  # our own send
    assert client._should_ignore("message", message) is False  # id consumed
    assert client._ignored_messages.targets == []


def test_pending_ids_do_not_grow_without_bound(client):
    targets = client._ignored_messages.targets
    # Ids of sends whose echo never came back; none of them is the id below.
    targets.extend(range(1000, 1000 + MAX_PENDING_IDS + 50))

    assert client._should_ignore("message", make_message("incoming")) is False

    assert len(targets) == MAX_PENDING_IDS
    assert targets[-1] == 1000 + MAX_PENDING_IDS + 49  # newest ids survive
