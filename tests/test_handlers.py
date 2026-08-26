"""Unit tests for the pieces that do not need a live Bale connection."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from baleclient.enums import ChatType

from balebot.config import Config
from balebot.handlers import MessageHandler, split_reply
from balebot.storage import ConversationStore


def make_config(tmp_path: Path, **overrides) -> Config:
    base = Config(
        session_file=tmp_path / "session.bale",
        proxy=None,
        allowed_user_ids=frozenset(),
        reply_mode="answer",
        typing_indicator=False,
        handle_groups=False,
        gemini_api_key="test-key",
        gemini_model="gemini-2.5-flash",
        temperature=0.7,
        max_output_tokens=256,
        system_prompt="be helpful",
        db_path=tmp_path / "chat.db",
        max_history_turns=4,
        log_level="INFO",
        log_message_text=False,
        max_input_chars=100,
        max_reply_chars=50,
    )
    return replace(base, **overrides) if overrides else base


class FakeClient:
    id = 111

    async def start_typing(self, *args, **kwargs):
        return None

    async def stop_typing(self, *args, **kwargs):
        return None


class FakeChat:
    def __init__(self, chat_id: int, chat_type: ChatType) -> None:
        self.id = chat_id
        self.type = chat_type


class FakeMessage:
    """Stands in for baleclient.types.Message (which needs a real wire payload)."""

    def __init__(self, text, sender_id=222, chat_id=222, chat_type=ChatType.PRIVATE):
        self.text = text
        self.sender_id = sender_id
        self.message_id = 1
        self.chat = FakeChat(chat_id, chat_type)
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)
        return None

    async def reply(self, text, **kwargs):
        self.sent.append(text)
        return None


class FakeLLM:
    def __init__(self, answer="سلام", error=None):
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, list]] = []

    async def reply(self, prompt, history=(), **kwargs):
        self.calls.append((prompt, list(history)))
        if self.error:
            raise self.error
        return self.answer


async def build(tmp_path, llm=None, **cfg):
    config = make_config(tmp_path, **cfg)
    store = ConversationStore(config.db_path)
    await store.init()
    return MessageHandler(config, store, llm or FakeLLM()), store


# --- split_reply ----------------------------------------------------------


def test_split_reply_keeps_short_text_intact():
    assert split_reply("hello", 50) == ["hello"]


def test_split_reply_chunks_on_boundaries():
    text = "\n".join(f"line {i}" for i in range(40))
    chunks = split_reply(text, 60)
    assert all(len(c) <= 60 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks).startswith("line 0line 1")


# --- loop prevention & gating --------------------------------------------


async def test_own_message_is_never_answered(tmp_path):
    handler, _ = await build(tmp_path)
    msg = FakeMessage("hi", sender_id=FakeClient.id)
    await handler(msg, FakeClient())
    assert msg.sent == []


async def test_group_message_ignored_by_default(tmp_path):
    handler, _ = await build(tmp_path)
    msg = FakeMessage("hi", chat_type=ChatType.GROUP)
    await handler(msg, FakeClient())
    assert msg.sent == []


async def test_group_message_answered_when_enabled(tmp_path):
    handler, _ = await build(tmp_path, handle_groups=True)
    msg = FakeMessage("hi", chat_type=ChatType.GROUP)
    await handler(msg, FakeClient())
    assert msg.sent == ["سلام"]


async def test_allowlist_blocks_strangers(tmp_path):
    handler, _ = await build(tmp_path, allowed_user_ids=frozenset({999}))
    msg = FakeMessage("hi", sender_id=222)
    await handler(msg, FakeClient())
    assert msg.sent == []


# --- conversation flow ----------------------------------------------------


async def test_reply_is_stored_as_history(tmp_path):
    llm = FakeLLM(answer="answer one")
    handler, store = await build(tmp_path, llm=llm)

    first = FakeMessage("question one")
    await handler(first, FakeClient())
    assert first.sent == ["answer one"]

    llm.answer = "answer two"
    second = FakeMessage("question two")
    await handler(second, FakeClient())

    prompt, history = llm.calls[-1]
    assert prompt == "question two"
    assert [(t.role, t.text) for t in history] == [
        ("user", "question one"),
        ("model", "answer one"),
    ]
    assert (await store.history(222, 10))[-1].text == "answer two"


async def test_reset_command_clears_history(tmp_path):
    handler, store = await build(tmp_path)
    await handler(FakeMessage("hello"), FakeClient())
    assert await store.history(222, 10)

    reset = FakeMessage("/reset")
    await handler(reset, FakeClient())
    assert await store.history(222, 10) == []
    assert "پاک شد" in reset.sent[0]


async def test_overlong_input_is_rejected(tmp_path):
    handler, _ = await build(tmp_path)
    msg = FakeMessage("x" * 500)
    await handler(msg, FakeClient())
    assert "بلند" in msg.sent[0]


async def test_llm_failure_is_reported_not_raised(tmp_path):
    from balebot.llm import LLMError

    handler, store = await build(tmp_path, llm=FakeLLM(error=LLMError("boom")))
    msg = FakeMessage("hi")
    await handler(msg, FakeClient())
    assert msg.sent  # user gets an apology, not silence
    assert await store.history(222, 10) == []  # nothing half-written


async def test_concurrent_message_gets_busy_notice(tmp_path):
    release = asyncio.Event()

    class SlowLLM(FakeLLM):
        async def reply(self, prompt, history=(), **kwargs):
            await release.wait()
            return "done"

    handler, _ = await build(tmp_path, llm=SlowLLM())
    first, second = FakeMessage("one"), FakeMessage("two")

    task = asyncio.create_task(handler(first, FakeClient()))
    await asyncio.sleep(0)  # let the first handler take the chat lock
    await handler(second, FakeClient())
    release.set()
    await task

    assert second.sent and "مشغول" in second.sent[0]
    assert first.sent == ["done"]


# --- storage --------------------------------------------------------------


async def test_history_window_is_bounded(tmp_path):
    store = ConversationStore(tmp_path / "chat.db")
    await store.init()
    for i in range(10):
        await store.append(1, 1, "user", f"q{i}")
        await store.append(1, 1, "model", f"a{i}")

    history = await store.history(1, max_turns=2)
    assert [t.text for t in history] == ["q8", "a8", "q9", "a9"]


async def test_histories_are_per_chat(tmp_path):
    store = ConversationStore(tmp_path / "chat.db")
    await store.init()
    await store.append(1, 1, "user", "mine")
    await store.append(2, 2, "user", "theirs")
    assert [t.text for t in await store.history(1, 5)] == ["mine"]
    assert [t.text for t in await store.history(2, 5)] == ["theirs"]


async def test_unknown_role_rejected(tmp_path):
    store = ConversationStore(tmp_path / "chat.db")
    await store.init()
    with pytest.raises(ValueError):
        await store.append(1, 1, "system", "nope")
