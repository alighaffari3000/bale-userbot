"""`BaleApp` — session, dispatcher, handler registration and the run loop."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from baleclient import Dispatcher, Router
from baleclient.enums import ChatType
from baleclient.filters import Filter
from baleclient.types import Message

from .client import KitClient
from .config import Config
from .content import MessageInfo, MessageKind, describe
from .logging_setup import setup_logging
from .media import FileLike, download, resend, send_media
from .routing import (
    ChatScope,
    ChatSerializer,
    FromUsers,
    NotSelf,
    kinds_filter,
    wrap_handler,
)

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]


class SessionMissingError(RuntimeError):
    """Raised when the app starts before an interactive login was done."""


def prepare_session_file(path: Path) -> None:
    """Create the session directory and keep the file owner-only.

    The session file holds the account JWT: whoever reads it is the account.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError as exc:
            logger.warning("could not tighten permissions on %s: %s", path, exc)


class BaleApp:
    """The infrastructure layer: one account, one dispatcher, any handler.

    ```python
    app = BaleApp()

    @app.on_message(kinds=[MessageKind.PHOTO])
    async def on_photo(message, client):
        path = await app.download(message)
        await message.answer(f"saved to {path}")

    app.run()
    ```
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        dispatcher: Dispatcher | None = None,
        lifespan: Callable[[BaleApp], Any] | None = None,
    ) -> None:
        self.config = config or Config()
        self.dispatcher = dispatcher or Dispatcher()
        self._serializer = ChatSerializer() if self.config.serialize_per_chat else None
        self._lifespan_hook = lifespan
        self._client: KitClient | None = None

    # -- client ------------------------------------------------------------
    @property
    def client(self) -> KitClient:
        """The underlying BaleClient. Built on first use."""
        if self._client is None:
            prepare_session_file(self.config.session_file)
            self._client = KitClient(
                dispatcher=self.dispatcher,
                session_file=self.config.session_file,
                proxy=self.config.proxy,
                lifespan=self._make_lifespan(),
            )
        return self._client

    def _make_lifespan(self):
        hook = self._lifespan_hook

        @asynccontextmanager
        async def lifespan(client: KitClient):
            logger.info(
                "connected as user %s (private=%s groups=%s handlers=%s)",
                client.id,
                self.config.handle_private,
                self.config.handle_groups,
                self.dispatcher.handler_count(),
            )
            if hook is None:
                yield
                return
            async with hook(self) as value:  # type: ignore[union-attr]
                yield value

        return lifespan

    # -- registration ------------------------------------------------------
    def on_message(
        self,
        *filters: Filter,
        kinds: Iterable[MessageKind] | None = None,
        router: Router | None = None,
    ) -> Callable[[Handler], Handler]:
        """Register a message handler.

        Config-level gating (chat scope, allowlist, self-messages) is applied
        before `filters`, so a handler only ever sees messages the app is
        configured to answer.
        """
        chain: list[Filter] = []
        if self.config.ignore_self:
            chain.append(NotSelf())
        chain.append(
            ChatScope(
                private=self.config.handle_private, groups=self.config.handle_groups
            )
        )
        if self.config.allowed_user_ids:
            chain.append(FromUsers(*self.config.allowed_user_ids))
        kind_filter = kinds_filter(kinds)
        if kind_filter is not None:
            chain.append(kind_filter)
        chain.extend(filters)

        target = router or self.dispatcher

        def decorator(handler: Handler) -> Handler:
            wrapped = wrap_handler(handler, serializer=self._serializer)
            target.message(*chain)(wrapped)
            return handler

        return decorator

    def include_router(self, router: Router) -> None:
        self.dispatcher.include_router(router)

    # -- convenience over the client --------------------------------------
    @staticmethod
    def describe(message: Message) -> MessageInfo:
        return describe(message)

    async def send(
        self,
        file: FileLike,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Message:
        """Send any file; the right send_* method is picked from its type."""
        return await send_media(self.client, file, chat_id, chat_type, **kwargs)

    async def reply_with(
        self, message: Message, file: FileLike, **kwargs: Any
    ) -> Message:
        """Send any file back into the chat a message came from."""
        return await send_media(
            self.client, file, message.chat.id, message.chat.type, **kwargs
        )

    async def resend(
        self,
        message: Message,
        chat_id: int | None = None,
        chat_type: ChatType | None = None,
        **kwargs: Any,
    ) -> Message:
        """Copy a received attachment somewhere without re-uploading it."""
        return await resend(
            self.client,
            message,
            chat_id if chat_id is not None else message.chat.id,
            chat_type if chat_type is not None else message.chat.type,
            **kwargs,
        )

    async def download(
        self,
        message: Message,
        destination: str | Path | None = None,
    ) -> Any:
        """Download an attachment; defaults to the configured download dir."""
        if destination is None:
            destination = self.config.download_dir
            Path(destination).mkdir(parents=True, exist_ok=True)
        return await download(self.client, message, destination)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if not self.config.session_file.exists():
            raise SessionMissingError(
                f"no Bale session at {self.config.session_file}. "
                "Run `python -m balekit login` once to authenticate."
            )
        setup_logging(self.config.log_level)
        await self.client.start()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()

    def run(self) -> None:
        """Start and block until the process is stopped."""
        import asyncio

        async def main() -> None:
            try:
                await self.start()
            finally:
                await self.stop()

        asyncio.run(main())
