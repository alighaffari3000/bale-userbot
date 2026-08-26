"""Member management, pinned messages and invite links."""

import pytest
from baleclient.enums import ChatType
from baleclient.types import BanData, Permissions

from bale_userbot.moderation import (
    PERMISSION_FLAGS,
    allow,
    banned,
    demote,
    invite_link,
    kick,
    mute,
    permissions_of,
    pins,
    promote,
    restrict,
    revoke_invite_link,
    set_default_permissions,
    set_permissions,
    unmute,
)
from tests import factories as f
from tests.test_groups import GROUP_ID, full_group

USER_ID = 77


class FakeClient:
    """Implements exactly the client surface `bale_userbot.moderation` touches."""

    def __init__(self, *, permissions=None, group=None, pins_pages=(), url="u/abc"):
        self.permissions = permissions or Permissions(
            see_message=True, send_message=True, send_media=True
        )
        self.group = group if group is not None else full_group()
        self.pins_pages = list(pins_pages)
        self.url = url
        self.actions: list[tuple] = []

    # -- members
    async def kick_user(self, chat_id, user_id):
        self.actions.append(("kick", chat_id, user_id))

    async def unban_user(self, chat_id, user_id):
        self.actions.append(("unban", chat_id, user_id))

    async def get_banned_users(self, chat_id):
        return [BanData(banned_id=USER_ID, banner_id=1)]

    async def make_user_admin(self, chat_id, user_id, admin_name=None):
        self.actions.append(("promote", chat_id, user_id, admin_name))

    async def remove_admin(self, chat_id, user_id):
        self.actions.append(("demote", chat_id, user_id))

    async def get_member_permissions(self, chat_id, user_id):
        return self.permissions

    async def set_member_permissions(self, chat_id, user_id, permissions):
        self.actions.append(("set_member", chat_id, user_id, permissions))
        self.permissions = permissions

    async def set_group_permissions(self, chat_id, permissions):
        self.actions.append(("set_group", chat_id, permissions))

    # -- group lookup, as `groups.fetch_group` issues it
    async def __call__(self, call):
        from baleclient.types.responses import FullGroupResponse

        return FullGroupResponse(fullgroup=self.group)

    # -- pins and links
    async def get_group_pins(self, chat_id, page=1, limit=20):
        from types import SimpleNamespace

        page_messages = (
            self.pins_pages[page - 1] if page <= len(self.pins_pages) else []
        )
        return SimpleNamespace(pins=list(page_messages[:limit]), count=0)

    async def pin_group_message(self, message, chat_id):
        self.actions.append(("pin", chat_id, message.message_id))

    async def get_group_link(self, chat_id):
        return self.url

    async def revoke_group_link(self, chat_id):
        return self.url + "-new"


def sent_permissions(client: FakeClient) -> Permissions:
    return next(a[-1] for a in reversed(client.actions) if a[0].startswith("set_"))


# --- members ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_kick_and_banned_list():
    client = FakeClient()
    await kick(client, GROUP_ID, USER_ID)
    assert client.actions == [("kick", GROUP_ID, USER_ID)]
    assert await banned(client, GROUP_ID) == [USER_ID]


@pytest.mark.asyncio
async def test_promote_and_demote():
    client = FakeClient()
    await promote(client, GROUP_ID, USER_ID, title="Moderator")
    await demote(client, GROUP_ID, USER_ID)
    assert client.actions == [
        ("promote", GROUP_ID, USER_ID, "Moderator"),
        ("demote", GROUP_ID, USER_ID),
    ]


@pytest.mark.asyncio
async def test_promote_grants_rights_in_the_same_call():
    # Bale keeps promotion and permissions apart; an admin who can do nothing
    # is almost never what the caller meant.
    client = FakeClient()
    await promote(client, GROUP_ID, USER_ID, delete_message=True)
    assert client.actions[0][0] == "promote"
    assert sent_permissions(client).delete_message is True


# --- permissions -----------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_one_permission_leaves_the_rest_alone():
    # The whole reason this layer exists: Permissions() defaults every flag to
    # False, so passing a fresh one would strip the member of everything.
    client = FakeClient(
        permissions=Permissions(see_message=True, send_message=True, send_media=True)
    )
    await restrict(client, GROUP_ID, USER_ID, "send_media")

    sent = sent_permissions(client)
    assert sent.send_media is False
    assert sent.see_message is True and sent.send_message is True


@pytest.mark.asyncio
async def test_allow_is_the_mirror_of_restrict():
    client = FakeClient(permissions=Permissions())
    await allow(client, GROUP_ID, USER_ID, "pin_message")
    assert sent_permissions(client).pin_message is True


@pytest.mark.asyncio
async def test_keyword_form_takes_explicit_values():
    client = FakeClient(permissions=Permissions(send_message=True, send_media=True))
    await set_permissions(
        client, GROUP_ID, USER_ID, send_message=False, pin_message=True
    )
    sent = sent_permissions(client)
    assert (sent.send_message, sent.pin_message, sent.send_media) == (False, True, True)


@pytest.mark.asyncio
async def test_a_misspelled_permission_is_refused_before_any_request():
    client = FakeClient()
    with pytest.raises(ValueError, match="unknown permission"):
        await set_permissions(client, GROUP_ID, USER_ID, send_mesage=False)
    assert client.actions == []


@pytest.mark.asyncio
async def test_mute_and_unmute_cover_every_way_of_speaking():
    client = FakeClient(permissions=Permissions(see_message=True, send_message=True))
    await mute(client, GROUP_ID, USER_ID)
    muted = sent_permissions(client)
    assert muted.send_message is False
    # Muting is not banishment: they can still read the chat.
    assert muted.see_message is True

    await unmute(client, GROUP_ID, USER_ID)
    assert sent_permissions(client).send_message is True


@pytest.mark.asyncio
async def test_group_defaults_start_from_the_groups_own_permissions():
    group = full_group()
    group.default_permissions = Permissions(see_message=True, send_message=True)
    client = FakeClient(group=group)
    await set_default_permissions(client, GROUP_ID, send_message=False)

    sent = sent_permissions(client)
    assert sent.send_message is False and sent.see_message is True


@pytest.mark.asyncio
async def test_permissions_of_reads_without_writing():
    client = FakeClient()
    assert (await permissions_of(client, GROUP_ID, USER_ID)).send_message is True
    assert client.actions == []


def test_every_permission_flag_is_offered():
    assert "send_message" in PERMISSION_FLAGS
    assert len(PERMISSION_FLAGS) == len(Permissions.model_fields)


# --- pins ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pins_carry_the_real_chat_type():
    # Upstream stamps every pin with ChatType.GROUP; in a channel that peer
    # type cannot be addressed by a reply or a reaction.
    pinned = f.message(f.text_content("rules"), chat_type=ChatType.GROUP)
    client = FakeClient(
        group=full_group(chat_type=ChatType.CHANNEL), pins_pages=[[pinned]]
    )
    (result,) = await pins(client, GROUP_ID)
    assert result.chat.type == ChatType.CHANNEL
    assert result.chat.id == GROUP_ID


@pytest.mark.asyncio
async def test_pins_page_until_the_server_runs_out():
    pages = [
        [f.message(f.text_content(f"p{i}"), message_id=i) for i in (1, 2)],
        [f.message(f.text_content("p3"), message_id=3)],
    ]
    client = FakeClient(pins_pages=pages)
    found = await pins(client, GROUP_ID, page_size=2)
    assert [m.message_id for m in found] == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_known_chat_type_skips_the_group_lookup():
    calls = []

    class Counted(FakeClient):
        async def __call__(self, call):
            calls.append(call)
            return await super().__call__(call)

    client = Counted(pins_pages=[[f.message(f.text_content("x"))]])
    await pins(client, GROUP_ID, chat_type=ChatType.SUPER_GROUP)
    assert calls == []


@pytest.mark.asyncio
async def test_no_pins_is_not_an_error():
    assert await pins(FakeClient(), GROUP_ID) == []


# --- invite links ----------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_link_and_its_revocation():
    client = FakeClient(url="https://ble.ir/join/abc")
    link = await invite_link(client, GROUP_ID)
    assert (link.url, link.chat_id, link.revoked) == (
        "https://ble.ir/join/abc",
        GROUP_ID,
        False,
    )

    fresh = await revoke_invite_link(client, GROUP_ID)
    assert fresh.url.endswith("-new") and fresh.revoked is True
    assert fresh.as_dict()["chat_id"] == GROUP_ID


# --- the facade ------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_defaults_are_reachable_from_the_app():
    # Every other permission call has a facade method; without this one the
    # group-wide baseline could only be changed through the raw client.
    from bale_userbot import BaleApp, Config

    app = BaleApp(Config())
    app._client = FakeClient()
    updated = await app.set_default_permissions(GROUP_ID, send_media=True)
    assert updated.send_media is True


@pytest.mark.asyncio
async def test_explicit_permission_values_are_reachable_from_the_app():
    from bale_userbot import BaleApp, Config

    app = BaleApp(Config())
    app._client = FakeClient(permissions=Permissions(send_message=True))
    updated = await app.set_permissions(GROUP_ID, USER_ID, send_message=False)
    assert updated.send_message is False
