"""Filters and handler wrapping.

Two footguns in BaleClient's dispatcher are handled here once:

* handlers must be coroutine *functions* — a callable instance is detected as
  non-awaitable by `inspect.iscoroutinefunction` and silently pushed into a
  thread executor, where its coroutine is never awaited;
* updates are dispatched in fire-and-forget tasks, so an exception escaping a
  handler disappears with the task.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

from baleclient import Client
from baleclient.enums import ChatType
from baleclient.filters import Filter
from baleclient.types import Message

from .content import MEDIA_KINDS, MessageKind, describe

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

_GROUP_CHATS = (ChatType.GROUP, ChatType.SUPER_GROUP, ChatType.CHANNEL)
_PRIVATE_CHATS = (ChatType.PRIVATE, ChatType.BOT)


class Kind(Filter):
    """Match messages of one or more `MessageKind`s."""

    def __init__(self, *kinds: MessageKind) -> None:
        if not kinds:
            raise ValueError("Kind() needs at least one MessageKind")
        self.kinds = frozenset(kinds)

    async def __call__(self, event: Any) -> bool:
        if not isinstance(event, Message):
            return False
        return describe(event).kind in self.kinds


class IsMedia(Filter):
    """Match any message carrying a downloadable file."""

    async def __call__(self, event: Any) -> bool:
        if not isinstance(event, Message):
            return False
        return describe(event).kind in MEDIA_KINDS


class NotSelf(Filter):
    """Drop messages this account sent itself — the basic echo-loop guard."""

    async def __call__(self, event: Any, client: Client | None = None) -> bool:
        if not isinstance(event, Message):
            return False
        own_id = getattr(client, "id", None)
        return own_id is None or event.sender_id != own_id


class FromUsers(Filter):
    """Allowlist: only messages from these sender ids."""

    def __init__(self, *user_ids: int) -> None:
        self.user_ids = frozenset(user_ids)

    async def __call__(self, event: Any) -> bool:
        if not isinstance(event, Message):
            return False
        return event.sender_id in self.user_ids


class InChats(Filter):
    """Only messages from these chat ids."""

    def __init__(self, *chat_ids: int) -> None:
        self.chat_ids = frozenset(chat_ids)

    async def __call__(self, event: Any) -> bool:
        if not isinstance(event, Message):
            return False
        return event.chat.id in self.chat_ids


class ChatScope(Filter):
    """Restrict to private chats, group-like chats, or both."""

    def __init__(self, private: bool = True, groups: bool = False) -> None:
        self.private = private
        self.groups = groups

    async def __call__(self, event: Any) -> bool:
        if not isinstance(event, Message):
            return False
        chat_type = event.chat.type
        if chat_type in _PRIVATE_CHATS:
            return self.private
        if chat_type in _GROUP_CHATS:
            return self.groups
        return False


class ChatSerializer:
    """Keeps handling of one chat sequential without blocking other chats.

    Locks are reference counted rather than dropped when they look idle:
    `asyncio.Lock.release()` clears `locked()` before the next waiter wakes,
    so a lock discarded on that criterion can still have a task queued on it.
    The next message for that chat would then take a fresh lock and run
    concurrently with it — silently breaking the ordering this class exists
    to provide.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._users: Counter[int] = Counter()

    @asynccontextmanager
    async def hold(self, chat_id: int) -> AsyncIterator[None]:
        """Hold one chat's turn, releasing the lock once nobody needs it."""
        lock = self._locks[chat_id]
        self._users[chat_id] += 1
        try:
            async with lock:
                yield
        finally:
            self._users[chat_id] -= 1
            if self._users[chat_id] <= 0:
                del self._users[chat_id]
                self._locks.pop(chat_id, None)

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks[chat_id]

    @property
    def tracked_chats(self) -> int:
        """How many chats currently hold a lock. Zero when fully drained."""
        return len(self._locks)


def wrap_handler(
    handler: Handler,
    *,
    serializer: ChatSerializer | None = None,
    on_error: Callable[[BaseException, Message], Awaitable[None]] | None = None,
) -> Handler:
    """Return a coroutine function the dispatcher can safely call.

    Logs anything the handler raises instead of letting it vanish with the
    dispatch task, and — when a serializer is given — keeps one chat's messages
    strictly in order.
    """

    async def run(message: Message, client: Client) -> None:
        try:
            await handler(message, client)
        except Exception as exc:
            logger.exception(
                "handler %s failed on message %s in chat %s",
                getattr(handler, "__name__", handler),
                getattr(message, "message_id", "?"),
                getattr(getattr(message, "chat", None), "id", "?"),
            )
            if on_error is not None:
                try:
                    await on_error(exc, message)
                except Exception:
                    logger.exception("on_error callback failed")

    async def wrapped(message: Message, client: Client) -> None:
        if serializer is None:
            await run(message, client)
            return
        async with serializer.hold(message.chat.id):
            await run(message, client)

    wrapped.__name__ = getattr(handler, "__name__", "handler")
    return wrapped


def kinds_filter(kinds: Iterable[MessageKind] | None) -> Kind | None:
    kinds = list(kinds or [])
    return Kind(*kinds) if kinds else None
