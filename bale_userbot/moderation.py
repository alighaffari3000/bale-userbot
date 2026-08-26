"""Acting on a group: its members, its pinned messages, its invite link.

BaleClient has a call for each of these. What it does not have is a safe way to
change *one* permission. `Permissions` defaults every one of its twenty flags to
False, so `set_member_permissions(chat, user, Permissions(send_message=False))`
does not mute someone — it strips them of everything. `restrict()` and
`allow()` read the member's current permissions first and change only the flags
you name.

Bale has no separate "ban" call: removing someone is `kick()`, and the server
keeps a list you can read with `banned()` and clear with `unban()`.

`pins()` is here for a smaller reason: upstream's `get_group_pins` stamps every
pinned message with `ChatType.GROUP`, so in a channel or super-group the
messages come back mislabelled and anything that replies into that chat
addresses the wrong peer type.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from baleclient import Client
from baleclient.enums import ChatType
from baleclient.types import Chat, Message, Permissions

from .groups import GroupInfo, GroupUnavailableError, fetch_group, get_group, group_info

logger = logging.getLogger(__name__)

#: Pinned messages asked for per `GetPins` page.
PIN_PAGE_SIZE = 20
#: Backstop against a server that keeps paging pins forever.
MAX_PIN_PAGES = 100

#: Every flag `Permissions` carries, by the name `restrict()`/`allow()` take.
PERMISSION_FLAGS = tuple(Permissions.model_fields)


def _checked(flags: dict[str, bool]) -> dict[str, bool]:
    """Reject a misspelled flag loudly.

    Without this a typo would be accepted by `model_copy(update=...)` and land
    on the model as an extra field, silently changing nothing.
    """
    unknown = sorted(set(flags) - set(PERMISSION_FLAGS))
    if unknown:
        raise ValueError(
            f"unknown permission(s): {', '.join(unknown)}. "
            f"Known: {', '.join(PERMISSION_FLAGS)}"
        )
    return flags


# --- members ----------------------------------------------------------------


async def kick(client: Client, chat_id: int, user_id: int) -> Any:
    """Remove someone from a group or channel."""
    return await client.kick_user(chat_id, user_id)


async def unban(client: Client, chat_id: int, user_id: int) -> Any:
    """Let a removed user back in."""
    return await client.unban_user(chat_id, user_id)


async def banned(client: Client, chat_id: int) -> list[int]:
    """The user ids currently barred from a group."""
    return [ban.banned_id for ban in await client.get_banned_users(chat_id)]


async def promote(
    client: Client,
    chat_id: int,
    user_id: int,
    *,
    title: str | None = None,
    **permissions: bool,
) -> Any:
    """Make someone an admin.

    Promotion alone grants no rights — Bale keeps the two calls separate — so
    any permissions named here are applied straight afterwards, which is what
    "make them an admin who can delete messages" actually requires.
    """
    result = await client.make_user_admin(chat_id, user_id, admin_name=title)
    if permissions:
        await allow(client, chat_id, user_id, **permissions)
    return result


async def demote(client: Client, chat_id: int, user_id: int) -> Any:
    """Take admin rights back."""
    return await client.remove_admin(chat_id, user_id)


async def permissions_of(client: Client, chat_id: int, user_id: int) -> Permissions:
    """What one member is currently allowed to do."""
    return await client.get_member_permissions(chat_id, user_id)


async def set_permissions(
    client: Client, chat_id: int, user_id: int, **flags: bool
) -> Permissions:
    """Change only the named permissions, leaving every other flag as it is.

    Returns the permission set that was sent, so a caller can log or store the
    exact state it produced.
    """
    _checked(flags)
    current = await permissions_of(client, chat_id, user_id)
    updated = current.model_copy(update={name: bool(v) for name, v in flags.items()})
    await client.set_member_permissions(chat_id, user_id, updated)
    return updated


async def restrict(
    client: Client, chat_id: int, user_id: int, *names: str, **flags: bool
) -> Permissions:
    """Turn permissions off. `restrict(c, g, u, "send_message", "send_media")`."""
    return await set_permissions(
        client, chat_id, user_id, **dict.fromkeys(names, False), **flags
    )


async def allow(
    client: Client, chat_id: int, user_id: int, *names: str, **flags: bool
) -> Permissions:
    """Turn permissions on. The mirror of `restrict()`."""
    return await set_permissions(
        client, chat_id, user_id, **dict.fromkeys(names, True), **flags
    )


async def mute(client: Client, chat_id: int, user_id: int) -> Permissions:
    """Take away every way of speaking, leaving the member in the group."""
    return await restrict(
        client,
        chat_id,
        user_id,
        "send_message",
        "send_media",
        "send_gif_stickers",
        "send_link_message",
        "send_forwarded_message",
        "send_gift_packet",
    )


async def unmute(client: Client, chat_id: int, user_id: int) -> Permissions:
    """Give speech back."""
    return await allow(
        client,
        chat_id,
        user_id,
        "send_message",
        "send_media",
        "send_gif_stickers",
        "send_link_message",
        "send_forwarded_message",
        "send_gift_packet",
    )


async def set_default_permissions(
    client: Client, chat_id: int, **flags: bool
) -> Permissions:
    """Change the group's baseline permissions, one named flag at a time."""
    _checked(flags)
    # The raw group, not `GroupInfo`: the flat view keeps permission *names*,
    # and setting them back needs the object every other flag came from.
    group = await fetch_group(client, chat_id)
    current = group.default_permissions or Permissions()
    updated = current.model_copy(update={name: bool(v) for name, v in flags.items()})
    await client.set_group_permissions(chat_id, updated)
    return updated


# --- pinned messages --------------------------------------------------------


def _retype(
    messages: Iterable[Message], chat_id: int, chat_type: ChatType
) -> list[Message]:
    """Put the real chat back on messages upstream stamped `ChatType.GROUP`."""
    fixed = []
    for message in messages:
        message.chat = Chat(id=chat_id, type=chat_type)
        fixed.append(message)
    return fixed


async def pins(
    client: Client,
    chat_id: int,
    *,
    limit: int | None = None,
    page_size: int = PIN_PAGE_SIZE,
    chat_type: ChatType | None = None,
) -> list[Message]:
    """Every pinned message in a group or channel.

    The chat type is looked up unless given, because upstream hardcodes
    `ChatType.GROUP` on each pin and a channel's pins would otherwise carry a
    peer type that no reply or reaction can address.
    """
    if page_size < 1:
        raise ValueError("page_size must be at least 1")
    if chat_type is None:
        chat_type = (await get_group(client, chat_id)).chat_type

    found: list[Message] = []
    seen: set[int] = set()

    for page in range(1, MAX_PIN_PAGES + 1):
        want = page_size if limit is None else min(page_size, limit - len(found))
        if want < 1:
            break

        result = await client.get_group_pins(chat_id, page=page, limit=want)
        fresh = [m for m in result.pins if m.message_id not in seen]
        if not fresh:
            break

        seen.update(m.message_id for m in fresh)
        found.extend(_retype(fresh, chat_id, chat_type))

        if len(result.pins) < want:
            break
    else:
        logger.warning("group %s: stopped after %s pin pages", chat_id, MAX_PIN_PAGES)

    return found


async def pin(client: Client, chat_id: int, message: Message) -> Any:
    """Pin a message."""
    return await client.pin_group_message(message, chat_id)


async def unpin(client: Client, chat_id: int, message: Message) -> Any:
    """Unpin one message."""
    return await client.unpin_group_message(message, chat_id)


async def unpin_all(client: Client, chat_id: int) -> Any:
    """Clear every pin at once."""
    return await client.remove_group_pins(chat_id)


# --- invite links -----------------------------------------------------------


@dataclass(frozen=True)
class InviteLink:
    """A group's invite URL, and the group it opens."""

    url: str
    chat_id: int
    revoked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "chat_id": self.chat_id, "revoked": self.revoked}


async def invite_link(client: Client, chat_id: int) -> InviteLink:
    """The group's current invite link."""
    return InviteLink(url=await client.get_group_link(chat_id), chat_id=chat_id)


async def revoke_invite_link(client: Client, chat_id: int) -> InviteLink:
    """Kill the current invite link and return the replacement.

    Anyone still holding the old URL loses access; this is the undo for a link
    that spread further than intended.
    """
    return InviteLink(
        url=await client.revoke_group_link(chat_id), chat_id=chat_id, revoked=True
    )


async def preview(client: Client, token_or_url: str) -> GroupInfo:
    """What a group looks like from the outside, before joining it."""
    full = await client.get_group_preview(token_or_url)
    if full is None:
        raise GroupUnavailableError(f"no group behind {token_or_url!r}")
    return group_info(full)


async def join(client: Client, token_or_url: str) -> Any:
    """Join a group or channel from an invite link."""
    return await client.join_chat(token_or_url)


async def leave(client: Client, chat_id: int) -> Any:
    """Leave a group or channel."""
    return await client.leave_group(chat_id)
