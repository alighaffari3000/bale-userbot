"""`BaleApp` — session, dispatcher, handler registration and the run loop."""

from __future__ import annotations

import logging
import os
import stat
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from baleclient import Dispatcher, Router
from baleclient.enums import ChatType
from baleclient.filters import Filter
from baleclient.types import InfoMessage, Message, Permissions

from .client import KitClient
from .config import Config
from .content import MessageInfo, MessageKind, describe
from .extras import (
    mark_seen,
    react,
    send_contact,
    send_content,
    send_location,
    send_sticker,
    set_typing,
    unreact,
)
from .groups import (
    GroupInfo,
    MemberInfo,
    MembershipChanges,
    export_members,
    get_group,
    hydrate_profiles,
    iter_members,
    list_groups,
    load_admins,
    load_members,
    member_count,
    watch_membership,
)
from .history import export_history, iter_history, load_history
from .limiter import RateLimiter
from .logging_setup import setup_logging
from .media import FileLike, download, resend, send_media
from .moderation import (
    InviteLink,
    allow,
    banned,
    demote,
    invite_link,
    join,
    kick,
    leave,
    mute,
    permissions_of,
    pin,
    pins,
    preview,
    promote,
    restrict,
    revoke_invite_link,
    set_default_permissions,
    set_permissions,
    unban,
    unmute,
    unpin,
    unpin_all,
)
from .routing import (
    ChatScope,
    ChatSerializer,
    FromUsers,
    NotSelf,
    kinds_filter,
    wrap_handler,
)
from .store import MessageStore, SearchResult
from .sync import SweepReport, sync_chat, sync_chats

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]


#: Distinguishes "no destination given" from an explicit `destination=None`,
#: which asks for the bytes.
_DEFAULT: Any = object()


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
        self._store: MessageStore | None = None
        self._limiter = RateLimiter()
        #: Ids of messages this app sent, so `on_message_sent` can tell an
        #: agent's own reply from the owner typing on their phone.
        self._sent_ids: OrderedDict[int, None] = OrderedDict()

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

    def on_message_sent(
        self,
        *,
        include_own: bool = False,
        router: Router | None = None,
    ) -> Callable[[Handler], Handler]:
        """Register a handler for messages this account sent.

        Bale reports every outgoing message as a separate `message_sent`
        update, whether it came from this process or from the owner's phone.
        The two are indistinguishable on the wire, so this app remembers the
        ids it sent itself and drops their echoes — what reaches the handler is
        then genuinely someone at a keyboard.

        That distinction is the point: an agent that keeps answering while its
        owner is already replying in the same chat is the most obvious way for
        it to give itself away. Pass `include_own=True` to see both.

        The payload is `InfoMessage`, not `Message`: it carries `peer`,
        `message_id` and `date` but **no text**. Read the body from history if
        the handler needs it.

        The `on_message` filter chain is deliberately not reused here — every
        filter in it starts by rejecting anything that is not a `Message`, so
        it would drop all of these.
        """
        target = router or self.dispatcher

        def decorator(handler: Handler) -> Handler:
            async def wrapped(event: InfoMessage, client: Any) -> Any:
                if not include_own and self._was_sent_here(event.message_id):
                    return None
                try:
                    return await handler(event, client)
                except Exception:
                    # Dispatch runs handlers as detached tasks, so an exception
                    # here would otherwise vanish without a trace.
                    logger.exception("message_sent handler failed")
                    return None

            target.register("message_sent")(wrapped)
            return handler

        return decorator

    def include_router(self, router: Router) -> None:
        self.dispatcher.include_router(router)

    # -- own-message bookkeeping -------------------------------------------
    #: Enough to recognise an echo, which arrives within seconds of the send.
    _SENT_ID_MEMORY = 512

    def _remember_sent(self, message: Message | InfoMessage | Any) -> Any:
        """Record an id we sent, so its echo is not mistaken for the owner."""
        message_id = getattr(message, "message_id", None)
        if message_id is not None:
            self._sent_ids[message_id] = None
            while len(self._sent_ids) > self._SENT_ID_MEMORY:
                self._sent_ids.popitem(last=False)
        return message

    def _was_sent_here(self, message_id: int | None) -> bool:
        return message_id is not None and message_id in self._sent_ids

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
        return self._remember_sent(
            await send_media(self.client, file, chat_id, chat_type, **kwargs)
        )

    async def reply_with(
        self, message: Message, file: FileLike, **kwargs: Any
    ) -> Message:
        """Send any file back into the chat a message came from."""
        return self._remember_sent(
            await send_media(
                self.client, file, message.chat.id, message.chat.type, **kwargs
            )
        )

    async def send_location(
        self,
        latitude: float,
        longitude: float,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Message:
        """Share a map point."""
        return self._remember_sent(
            await send_location(
                self.client, latitude, longitude, chat_id, chat_type, **kwargs
            )
        )

    async def send_contact(
        self,
        name: str,
        phones: Iterable[str],
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Message:
        """Share a contact card."""
        return self._remember_sent(
            await send_contact(self.client, name, phones, chat_id, chat_type, **kwargs)
        )

    async def send_sticker(
        self,
        sticker: Any,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Message:
        """Send a sticker taken from a received message or `StickerInfo`."""
        return self._remember_sent(
            await send_sticker(self.client, sticker, chat_id, chat_type, **kwargs)
        )

    async def reply_location(
        self, message: Message, latitude: float, longitude: float, **kwargs: Any
    ) -> Message:
        """Share a map point back into the chat a message came from."""
        return self._remember_sent(
            await send_location(
                self.client,
                latitude,
                longitude,
                message.chat.id,
                message.chat.type,
                **kwargs,
            )
        )

    async def reply_contact(
        self, message: Message, name: str, phones: Iterable[str], **kwargs: Any
    ) -> Message:
        """Share a contact card back into the chat a message came from."""
        return self._remember_sent(
            await send_contact(
                self.client, name, phones, message.chat.id, message.chat.type, **kwargs
            )
        )

    async def reply_sticker(
        self, message: Message, sticker: Any, **kwargs: Any
    ) -> Message:
        """Send a sticker back into the chat a message came from."""
        return self._remember_sent(
            await send_sticker(
                self.client, sticker, message.chat.id, message.chat.type, **kwargs
            )
        )

    async def reply_content(
        self, message: Message, content: Any, **kwargs: Any
    ) -> Message:
        """Send any raw `MessageContent` back into a message's chat."""
        return self._remember_sent(
            await send_content(
                self.client, content, message.chat.id, message.chat.type, **kwargs
            )
        )

    async def send_text(
        self,
        text: str,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Message:
        """Send a plain text message.

        `client.send_message` and `message.answer()` do the same thing, but
        neither records the id — so a reply sent that way comes back through
        `on_message_sent` looking like the owner typing. Prefer this.
        """
        return self._remember_sent(
            await self.client.send_message(text, chat_id, chat_type, **kwargs)
        )

    async def reply_text(self, message: Message, text: str, **kwargs: Any) -> Message:
        """Send a plain text message back into the chat a message came from."""
        return await self.send_text(
            text, message.chat.id, message.chat.type, **kwargs
        )

    async def seen(
        self,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Any:
        """Mark a chat as read — works in bot chats and channels too."""
        return await mark_seen(self.client, chat_id, chat_type, **kwargs)

    async def seen_message(self, message: Message, **kwargs: Any) -> Any:
        """Mark the chat a message came from as read."""
        return await mark_seen(
            self.client, message.chat.id, message.chat.type, **kwargs
        )

    async def react(self, message: Message, emoji: str, **kwargs: Any) -> Any:
        """React to a message — works in bot chats and channels too."""
        return await react(self.client, message, emoji, **kwargs)

    async def unreact(self, message: Message, emoji: str) -> Any:
        """Take a reaction back."""
        return await unreact(self.client, message, emoji)

    async def typing(
        self,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Any:
        """Show or clear the typing indicator in a chat."""
        return await set_typing(self.client, chat_id, chat_type, **kwargs)

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
        destination: str | Path | None = _DEFAULT,
    ) -> Any:
        """Download an attachment.

        Called with no destination the file lands in the configured download
        directory and the path is returned; an explicit `destination=None`
        returns the bytes instead.
        """
        if destination is _DEFAULT:
            destination = self.config.download_dir
            Path(destination).mkdir(parents=True, exist_ok=True)
        return await download(self.client, message, destination)

    # -- groups and members ------------------------------------------------
    async def group(self, chat_id: int, **kwargs: Any) -> GroupInfo:
        """Title, member count, owner and type of one group or channel."""
        return await get_group(self.client, chat_id, **kwargs)

    async def member_count(self, chat_id: int) -> int:
        """How many members a group has, without listing them."""
        return await member_count(self.client, chat_id)

    async def members(self, chat_id: int, **kwargs: Any) -> list[MemberInfo]:
        """The member list of a group; `profiles=True` adds names and usernames."""
        return await load_members(self.client, chat_id, **kwargs)

    def iter_members(self, chat_id: int, **kwargs: Any) -> Any:
        """Members one at a time, for groups too large to hold in memory."""
        return iter_members(self.client, chat_id, **kwargs)

    async def admins(self, chat_id: int, **kwargs: Any) -> list[MemberInfo]:
        """The admins and owner of a group."""
        return await load_admins(self.client, chat_id, **kwargs)

    async def groups(self, **kwargs: Any) -> list[GroupInfo]:
        """Every group and channel this account is in."""
        return await list_groups(self.client, **kwargs)

    async def export_members(
        self, chat_id: int, **kwargs: Any
    ) -> tuple[GroupInfo, list[dict[str, Any]]]:
        """A group and its members as plain dicts, ready for you to store."""
        return await export_members(self.client, chat_id, **kwargs)

    async def hydrate_profiles(
        self, members: Sequence[MemberInfo], **kwargs: Any
    ) -> list[MemberInfo]:
        """Fill in names and usernames for members you already have."""
        return await hydrate_profiles(self.client, members, **kwargs)

    async def watch_membership(
        self, chat_id: int, previous: Iterable[MemberInfo], **kwargs: Any
    ) -> tuple[MembershipChanges, list[MemberInfo]]:
        """Diff against your last snapshot; returns the changes and the new one."""
        return await watch_membership(self.client, chat_id, previous, **kwargs)

    # -- history -----------------------------------------------------------
    async def history(
        self,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> list[Message]:
        """A chat's messages, oldest first. `since=`/`until=` bound the range."""
        return await load_history(self.client, chat_id, chat_type, **kwargs)

    def iter_history(
        self,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> Any:
        """Messages newest-first, one at a time, for a long archive."""
        return iter_history(self.client, chat_id, chat_type, **kwargs)

    async def export_history(
        self,
        chat_id: int,
        chat_type: ChatType = ChatType.PRIVATE,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """A chat's archive as plain dicts, ready for you to store."""
        return await export_history(self.client, chat_id, chat_type, **kwargs)

    # -- moderation --------------------------------------------------------
    async def kick(self, chat_id: int, user_id: int) -> Any:
        """Remove someone from a group or channel."""
        return await kick(self.client, chat_id, user_id)

    async def unban(self, chat_id: int, user_id: int) -> Any:
        """Let a removed user back in."""
        return await unban(self.client, chat_id, user_id)

    async def banned(self, chat_id: int) -> list[int]:
        """The user ids currently barred from a group."""
        return await banned(self.client, chat_id)

    async def promote(self, chat_id: int, user_id: int, **kwargs: Any) -> Any:
        """Make someone an admin, optionally granting rights in the same call."""
        return await promote(self.client, chat_id, user_id, **kwargs)

    async def demote(self, chat_id: int, user_id: int) -> Any:
        """Take admin rights back."""
        return await demote(self.client, chat_id, user_id)

    async def permissions_of(self, chat_id: int, user_id: int) -> Permissions:
        """What one member is currently allowed to do."""
        return await permissions_of(self.client, chat_id, user_id)

    async def restrict(
        self, chat_id: int, user_id: int, *names: str, **flags: bool
    ) -> Permissions:
        """Turn named permissions off, leaving every other flag alone."""
        return await restrict(self.client, chat_id, user_id, *names, **flags)

    async def allow(
        self, chat_id: int, user_id: int, *names: str, **flags: bool
    ) -> Permissions:
        """Turn named permissions on, leaving every other flag alone."""
        return await allow(self.client, chat_id, user_id, *names, **flags)

    async def set_permissions(
        self, chat_id: int, user_id: int, **flags: bool
    ) -> Permissions:
        """Set named permissions to explicit values, leaving the rest alone."""
        return await set_permissions(self.client, chat_id, user_id, **flags)

    async def set_default_permissions(self, chat_id: int, **flags: bool) -> Permissions:
        """Change the group's baseline permissions, one named flag at a time."""
        return await set_default_permissions(self.client, chat_id, **flags)

    async def mute(self, chat_id: int, user_id: int) -> Permissions:
        """Take away every way of speaking, without removing the member."""
        return await mute(self.client, chat_id, user_id)

    async def unmute(self, chat_id: int, user_id: int) -> Permissions:
        """Give speech back."""
        return await unmute(self.client, chat_id, user_id)

    # -- pins and links ----------------------------------------------------
    async def pins(self, chat_id: int, **kwargs: Any) -> list[Message]:
        """Every pinned message, with the chat type upstream gets wrong."""
        return await pins(self.client, chat_id, **kwargs)

    async def pin(self, chat_id: int, message: Message) -> Any:
        """Pin a message."""
        return await pin(self.client, chat_id, message)

    async def unpin(self, chat_id: int, message: Message) -> Any:
        """Unpin one message."""
        return await unpin(self.client, chat_id, message)

    async def unpin_all(self, chat_id: int) -> Any:
        """Clear every pin at once."""
        return await unpin_all(self.client, chat_id)

    async def invite_link(self, chat_id: int, *, revoke: bool = False) -> InviteLink:
        """The group's invite link; `revoke=True` replaces it with a new one."""
        call = revoke_invite_link if revoke else invite_link
        return await call(self.client, chat_id)

    async def preview(self, token_or_url: str) -> GroupInfo:
        """What a group looks like from the outside, before joining."""
        return await preview(self.client, token_or_url)

    async def join(self, token_or_url: str) -> Any:
        """Join a group or channel from an invite link."""
        return await join(self.client, token_or_url)

    async def leave(self, chat_id: int) -> Any:
        """Leave a group or channel."""
        return await leave(self.client, chat_id)

    # -- searchable cache --------------------------------------------------
    @property
    def store(self) -> MessageStore:
        """The local message cache. Opened on first use."""
        if self._store is None:
            self._store = MessageStore(self.config.store_file)
        return self._store

    async def sync(
        self, chat_ids: Sequence[int] | None = None, **kwargs: Any
    ) -> SweepReport:
        """Bring the cache up to date. No ids means every group you are in."""
        return await sync_chats(
            self, self.store, chat_ids, limiter=self._limiter, **kwargs
        )

    async def sync_chat(self, chat_id: int, **kwargs: Any) -> Any:
        """Bring one chat's cache up to date."""
        return await sync_chat(
            self, self.store, chat_id, limiter=self._limiter, **kwargs
        )

    def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Search the cache. Offline — `sync()` is what fills it."""
        return self.store.search(query, **kwargs)

    # -- lifecycle ---------------------------------------------------------
    async def start(self, *, background: bool = False) -> None:
        """Connect and begin handling updates.

        Blocks until the client stops. `background=True` returns as soon as the
        connection is up, which is what a one-shot script — export the members
        of a group, then exit — needs instead of an event loop.
        """
        if not self.config.session_file.exists():
            raise SessionMissingError(
                f"no Bale session at {self.config.session_file}. "
                "Run `python -m bale_userbot login` once to authenticate."
            )
        await self.client.start(run_in_background=background)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
        if self._store is not None:
            self._store.close()
            self._store = None

    def run(self) -> None:
        """Start and block until the process is stopped.

        This is the "I own the process" entry point, so it is also where
        logging gets configured — `setup_logging` calls `basicConfig(force=True)`,
        which tears down every root handler and would clobber the configuration
        of a host that embeds `start()` instead. A host owns its own logging;
        only a process that is nothing but this app gets to set it.
        """
        import asyncio

        if not logging.getLogger().handlers:
            setup_logging(self.config.log_level)

        async def main() -> None:
            try:
                await self.start()
            finally:
                await self.stop()

        asyncio.run(main())
