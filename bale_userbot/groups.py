"""Who is in a group or channel, and what we know about them.

BaleClient can list members, but not usefully. `Client.load_members` formats a
missing offset as the *string* `"None"` and sends it, so the very first page is
already asked for with a nonsense cursor; and `MembersResponse` models only the
member list, discarding whatever cursor the server sent back. This module calls
`LoadMembers` directly, omits the offset when there is none, and walks the pages
itself — reading the cursor out of `model_extra` when the server provides one,
and otherwise stopping on the only signal left: a page that adds no new ids.

A `Member` off the wire is an id and a role, nothing else — no name, no
username. `load_members(..., profiles=True)` fills those in through `LoadUsers`,
in chunks.

Nothing here writes anything down. `MemberInfo.as_dict()` and
`GroupInfo.as_dict()` hand back plain JSON-serializable dicts; *where* they are
stored — a JSON file, SQLite, Postgres — is the application's decision, not the
infrastructure's.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from baleclient import Client
from baleclient.enums import ChatType, GroupType, PeerType
from baleclient.methods import GetFullGroup, LoadMembers
from baleclient.types import (
    BoolValue,
    Condition,
    FullGroup,
    InfoPeer,
    Member,
    Permissions,
    ShortPeer,
    StringValue,
    User,
)
from baleclient.types.responses import FullGroupResponse, MembersResponse

from .patches import MEMBER_NAME_KEY

logger = logging.getLogger(__name__)

#: Members asked for per `LoadMembers` call.
DEFAULT_PAGE_SIZE = 100
#: Users asked for per `LoadUsers` call when filling in profiles. Bale rejects
#: very large peer lists, and a failed chunk costs less than a failed sweep.
PROFILE_CHUNK = 50
#: Backstop against a server that keeps handing out cursors forever.
MAX_PAGES = 1_000

#: `LoadMembers` filters. "contacts" narrows to members in your contact list;
#: "excepted_permissions" to those whose permissions differ from the group
#: default — in practice admins and restricted members.
MemberCondition = Literal["none", "contacts", "excepted_permissions"]

_CONDITIONS: dict[str, Condition] = {
    "contacts": Condition(contacts=BoolValue(value=True)),
    "excepted_permissions": Condition(excepted_permissions=BoolValue(value=True)),
}


class GroupUnavailableError(RuntimeError):
    """The server answered about a group with no group in it.

    `GetFullGroup` returns an empty envelope — not an error — for a chat this
    account cannot see: wrong id, a private group we never joined, one that was
    deleted. Upstream's `get_full_group` hands the `None` straight back and the
    caller trips over it one attribute later.
    """


# --- flat views -------------------------------------------------------------


def _as_bool(value: Any) -> bool:
    # `use_enum_values` and `IntBool` mean these arrive as 0/1 as often as bools.
    return bool(value)


@dataclass(frozen=True)
class MemberInfo:
    """One member of a group: their role, plus their profile once hydrated."""

    user_id: int
    chat_id: int
    is_admin: bool = False
    is_owner: bool = False
    inviter_id: int | None = None
    #: When they joined — often None. In a `GetFullGroup` response the server
    #: reuses this wire field for the member's display name, and `patches.py`
    #: drops the non-numeric value so the rest of the response survives.
    joined_at: int | None = None
    promoted_by: int | None = None
    promoted_at: int | None = None
    # Profile fields. All are None/False until `profile_loaded` is True, with
    # one exception: a group's own member list sometimes carries a display
    # name, and that arrives before any profile is fetched.
    name: str | None = None
    local_name: str | None = None
    username: str | None = None
    access_hash: int | None = None
    is_bot: bool = False
    is_deleted: bool = False
    account_created_at: int | None = None
    #: Whether the profile fields above were actually fetched. Without it a
    #: caller cannot tell "this member has no username" from "we never asked".
    profile_loaded: bool = False

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable record, for the caller to persist as it likes."""
        return asdict(self)


@dataclass(frozen=True)
class GroupInfo:
    """A flat view of a group or channel."""

    id: int
    title: str
    members_count: int
    chat_type: ChatType
    access_hash: int | None = None
    username: str | None = None
    about: str | None = None
    owner_id: int | None = None
    created_at: int | None = None
    is_joined: bool = False
    available_reactions: tuple[str, ...] = ()
    #: Names of the permissions every member has by default. Kept as names
    #: rather than a `Permissions` object so the record stays JSON-serializable.
    default_permissions: tuple[str, ...] = ()
    #: Whichever members `GetFullGroup` happened to include — usually the
    #: admins. Never the whole list; use `load_members()` for that.
    known_members: tuple[MemberInfo, ...] = ()

    @property
    def is_channel(self) -> bool:
        return self.chat_type == ChatType.CHANNEL

    @property
    def is_public(self) -> bool:
        return self.username is not None

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable record, for the caller to persist as it likes."""
        record = asdict(self)
        record["chat_type"] = int(self.chat_type)
        record["available_reactions"] = list(self.available_reactions)
        record["default_permissions"] = list(self.default_permissions)
        record["known_members"] = [m.as_dict() for m in self.known_members]
        return record


def member_info(
    member: Member, chat_id: int, *, owner_id: int | None = None
) -> MemberInfo:
    """Flatten one wire `Member`.

    Ownership is only knowable from the group, and a display name only
    sometimes present — `profile_loaded` stays False either way, because
    nothing else about the user has been fetched.
    """
    return MemberInfo(
        user_id=member.id,
        chat_id=chat_id,
        is_admin=_as_bool(member.is_admin),
        is_owner=owner_id is not None and member.id == owner_id,
        inviter_id=member.inviter_id,
        joined_at=member.date,
        promoted_by=member.promoted_by,
        promoted_at=member.promoted_at,
        # Free of charge when the server sent one; see `patches.MEMBER_NAME_KEY`.
        name=getattr(member, MEMBER_NAME_KEY, None),
    )


def _with_profile(member: MemberInfo, user: User) -> MemberInfo:
    return replace(
        member,
        name=user.name,
        local_name=user.local_name,
        username=user.username,
        access_hash=user.access_hash,
        is_bot=_as_bool(user.is_bot),
        is_deleted=_as_bool(user.is_deleted),
        account_created_at=user.created_at,
        profile_loaded=True,
    )


def permission_names(permissions: Permissions | None) -> tuple[str, ...]:
    """The permissions that are switched on, by name.

    Twenty booleans are unreadable in a log line and unstorable in a flat
    record; the names that are True say the same thing.
    """
    if permissions is None:
        return ()
    return tuple(
        name for name in Permissions.model_fields if getattr(permissions, name, False)
    )


def group_info(group: FullGroup) -> GroupInfo:
    """Flatten a wire `FullGroup`.

    `ex_info.expeer_type` is the only place the real chat type shows up — a
    super-group and a plain group share the same `group_type` — and every
    method that sends into the chat needs them told apart.
    """
    chat_type = ChatType.GROUP
    ex_info = group.ex_info
    if ex_info is not None and ex_info.expeer_type is not None:
        chat_type = ChatType(ex_info.expeer_type)
    elif int(group.group_type) == GroupType.CHANNEL:
        chat_type = ChatType.CHANNEL

    owner_id = group.owner_id
    return GroupInfo(
        id=group.id,
        title=group.title,
        members_count=group.members_count,
        chat_type=chat_type,
        access_hash=group.access_hash,
        username=group.username,
        about=group.about,
        owner_id=owner_id,
        created_at=group.created_at,
        is_joined=_as_bool(group.is_joined),
        available_reactions=tuple(group.available_reactions or ()),
        default_permissions=permission_names(group.default_permissions),
        known_members=tuple(
            member_info(m, group.id, owner_id=owner_id) for m in (group.members or ())
        ),
    )


# --- reading a group --------------------------------------------------------


async def fetch_group(
    client: Client, chat_id: int, *, access_hash: int = 1
) -> FullGroup:
    """The raw `FullGroup`, for the few callers that need its wire objects.

    `GetFullGroup` answers a chat this account cannot see with an empty
    envelope rather than an error, so the `None` is turned into one here — once,
    instead of in every caller.
    """
    result: FullGroupResponse = await client(
        GetFullGroup(group=ShortPeer(id=chat_id, access_hash=access_hash))
    )
    if result.fullgroup is None:
        raise GroupUnavailableError(
            f"no group {chat_id} visible to this account "
            "(wrong id, never joined, or deleted)"
        )
    return result.fullgroup


async def get_group(client: Client, chat_id: int, *, access_hash: int = 1) -> GroupInfo:
    """Everything the server will say about one group or channel."""
    return group_info(await fetch_group(client, chat_id, access_hash=access_hash))


async def member_count(client: Client, chat_id: int) -> int:
    """How many members a group has, without listing them.

    One request, and it counts everyone — including the members a paged sweep
    would never reach in a large channel.
    """
    return (await get_group(client, chat_id)).members_count


# --- listing members --------------------------------------------------------


def _offset_from(response: MembersResponse) -> str | None:
    """Read the pagination cursor `MembersResponse` forgets to model.

    Anything the server sent beyond the member list lands in `model_extra`.
    A cursor arrives either bare or wrapped as a `StringValue` (`{"1": ...}`).
    """
    for value in (response.model_extra or {}).values():
        if isinstance(value, dict):
            value = value.get("1")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        text = str(value)
        if text:
            return text
    return None


async def _members_page(
    client: Client,
    chat_id: int,
    *,
    limit: int,
    offset: str | None,
    condition: MemberCondition,
    access_hash: int,
) -> tuple[list[Member], str | None]:
    call = LoadMembers(
        group=ShortPeer(id=chat_id, access_hash=access_hash),
        limit=limit,
        # Upstream sends StringValue(value="None") here when there is no
        # offset; leaving the field out is what "start at the beginning" means.
        next_offset=StringValue(value=offset) if offset is not None else None,
        condition=_CONDITIONS.get(condition),
    )
    result: MembersResponse = await client(call)
    return list(result.members), _offset_from(result)


async def iter_members(
    client: Client,
    chat_id: int,
    *,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    condition: MemberCondition = "none",
    owner_id: int | None = None,
    access_hash: int = 1,
) -> AsyncIterator[MemberInfo]:
    """Yield members page by page, so a large group never has to fit in memory.

    Pagination stops when the server runs out of members, when `limit` is
    reached, or when a page repeats ids already yielded. That last rule is what
    keeps a server that ignores our cursor from looping forever: we return what
    we saw rather than spinning.
    """
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    seen: set[int] = set()
    offset: str | None = None

    for page in range(MAX_PAGES):
        want = page_size if limit is None else min(page_size, limit - len(seen))
        if want < 1:
            return

        members, offset = await _members_page(
            client,
            chat_id,
            limit=want,
            offset=offset,
            condition=condition,
            access_hash=access_hash,
        )
        if not members:
            return

        fresh = [m for m in members if m.id not in seen]
        if not fresh:
            logger.debug(
                "group %s: page %s repeated %s known members, stopping",
                chat_id,
                page,
                len(members),
            )
            return

        for member in fresh:
            seen.add(member.id)
            yield member_info(member, chat_id, owner_id=owner_id)
            if limit is not None and len(seen) >= limit:
                return

        if offset is None:
            # No cursor to advance with; the last id is the only thing the
            # server could mean by "continue after".
            offset = str(members[-1].id)
    else:
        logger.warning("group %s: stopped after %s member pages", chat_id, MAX_PAGES)


async def hydrate_profiles(
    client: Client,
    members: Sequence[MemberInfo],
    *,
    chunk: int = PROFILE_CHUNK,
) -> list[MemberInfo]:
    """Fill in names and usernames for members already listed.

    Members the server declines to describe — deleted accounts, mostly — come
    back unchanged with `profile_loaded` still False, rather than dropping out
    of the list.
    """
    if chunk < 1:
        raise ValueError("chunk must be at least 1")

    ids = list({m.user_id for m in members})
    profiles: dict[int, User] = {}

    for start in range(0, len(ids), chunk):
        peers = [
            InfoPeer(id=user_id, type=ChatType.PRIVATE)
            for user_id in ids[start : start + chunk]
        ]
        for user in await client.load_users(peers):
            profiles[user.id] = user

    missing = len(ids) - len(profiles)
    if missing:
        logger.debug("no profile returned for %s of %s members", missing, len(ids))

    return [
        _with_profile(m, profiles[m.user_id]) if m.user_id in profiles else m
        for m in members
    ]


async def load_members(
    client: Client,
    chat_id: int,
    *,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    condition: MemberCondition = "none",
    profiles: bool = False,
    access_hash: int = 1,
) -> list[MemberInfo]:
    """The whole member list of a group, in one call.

    `profiles=True` follows up with `LoadUsers` so every member carries a name
    and username; that costs one extra request per `PROFILE_CHUNK` members.
    The group itself is fetched first either way — it is the only source of
    `owner_id`, and an owner is not flagged as an admin on the wire.
    """
    group = await get_group(client, chat_id, access_hash=access_hash)
    members = [
        member
        async for member in iter_members(
            client,
            chat_id,
            limit=limit,
            page_size=page_size,
            condition=condition,
            owner_id=group.owner_id,
            access_hash=group.access_hash or access_hash,
        )
    ]
    return await hydrate_profiles(client, members) if profiles else members


async def load_admins(
    client: Client, chat_id: int, *, profiles: bool = False
) -> list[MemberInfo]:
    """Just the admins and the owner.

    Filtered server-side: `excepted_permissions` returns the members whose
    permissions differ from the group default, which is a far cheaper sweep
    than paging a whole channel to find four people.
    """
    members = await load_members(
        client, chat_id, condition="excepted_permissions", profiles=profiles
    )
    return [m for m in members if m.is_admin or m.is_owner]


# --- the account's own groups ----------------------------------------------


async def list_groups(
    client: Client,
    *,
    limit: int = 200,
    page_size: int = 40,
) -> list[GroupInfo]:
    """Every group and channel in this account's dialog list.

    `load_dialogs` only says `PeerType.GROUP` — it cannot tell a channel from a
    super-group, and it carries no title or member count. Each group-like peer
    therefore costs one `GetFullGroup`; a group that errors out is skipped with
    a log line rather than sinking the whole sweep.
    """
    seen: set[int] = set()
    groups: list[GroupInfo] = []
    offset_date = -1

    while len(groups) < limit:
        dialogs = await client.load_dialogs(limit=page_size, offset_date=offset_date)
        if not dialogs:
            break

        for dialog in dialogs:
            if int(dialog.peer.type) != PeerType.GROUP or dialog.peer.id in seen:
                continue
            seen.add(dialog.peer.id)
            try:
                groups.append(await get_group(client, dialog.peer.id))
            except Exception:
                logger.warning("skipping group %s", dialog.peer.id, exc_info=True)
            if len(groups) >= limit:
                break

        next_date = min(dialog.sort_date for dialog in dialogs)
        if next_date == offset_date:
            break
        offset_date = next_date

    return groups


# --- watching a membership change over time ---------------------------------


@dataclass(frozen=True)
class MembershipChanges:
    """What happened to a group's membership between two snapshots."""

    chat_id: int
    joined: tuple[MemberInfo, ...] = ()
    left: tuple[MemberInfo, ...] = ()
    promoted: tuple[MemberInfo, ...] = ()
    demoted: tuple[MemberInfo, ...] = ()
    #: Members whose name or username changed, as `(before, after)` pairs. Only
    #: ever populated when both snapshots carry profiles.
    renamed: tuple[tuple[MemberInfo, MemberInfo], ...] = ()

    def __bool__(self) -> bool:
        """False when nothing changed, so `if changes:` reads naturally."""
        return bool(
            self.joined or self.left or self.promoted or self.demoted or self.renamed
        )

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable record, for the caller to persist as it likes."""
        return {
            "chat_id": self.chat_id,
            "joined": [m.as_dict() for m in self.joined],
            "left": [m.as_dict() for m in self.left],
            "promoted": [m.as_dict() for m in self.promoted],
            "demoted": [m.as_dict() for m in self.demoted],
            "renamed": [
                {"before": before.as_dict(), "after": after.as_dict()}
                for before, after in self.renamed
            ],
        }


def diff_members(
    before: Iterable[MemberInfo], after: Iterable[MemberInfo]
) -> MembershipChanges:
    """Compare two member snapshots. Pure — it makes no requests.

    Storing snapshots and diffing them is what most "watch this group" jobs
    actually are, and keeping the comparison free of I/O means the application
    can diff against whatever it saved last night, from wherever it saved it.

    Renames are only reported when both snapshots were taken with profiles;
    otherwise every name is None on one side and every member would look
    renamed.
    """
    old = {m.user_id: m for m in before}
    new = {m.user_id: m for m in after}
    chat_id = next((m.chat_id for m in (*new.values(), *old.values())), 0)

    joined = [new[uid] for uid in new.keys() - old.keys()]
    left = [old[uid] for uid in old.keys() - new.keys()]

    promoted, demoted, renamed = [], [], []
    for uid in old.keys() & new.keys():
        was, is_now = old[uid], new[uid]
        was_admin = was.is_admin or was.is_owner
        is_admin = is_now.is_admin or is_now.is_owner
        if is_admin and not was_admin:
            promoted.append(is_now)
        elif was_admin and not is_admin:
            demoted.append(is_now)
        if (
            was.profile_loaded
            and is_now.profile_loaded
            and (was.name, was.username) != (is_now.name, is_now.username)
        ):
            renamed.append((was, is_now))

    def by_id(members: list[MemberInfo]) -> tuple[MemberInfo, ...]:
        return tuple(sorted(members, key=lambda m: m.user_id))

    return MembershipChanges(
        chat_id=chat_id,
        joined=by_id(joined),
        left=by_id(left),
        promoted=by_id(promoted),
        demoted=by_id(demoted),
        renamed=tuple(sorted(renamed, key=lambda pair: pair[1].user_id)),
    )


async def watch_membership(
    client: Client,
    chat_id: int,
    previous: Iterable[MemberInfo],
    *,
    profiles: bool = True,
    **kwargs: Any,
) -> tuple[MembershipChanges, list[MemberInfo]]:
    """Take a fresh snapshot and diff it against the last one.

    Returns the changes *and* the new snapshot, because the caller has to store
    that snapshot to have something to compare against next time.
    """
    current = await load_members(client, chat_id, profiles=profiles, **kwargs)
    return diff_members(previous, current), current


# --- records for the application to store -----------------------------------


def member_records(
    members: Iterable[MemberInfo], group: GroupInfo | None = None
) -> list[dict[str, Any]]:
    """Turn members into plain dicts, optionally stamped with their group.

    This is where bale_userbot stops. The rows are JSON-serializable and flat —
    `json.dump` them, hand them to `csv.DictWriter`, or feed them to
    `executemany`. Persistence is the application's call, not ours.
    """
    stamp = (
        {
            "group_id": group.id,
            "group_title": group.title,
            "group_username": group.username,
        }
        if group is not None
        else {}
    )
    return [{**member.as_dict(), **stamp} for member in members]


async def export_members(
    client: Client,
    chat_id: int,
    *,
    profiles: bool = True,
    limit: int | None = None,
    condition: MemberCondition = "none",
) -> tuple[GroupInfo, list[dict[str, Any]]]:
    """One call from a chat id to storable rows: the group, and its members.

    Returns data, never a file. What the caller does with the rows — write
    JSON, upsert into a table, diff against yesterday's snapshot — is theirs.
    """
    group = await get_group(client, chat_id)
    members = await load_members(
        client,
        chat_id,
        limit=limit,
        condition=condition,
        profiles=profiles,
        access_hash=group.access_hash or 1,
    )
    return group, member_records(members, group)
