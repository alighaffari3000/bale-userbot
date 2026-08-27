"""The MCP surface: read-only, served from the store, honest about limits."""

import pytest

pytest.importorskip("mcp", reason="the mcp extra is not installed")

from bale_mcp.server import BODY_CAP, build_server  # noqa: E402
from bale_userbot import Config, describe  # noqa: E402
from bale_userbot.store import MessageStore  # noqa: E402
from tests import factories as f  # noqa: E402

CHAT = 900

#: Tools that must exist, and the writes that must not.
EXPECTED_TOOLS = {
    "search_messages",
    "get_messages",
    "store_status",
    "sync_chats",
    "list_chats",
    "list_members",
    "read_chat",
}
FORBIDDEN_FRAGMENTS = ("send", "reply", "kick", "ban", "promote", "pin", "leave")


@pytest.fixture
def config(tmp_path):
    return Config(store_file=tmp_path / "m.db", session_file=tmp_path / "s.bale")


@pytest.fixture
def server(config):
    return build_server(config)


def fill(config, texts):
    """Put messages in the store the way a sync would."""
    with MessageStore(config.store_file) as store:
        store.put_chat(CHAT, title="سولار تهران")
        store.put_messages(
            describe(f.message(f.text_content(text), chat_id=CHAT, message_id=i))
            for i, text in enumerate(texts, start=1)
        )
        store.record_sync(CHAT)


async def call(server, tool, **arguments):
    result = await server.call_tool(tool, arguments)
    assert not result.is_error
    return result.structured_content


async def test_the_surface_is_exactly_the_read_tools(server):
    names = {tool.name for tool in await server.list_tools()}
    assert names == EXPECTED_TOOLS
    for name in names:
        assert not any(bad in name for bad in FORBIDDEN_FRAGMENTS)


async def test_search_serves_from_the_store(config):
    fill(config, ["پنل ۵۵۰ وات موجود", "باتری لیتیوم"])
    server = build_server(config)

    result = await call(server, "search_messages", query="پنل 550")

    assert result["total"] == 1
    assert result["hits"][0]["chat_title"] == "سولار تهران"


async def test_search_reports_the_honest_total(config):
    fill(config, [f"پنل شماره {i}" for i in range(30)])
    server = build_server(config)

    result = await call(server, "search_messages", query="پنل", limit=5)

    assert (result["returned"], result["total"], result["truncated"]) == (5, 30, True)


async def test_get_messages_returns_bodies_and_names_the_missing(config):
    fill(config, ["اولی", "دومی"])
    server = build_server(config)

    result = await call(server, "get_messages", chat_id=CHAT, message_ids=[1, 99])

    assert [m["body"] for m in result["messages"]] == ["اولی"]
    assert result["missing"] == [99]


async def test_get_messages_does_not_trim_the_body(config):
    fill(config, ["ط" * (BODY_CAP + 200)])
    server = build_server(config)

    result = await call(server, "get_messages", chat_id=CHAT, message_ids=[1])

    assert len(result["messages"][0]["body"]) == BODY_CAP + 200


async def test_store_status_shows_what_search_can_see(config):
    fill(config, ["پیام"])
    server = build_server(config)

    result = await call(server, "store_status")

    assert result["chats"][0]["chat_id"] == CHAT
    assert result["chats"][0]["title"] == "سولار تهران"
    assert result["chats"][0]["messages"] == 1


async def test_an_empty_store_answers_calmly(server):
    result = await call(server, "store_status")
    assert result["chats"] == []

    result = await call(server, "search_messages", query="هرچیزی")
    assert result["total"] == 0


async def test_network_tools_do_not_connect_at_build_time(config):
    # Building the server must not touch the session file or the network:
    # an agent that only searches should never pay for a connection.
    server = build_server(config)
    assert not (config.session_file.exists())
    result = await call(server, "search_messages", query="چیزی")
    assert result["total"] == 0
