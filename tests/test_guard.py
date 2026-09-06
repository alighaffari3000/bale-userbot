"""A revoked login is recognised, backed off, and recovered from — no network.

The websocket is a fake that either stays quiet (healthy) or hands back a
close frame at once (what Bale does for a dead token).
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from baleclient.exceptions import BaleClientError

from bale_userbot.guard import (
    CLOSE_UNAUTHORIZED,
    GuardedSession,
    SessionRejected,
    hold_back_seconds,
)


class FakeWs:
    """Quiet, or closed with a code before anything is sent."""

    def __init__(self, close_code: int | None = None, first=None) -> None:
        self.close_code = close_code
        self.closed = close_code is not None
        self._first = first
        self.close_calls = 0

    async def receive(self, timeout=None):
        if self.close_code is not None:
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, self.close_code, "")
        if self._first is not None:
            first, self._first = self._first, None
            return first
        raise TimeoutError

    async def close(self):
        self.close_calls += 1


class FakeClient:
    """Only what the guard touches: the session file and the token."""

    _stopped = False

    def __init__(self, content: bytes | None = b"session-1") -> None:
        self.content = content
        self.token = "token-1"
        self.parsed: list[bytes] = []

    def _get_session_content(self):
        return self.content

    def _parse_session_content(self, data: bytes):
        self.parsed.append(data)
        self.token = "token-" + data.decode()[-1]


def make_session(*answers, client=None, **overrides) -> GuardedSession:
    """A session whose `ws_connect` yields the given fake sockets in order."""
    options = {"peek_seconds": 0.01, "hold_base": 0.05, "hold_cap": 0.2}
    options.update(overrides)
    session = GuardedSession(**options)
    session._bind_client(client or FakeClient())
    queue = list(answers)
    session.upgrades: list[str] = []

    async def fake_connect(token: str) -> None:
        session.upgrades.append(token)
        session.ws = queue.pop(0)
        session._running = True

    session._raw_connect = fake_connect
    return session


@pytest.fixture(autouse=True)
def _bypass_aiohttp(monkeypatch):
    async def connect(self, token):
        await self._raw_connect(token)

    monkeypatch.setattr("bale_userbot.guard.AiohttpSession.connect", connect)


# --- healthy ---------------------------------------------------------------


async def test_a_quiet_socket_is_a_good_connection():
    session = make_session(FakeWs())
    await session.connect("token-1")
    assert session.running
    assert not session.rejected


async def test_an_early_frame_is_handed_on_not_swallowed(monkeypatch):
    seen = []

    async def handle(self, data):
        seen.append(data)

    monkeypatch.setattr(GuardedSession, "_handle_received_data", handle)
    early = aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, b"\x01", None)
    session = make_session(FakeWs(first=early))
    await session.connect("token-1")
    await asyncio.sleep(0)
    assert seen == [b"\x01"]
    assert session.running


# --- rejection -------------------------------------------------------------


async def test_close_4401_means_the_login_is_revoked():
    session = make_session(FakeWs(CLOSE_UNAUTHORIZED))
    with pytest.raises(SessionRejected) as info:
        await session.connect("token-1")
    assert info.value.revoked
    assert info.value.code == CLOSE_UNAUTHORIZED
    assert session.rejected
    assert session.reject_code == CLOSE_UNAUTHORIZED
    assert session.reject_count == 1
    assert session.rejected_since is not None
    assert session.next_attempt_at > time.time()
    assert not session.running
    assert session.ws is None


async def test_one_dropped_connection_is_only_a_strike():
    session = make_session(FakeWs(1006))
    with pytest.raises(SessionRejected) as info:
        await session.connect("token-1")
    assert not info.value.revoked
    assert not session.rejected
    assert session.next_attempt_at == 0.0


async def test_three_immediate_drops_in_a_row_count_as_rejected():
    session = make_session(FakeWs(1006), FakeWs(1006), FakeWs(1006))
    for _ in range(2):
        with pytest.raises(SessionRejected):
            await session.connect("token-1")
        assert not session.rejected
    with pytest.raises(SessionRejected) as info:
        await session.connect("token-1")
    assert info.value.revoked
    assert session.rejected
    assert session.reject_code == 1006


async def test_a_good_connection_clears_the_strikes():
    session = make_session(FakeWs(1006), FakeWs(), FakeWs(1006), FakeWs(1006))
    with pytest.raises(SessionRejected):
        await session.connect("token-1")
    await session.connect("token-1")
    # Two more drops after a healthy connect are strikes 1 and 2, not 2 and 3.
    for _ in range(2):
        session._running = False
        with pytest.raises(SessionRejected):
            await session.connect("token-1")
    assert not session.rejected


# --- backing off -----------------------------------------------------------


def test_hold_back_doubles_and_caps():
    assert hold_back_seconds(0) == 0
    assert hold_back_seconds(1) == 30
    assert hold_back_seconds(2) == 60
    assert hold_back_seconds(3) == 120
    assert hold_back_seconds(6) == 900
    assert hold_back_seconds(50) == 900


async def test_a_rejected_login_is_not_retried_at_once():
    session = make_session(FakeWs(CLOSE_UNAUTHORIZED), FakeWs(CLOSE_UNAUTHORIZED))
    with pytest.raises(SessionRejected):
        await session.connect("token-1")
    started = time.monotonic()
    with pytest.raises(SessionRejected):
        await session.connect("token-1")
    assert time.monotonic() - started >= 0.04
    assert session.reject_count == 2
    # Doubling: the second hold is twice the first.
    assert session.next_attempt_at - time.time() > 0.05


async def test_a_drop_after_a_rejection_keeps_backing_off():
    """Live Bale alternates 4401 and a bare TCP drop for a dead token."""
    session = make_session(
        FakeWs(CLOSE_UNAUTHORIZED), FakeWs(1006), FakeWs(CLOSE_UNAUTHORIZED)
    )
    for expected_count in (1, 2, 3):
        with pytest.raises(SessionRejected) as info:
            await session.connect("token-1")
        assert info.value.revoked
        assert session.rejected
        assert session.reject_count == expected_count
    assert session.next_attempt_at - time.time() > 0.15


async def test_stopping_the_client_ends_the_hold_early():
    client = FakeClient()
    session = make_session(FakeWs(CLOSE_UNAUTHORIZED), client=client, hold_base=60.0)
    with pytest.raises(SessionRejected):
        await session.connect("token-1")
    client._stopped = True
    with pytest.raises(BaleClientError, match="stopping"):
        await asyncio.wait_for(session.connect("token-1"), timeout=3.0)


# --- recovery --------------------------------------------------------------


async def test_a_new_session_file_is_used_without_a_restart():
    client = FakeClient(b"session-1")
    session = make_session(
        FakeWs(CLOSE_UNAUTHORIZED), FakeWs(), client=client, hold_base=60.0
    )
    with pytest.raises(SessionRejected):
        await session.connect("token-1")

    # The operator logs in again: the file on disk changes.
    client.content = b"session-2"
    await asyncio.wait_for(session.connect("token-1"), timeout=3.0)

    assert client.parsed == [b"session-2"]
    assert session.upgrades[-1] == "token-2"
    assert not session.rejected
    assert session.running


async def test_an_unchanged_file_keeps_the_old_token():
    client = FakeClient(b"session-1")
    session = make_session(FakeWs(), FakeWs(), client=client)
    await session.connect("token-1")
    session._running = False
    await session.connect("token-1")
    assert client.parsed == []
    assert session.upgrades == ["token-1", "token-1"]


async def test_a_file_that_appears_mid_hold_cuts_the_hold_short():
    client = FakeClient(b"session-1")
    session = make_session(
        FakeWs(CLOSE_UNAUTHORIZED), FakeWs(), client=client, hold_base=60.0
    )
    with pytest.raises(SessionRejected):
        await session.connect("token-1")

    async def relogin():
        await asyncio.sleep(0.05)
        client.content = b"session-3"

    asyncio.create_task(relogin())
    await asyncio.wait_for(session.connect("token-1"), timeout=3.0)
    assert session.upgrades[-1] == "token-3"


async def test_a_corrupt_new_file_is_ignored():
    client = FakeClient(b"session-1")

    def broken(data):
        raise ValueError("garbage")

    client._parse_session_content = broken
    session = make_session(FakeWs(), FakeWs(), client=client)
    await session.connect("token-1")
    client.content = b"session-9"
    session._running = False
    await session.connect("token-1")
    assert session.upgrades == ["token-1", "token-1"]
