"""The four repairs beyond `MessageContent` — each asserts a live-confirmed bug.

If one of these tests ever *fails* after a BaleClient upgrade, the upstream bug
may be fixed and the corresponding patch can be considered for removal.
"""

from __future__ import annotations

import pytest
from baleclient.dispatcher.event.handler import FilterObject
from baleclient.types import Member
from baleclient.types.responses import (
    DialogResponse,
    FullGroupResponse,
    MessageResponse,
)

import bale_userbot  # noqa: F401 - importing applies the wire fixes
from bale_userbot.groups import member_info
from bale_userbot.patches import MEMBER_NAME_KEY


# -- Member: display name in the date field ---------------------------------
def test_member_with_a_display_name_in_field_3_parses():
    # Exactly what a group's GetFullGroup returns for a named member.
    member = Member.model_validate({"1": 123, "3": "سولار تهران", "4": {"1": 1}})
    assert member.id == 123
    assert member.date is None  # it was never a date; the member survives
    assert bool(member.is_admin) is True
    # The name has to leave field "3" for the member to parse, but it is worth
    # keeping: it saves a LoadUsers round trip for every member that has one.
    assert member.display_name == "سولار تهران"


def test_member_with_a_real_date_keeps_it():
    member = Member.model_validate({"1": 123, "3": 1_700_000_000_000})
    assert member.date == 1_700_000_000_000
    assert getattr(member, MEMBER_NAME_KEY, None) is None


def test_a_blank_name_is_not_kept():
    # Whitespace is not a name; storing it would make `or` fallbacks pick it
    # over a real one fetched later.
    member = Member.model_validate({"1": 123, "3": "   "})
    assert getattr(member, MEMBER_NAME_KEY, None) is None


def test_the_kept_name_reaches_member_info():
    member = Member.model_validate({"1": 123, "3": "سولار تهران"})
    info = member_info(member, chat_id=42)
    assert info.name == "سولار تهران"
    # Still not a fetched profile: nothing else about the user is known.
    assert info.profile_loaded is False and info.username is None


def test_full_group_title_survives_a_named_member():
    # The original bug: one stringly member nuked the response, title included.
    response = FullGroupResponse.model_validate(
        {
            "1": {
                "1": 42,
                "3": {"1": "گروه آزمایشی"},
                "10": {"1": 5},
                "17": [{"1": 1, "3": "یک عضو با اسم"}],
            }
        }
    )
    assert response.fullgroup is not None
    assert response.fullgroup.title == "گروه آزمایشی"


# -- dispatcher: postponed annotations --------------------------------------
async def test_a_filter_with_string_annotations_is_dispatchable():
    # `from __future__ import annotations` in this module makes every
    # annotation a string; the unpatched dispatcher crashes on `__name__`.
    seen = {}

    async def gate(event: object, client: "Client | None" = None) -> bool:  # noqa: F821,UP037
        seen["client"] = client
        return True

    wrapped = FilterObject(gate)
    assert await wrapped.call("event", client="the-client") is True
    assert seen["client"] == "the-client"


async def test_client_parameter_is_still_injected_for_real_annotations():
    from baleclient import Client

    calls = {}

    async def gate(event, client: Client = None) -> bool:
        calls["client"] = client
        return True

    wrapped = FilterObject(gate)
    assert await wrapped.call("event", client="injected") is True
    assert calls["client"] == "injected"


# -- MessageResponse: the HTTP fallback -------------------------------------
def test_message_response_without_context_returns_empty_message():
    # session.post validates with neither context nor method_data.
    response = MessageResponse.model_validate({"2": 1_700_000_000_000})
    assert response.message is None


def test_message_response_with_an_existing_message_is_untouched():
    data = {"message": None, "2": 5}
    assert MessageResponse.model_validate(data).message is None


# -- DialogResponse: one bad dialog must not kill the page -------------------
def _dialog(peer_id: int, content: dict) -> dict:
    return {
        "1": {"1": 2, "2": peer_id},
        "3": 1_700_000_000_000,
        "4": 1,
        "5": 10,
        "6": 1_700_000_000_000,
        "7": content,
        "9": {"1": 0},
        "13": {"1": 0},
    }


def test_a_dialog_with_unparseable_content_keeps_its_peer():
    exotic = {"13": {"5": {"1": {"292289810440569675": 8872416474}}}}
    response = DialogResponse.model_validate(
        {"3": [_dialog(111, {"15": {"1": "سلام"}}), _dialog(222, exotic)]}
    )
    ids = [d.peer.id for d in response.dialogs]
    assert ids == [111, 222]  # both survive; the exotic one loses only content


def test_dialog_sort_dates_survive_content_stripping():
    exotic = {"13": {"5": {"1": {"9": 9}}}}
    response = DialogResponse.model_validate({"3": [_dialog(333, exotic)]})
    assert response.dialogs[0].sort_date == 1_700_000_000_000


def test_a_healthy_dialog_page_is_untouched():
    response = DialogResponse.model_validate(
        {"3": [_dialog(1, {"15": {"1": "a"}}), _dialog(2, {"15": {"1": "b"}})]}
    )
    assert len(response.dialogs) == 2
    assert response.dialogs[1].message.text == "b"


def test_an_entry_that_cannot_be_saved_is_dropped_not_fatal():
    response = DialogResponse.model_validate(
        {"3": [_dialog(1, {"15": {"1": "ok"}}), "garbage"]}
    )
    assert [d.peer.id for d in response.dialogs] == [1]


@pytest.mark.parametrize("shape", [{}, {"3": []}])
def test_empty_dialog_pages_parse(shape):
    assert DialogResponse.model_validate(shape).dialogs == []
