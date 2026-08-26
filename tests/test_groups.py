"""Listing a group's members, and turning them into storable records."""

from dataclasses import replace

import pytest
from baleclient.enums import ChatType, GroupType, PeerType
from baleclient.methods import GetFullGroup, LoadMembers
from baleclient.types import FullGroup, Member, Peer, User
from baleclient.types.full_user import ExInfo
from baleclient.types.responses import FullGroupResponse, MembersResponse

from bale_userbot.groups import (
    GroupUnavailableError,
    diff_members,
    export_members,
    get_group,
    group_info,
    hydrate_profiles,
    iter_members,
    list_groups,
    load_admins,
    load_members,
    member_count,
    member_info,
    member_records,
    watch_membership,
)

GROUP_ID = 5000
OWNER_ID = 7
ADMIN_ID = 8


def full_group(
    *,
    members_count: int = 42,
    chat_type: ChatType = ChatType.SUPER_GROUP,
    members: list[Member] | None = None,
) -> FullGroup:
    return FullGroup(
        id=GROUP_ID,
        access_hash=1234,
        title="Cats",
        owner_id=OWNER_ID,
        created_at=1_600_000_000_000,
        group_type=GroupType.GROUP,
        members_count=members_count,
        username="cats",
        about="meow",
        members=members,
        ex_info=ExInfo(expeer_type=chat_type),
        available_reactions=["👍"],
    )


def member(user_id: int, *, is_admin: bool = False) -> Member:
    return Member(id=user_id, inviter_id=1, date=1_700_000_000_000, is_admin=is_admin)


def user(user_id: int, *, name: str = "Ali", username: str | None = None) -> User:
    return User(
        id=user_id,
        access_hash=9 * user_id,
        name=name,
        username=username,
        created_at=1_500_000_000_000,
        ex_info=ExInfo(expeer_type=ChatType.PRIVATE),
    )


class FakeClient:
    """Implements exactly the client surface `bale_userbot.groups` touches.

    `pages` is the sequence of `(members, cursor)` LoadMembers will answer with;
    `cursor` lands in `model_extra`, the same place a real response puts it.
    """

    def __init__(self, *, group=None, pages=(), users=(), dialogs=()):
        self.group = group if group is not None else full_group()
        self.pages = list(pages)
        self.users = {u.id: u for u in users}
        self.dialogs = list(dialogs)
        self.calls: list = []
        self.user_batches: list[list[int]] = []

    async def __call__(self, call):
        self.calls.append(call)
        if isinstance(call, GetFullGroup):
            return FullGroupResponse(fullgroup=self.group)
        if isinstance(call, LoadMembers):
            members, cursor = self.pages.pop(0) if self.pages else ([], None)
            payload = {
                "1": [m.model_dump(by_alias=True) for m in members[: call.limit]]
            }
            if cursor is not None:
                payload["2"] = {"1": cursor}
            return MembersResponse.model_validate(payload)
        raise AssertionError(f"unexpected call {type(call).__name__}")

    async def load_users(self, peers):
        self.user_batches.append([p.id for p in peers])
        return [self.users[p.id] for p in peers if p.id in self.users]

    async def load_dialogs(self, limit=40, offset_date=-1, exclude_pinned=False):
        return [
            d for d in self.dialogs if d.sort_date < offset_date or offset_date == -1
        ][:limit]


def load_members_calls(client: FakeClient) -> list[LoadMembers]:
    return [c for c in client.calls if isinstance(c, LoadMembers)]


# --- group facts -----------------------------------------------------------


@pytest.mark.asyncio
async def test_member_count_reads_the_group_total():
    client = FakeClient(group=full_group(members_count=1312))
    assert await member_count(client, GROUP_ID) == 1312


@pytest.mark.asyncio
async def test_missing_group_raises_instead_of_returning_none():
    class Empty(FakeClient):
        async def __call__(self, call):
            return FullGroupResponse(fullgroup=None)

    with pytest.raises(GroupUnavailableError):
        await get_group(Empty(), GROUP_ID)


def test_chat_type_comes_from_ex_info_not_group_type():
    # group_type says GROUP for a super-group; only ex_info tells them apart,
    # and every send_* call needs the real one.
    assert group_info(full_group()).chat_type == ChatType.SUPER_GROUP
    channel = group_info(full_group(chat_type=ChatType.CHANNEL))
    assert channel.chat_type == ChatType.CHANNEL and channel.is_channel


def test_owner_is_flagged_although_the_wire_never_says_so():
    info = group_info(
        full_group(members=[member(OWNER_ID), member(ADMIN_ID, is_admin=True)])
    )
    owner, admin = info.known_members
    assert (owner.is_owner, owner.is_admin) == (True, False)
    assert (admin.is_owner, admin.is_admin) == (False, True)


# --- pagination ------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_page_carries_no_offset():
    # Upstream sends StringValue(value="None") here — a literal "None" cursor.
    client = FakeClient(pages=[([member(1)], None)])
    await load_members(client, GROUP_ID)
    assert load_members_calls(client)[0].next_offset is None


@pytest.mark.asyncio
async def test_pages_follow_the_cursor_the_response_model_drops():
    client = FakeClient(
        pages=[([member(1), member(2)], "abc"), ([member(3)], None)],
    )
    members = await load_members(client, GROUP_ID, page_size=2)
    assert [m.user_id for m in members] == [1, 2, 3]
    assert [c.next_offset for c in load_members_calls(client)][1].value == "abc"


@pytest.mark.asyncio
async def test_without_a_cursor_the_last_id_is_the_offset():
    client = FakeClient(pages=[([member(1), member(2)], None), ([member(3)], None)])
    await load_members(client, GROUP_ID, page_size=2)
    assert load_members_calls(client)[1].next_offset.value == "2"


@pytest.mark.asyncio
async def test_a_repeated_page_ends_the_sweep():
    # A server that ignores the offset would otherwise loop forever.
    client = FakeClient(pages=[([member(1)], None)] * 20)
    members = await load_members(client, GROUP_ID, page_size=1)
    assert [m.user_id for m in members] == [1]
    assert len(load_members_calls(client)) == 2


@pytest.mark.asyncio
async def test_limit_stops_mid_page_and_never_over_asks():
    client = FakeClient(pages=[([member(1), member(2), member(3)], "next")])
    members = await load_members(client, GROUP_ID, limit=2, page_size=10)
    assert [m.user_id for m in members] == [1, 2]
    assert load_members_calls(client)[0].limit == 2


@pytest.mark.asyncio
async def test_iter_members_streams_without_fetching_the_group():
    client = FakeClient(pages=[([member(1)], None)])
    seen = [m.user_id async for m in iter_members(client, GROUP_ID)]
    assert seen == [1]
    assert not [c for c in client.calls if isinstance(c, GetFullGroup)]


@pytest.mark.asyncio
async def test_page_size_must_be_positive():
    with pytest.raises(ValueError):
        [m async for m in iter_members(FakeClient(), GROUP_ID, page_size=0)]


# --- profiles --------------------------------------------------------------


def listed(*user_ids: int) -> list:
    """Members as `iter_members` would yield them: ids and roles, no profile."""
    return [member_info(member(user_id), GROUP_ID) for user_id in user_ids]


@pytest.mark.asyncio
async def test_profiles_are_fetched_for_a_whole_member_list():
    ids = list(range(1, 6))
    client = FakeClient(
        pages=[([member(i) for i in ids], None)],
        users=[user(i, name=f"user{i}") for i in ids],
    )
    members = await load_members(client, GROUP_ID, profiles=True)
    assert all(m.profile_loaded for m in members)
    assert {m.name for m in members} == {f"user{i}" for i in ids}


@pytest.mark.asyncio
async def test_hydration_is_chunked():
    client = FakeClient(users=[user(i) for i in range(1, 6)])
    await hydrate_profiles(client, listed(1, 2, 3, 4, 5), chunk=2)
    assert [len(batch) for batch in client.user_batches] == [2, 2, 1]


@pytest.mark.asyncio
async def test_a_member_without_a_profile_survives_hydration():
    client = FakeClient(users=[user(1)])
    kept, unknown = await hydrate_profiles(client, listed(1, 2))
    assert kept.profile_loaded and kept.name == "Ali"
    assert unknown.user_id == 2 and not unknown.profile_loaded and unknown.name is None


# --- admins ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_admins_filter_server_side_and_include_the_owner():
    client = FakeClient(
        pages=[([member(OWNER_ID), member(ADMIN_ID, is_admin=True), member(99)], None)]
    )
    admins = await load_admins(client, GROUP_ID)
    assert {m.user_id for m in admins} == {OWNER_ID, ADMIN_ID}
    assert load_members_calls(client)[0].condition.excepted_permissions.value is True


# --- records ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_returns_flat_rows_and_writes_nothing():
    client = FakeClient(pages=[([member(1)], None)], users=[user(1, username="ali")])
    group, rows = await export_members(client, GROUP_ID)
    assert group.title == "Cats"
    assert rows == [
        {
            "user_id": 1,
            "chat_id": GROUP_ID,
            "is_admin": False,
            "is_owner": False,
            "inviter_id": 1,
            "joined_at": 1_700_000_000_000,
            "promoted_by": None,
            "promoted_at": None,
            "name": "Ali",
            "local_name": None,
            "username": "ali",
            "access_hash": 9,
            "is_bot": False,
            "is_deleted": False,
            "account_created_at": 1_500_000_000_000,
            "profile_loaded": True,
            "group_id": GROUP_ID,
            "group_title": "Cats",
            "group_username": "cats",
        }
    ]


def test_records_without_a_group_carry_no_stamp():
    info = group_info(full_group(members=[member(OWNER_ID)]))
    (row,) = member_records(info.known_members)
    assert "group_id" not in row


def test_group_as_dict_is_json_serializable():
    import json

    record = group_info(full_group(members=[member(OWNER_ID)])).as_dict()
    assert json.loads(json.dumps(record))["chat_type"] == int(ChatType.SUPER_GROUP)


# --- the account's own groups ----------------------------------------------


class Dialog:
    def __init__(self, peer_id, peer_type, sort_date):
        self.peer = Peer(id=peer_id, type=peer_type)
        self.sort_date = sort_date


@pytest.mark.asyncio
async def test_list_groups_skips_private_peers():
    client = FakeClient(
        dialogs=[
            Dialog(GROUP_ID, PeerType.GROUP, 300),
            Dialog(42, PeerType.PRIVATE, 200),
        ]
    )
    groups = await list_groups(client)
    assert [g.id for g in groups] == [GROUP_ID]


@pytest.mark.asyncio
async def test_a_broken_group_does_not_sink_the_sweep():
    class Flaky(FakeClient):
        async def __call__(self, call):
            if isinstance(call, GetFullGroup) and call.group.id == 999:
                raise RuntimeError("gone")
            return await super().__call__(call)

    client = Flaky(
        dialogs=[
            Dialog(999, PeerType.GROUP, 300),
            Dialog(GROUP_ID, PeerType.GROUP, 200),
        ]
    )
    assert [g.id for g in await list_groups(client)] == [GROUP_ID]


# --- watching membership over time -----------------------------------------


def snapshot(*specs) -> list:
    """Members from `(user_id, is_admin, name)` triples, profiles loaded."""
    return [
        replace(
            member_info(member(uid, is_admin=is_admin), GROUP_ID),
            name=name,
            profile_loaded=True,
        )
        for uid, is_admin, name in specs
    ]


def test_nothing_changed_is_falsy():
    before = snapshot((1, False, "Ali"))
    changes = diff_members(before, before)
    assert not changes
    assert changes.chat_id == GROUP_ID


def test_joins_and_departures_are_separated():
    changes = diff_members(
        snapshot((1, False, "Ali"), (2, False, "Sara")),
        snapshot((2, False, "Sara"), (3, False, "Reza")),
    )
    assert [m.user_id for m in changes.joined] == [3]
    assert [m.user_id for m in changes.left] == [1]
    assert changes


def test_promotion_and_demotion_are_noticed():
    changes = diff_members(
        snapshot((1, False, "Ali"), (2, True, "Sara")),
        snapshot((1, True, "Ali"), (2, False, "Sara")),
    )
    assert [m.user_id for m in changes.promoted] == [1]
    assert [m.user_id for m in changes.demoted] == [2]


def test_becoming_owner_counts_as_promotion():
    before = snapshot((1, False, "Ali"))
    after = [replace(before[0], is_owner=True)]
    assert [m.user_id for m in diff_members(before, after).promoted] == [1]


def test_renames_are_reported_as_before_and_after():
    (before, after) = (snapshot((1, False, "Ali")), snapshot((1, False, "Ali Reza")))
    ((was, is_now),) = diff_members(before, after).renamed
    assert (was.name, is_now.name) == ("Ali", "Ali Reza")


def test_renames_are_not_guessed_from_unhydrated_snapshots():
    # Without profiles every name is None on both sides; reporting a rename
    # there would fire on every single member of every group.
    bare = [member_info(member(1), GROUP_ID)]
    assert diff_members(bare, bare).renamed == ()
    assert diff_members(bare, snapshot((1, False, "Ali"))).renamed == ()


def test_changes_are_json_serializable():
    import json

    changes = diff_members(snapshot((1, False, "Ali")), snapshot((2, True, "Sara")))
    record = json.loads(json.dumps(changes.as_dict()))
    assert record["joined"][0]["user_id"] == 2
    assert record["left"][0]["user_id"] == 1


@pytest.mark.asyncio
async def test_watch_returns_the_new_snapshot_to_store():
    client = FakeClient(
        pages=[([member(1), member(2)], None)], users=[user(1), user(2)]
    )
    changes, current = await watch_membership(
        client, GROUP_ID, snapshot((1, False, "Ali"))
    )
    assert [m.user_id for m in changes.joined] == [2]
    assert [m.user_id for m in current] == [1, 2]


# --- names the group list carries on its own --------------------------------


@pytest.mark.asyncio
async def test_a_wire_name_survives_a_failed_profile_lookup():
    # patches.py keeps the display name the server puts in the join-date
    # field; a member LoadUsers refuses to describe still has it.
    named = Member.model_validate({"1": 1, "3": "Ali"})
    client = FakeClient(
        pages=[([named, member(2)], None)], users=[user(2, name="Sara")]
    )
    ali, sara = await load_members(client, GROUP_ID, profiles=True)

    assert (ali.name, ali.profile_loaded) == ("Ali", False)
    assert (sara.name, sara.profile_loaded) == ("Sara", True)


@pytest.mark.asyncio
async def test_a_fetched_profile_wins_over_the_wire_name():
    named = Member.model_validate({"1": 1, "3": "Old Name"})
    client = FakeClient(pages=[([named], None)], users=[user(1, name="New Name")])
    (only,) = await load_members(client, GROUP_ID, profiles=True)
    assert only.name == "New Name" and only.profile_loaded is True


@pytest.mark.asyncio
async def test_profiles_can_be_filled_in_later_from_the_app():
    # A stored snapshot has ids but no names; hydrating it must not require
    # re-listing the whole group.
    from bale_userbot import BaleApp, Config

    app = BaleApp(Config())
    app._client = FakeClient(users=[user(1, name="Ali")])
    (only,) = await app.hydrate_profiles([member_info(member(1), GROUP_ID)])
    assert only.name == "Ali" and only.profile_loaded is True
