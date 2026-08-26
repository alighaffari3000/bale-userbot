"""Incoming-message routing: filter, remember, ask Gemini, answer."""

import asyncio
import logging
from collections import defaultdict

from baleclient import Client, Router
from baleclient.enums import ChatType, TypingMode
from baleclient.filters import IsText
from baleclient.types import Message

from .config import Config
from .llm import GeminiLLM, LLMError
from .storage import ConversationStore

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "دستورها:\n"
    "/help — همین راهنما\n"
    "/reset — پاک کردن حافظهٔ گفتگو\n"
    "هر پیام دیگری مستقیم به مدل داده می‌شود."
)

_PRIVATE_CHATS = (ChatType.PRIVATE, ChatType.BOT)
_GROUP_CHATS = (ChatType.GROUP, ChatType.SUPER_GROUP, ChatType.CHANNEL)


def split_reply(text: str, limit: int) -> list[str]:
    """Split a long answer into chunks Bale will accept, on line/word borders."""
    if limit <= 0:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


class MessageHandler:
    """Holds the wiring the dispatcher callback needs (config, store, llm)."""

    def __init__(
        self, config: Config, store: ConversationStore, llm: GeminiLLM
    ) -> None:
        self._config = config
        self._store = store
        self._llm = llm
        # Updates arrive as concurrent tasks; one lock per chat keeps a single
        # conversation strictly ordered without serializing unrelated chats.
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- gating ------------------------------------------------------------
    def _accepts(self, message: Message, client: Client) -> bool:
        cfg = self._config

        # 1. Never answer ourselves — the single most important loop guard.
        own_id = getattr(client, "id", None)
        if own_id is not None and message.sender_id == own_id:
            return False

        # 2. Chat scope.
        chat_type = message.chat.type
        if chat_type in _PRIVATE_CHATS:
            pass
        elif chat_type in _GROUP_CHATS:
            if not cfg.handle_groups:
                return False
        else:
            return False

        # 3. Optional allowlist.
        if cfg.has_allowlist and message.sender_id not in cfg.allowed_user_ids:
            logger.info(
                "Ignoring message from non-allowlisted user %s", message.sender_id
            )
            return False

        return True

    # -- reply helpers -----------------------------------------------------
    async def _send(self, message: Message, text: str) -> None:
        send = message.reply if self._config.reply_mode == "reply" else message.answer
        for index, chunk in enumerate(split_reply(text, self._config.max_reply_chars)):
            if not chunk:
                continue
            if index:
                await asyncio.sleep(0.4)  # keep ordering and stay polite
            await send(chunk)

    async def _typing(self, message: Message, client: Client, active: bool) -> None:
        if not self._config.typing_indicator:
            return
        action = client.start_typing if active else client.stop_typing
        try:
            await action(message.chat.id, message.chat.type, TypingMode.TEXT)
        except Exception as exc:  # never fail a reply over a typing hint
            logger.debug("Typing indicator failed: %s", exc)

    # -- commands ----------------------------------------------------------
    async def _handle_command(self, message: Message, text: str) -> bool:
        command = text.split()[0].lower().lstrip("/")
        if command == "reset":
            removed = await self._store.clear(message.chat.id)
            await self._send(message, f"حافظهٔ گفتگو پاک شد ({removed} پیام).")
            return True
        if command in ("help", "start"):
            await self._send(message, HELP_TEXT)
            return True
        return False

    # -- entry point -------------------------------------------------------
    async def __call__(self, message: Message, client: Client) -> None:
        try:
            await self._process(message, client)
        except Exception:
            # The session dispatches updates in fire-and-forget tasks, so an
            # escaping exception would be silently swallowed. Log it here.
            logger.exception("Failed to handle message %s", message.message_id)

    async def _process(self, message: Message, client: Client) -> None:
        if not self._accepts(message, client):
            return

        text = (message.text or "").strip()
        if not text:
            return

        chat_id = message.chat.id
        if self._config.log_message_text:
            logger.info("Message from %s in %s: %s", message.sender_id, chat_id, text)
        else:
            logger.info(
                "Message from %s in %s (%s chars)",
                message.sender_id,
                chat_id,
                len(text),
            )

        if text.startswith("/") and await self._handle_command(message, text):
            return

        if len(text) > self._config.max_input_chars:
            await self._send(
                message,
                f"پیام خیلی بلند است (حداکثر {self._config.max_input_chars} کاراکتر).",
            )
            return

        lock = self._locks[chat_id]
        if lock.locked():
            await self._send(message, self._config.busy_notice)
            return

        async with lock:
            await self._typing(message, client, True)
            try:
                history = await self._store.history(
                    chat_id, self._config.max_history_turns
                )
                answer = await self._llm.reply(text, history)
            except LLMError as exc:
                logger.warning("LLM refused/failed for chat %s: %s", chat_id, exc)
                await self._send(message, "الان نتوانستم پاسخ بدهم؛ لطفاً دوباره بفرست.")
                return
            finally:
                await self._typing(message, client, False)

            await self._store.append(chat_id, message.sender_id, "user", text)
            await self._store.append(chat_id, message.sender_id, "model", answer)
            await self._send(message, answer)


def build_router(config: Config, store: ConversationStore, llm: GeminiLLM) -> Router:
    """Router with the single text-message handler this MVP needs."""
    router = Router(name="ai-chat")
    handler = MessageHandler(config, store, llm)

    # Registered as a plain coroutine function on purpose: the dispatcher
    # detects awaitables with `inspect.iscoroutinefunction`, which is False for
    # a callable instance and would push the handler into a thread executor.
    @router.message(IsText())
    async def on_message(message: Message, client: Client) -> None:
        await handler(message, client)

    return router
