"""Noticing a dead login instead of retrying it every five seconds.

When Bale revokes an account's authorisations — the "log out of every device"
action, which also throws the phone out — the websocket still *upgrades*: the
edge answers `101 Switching Protocols` and then closes the socket at once,
either with close code 4401 or by dropping the TCP connection (aiohttp reports
that as 1006). `BaleClient.Client.start` cannot tell that apart from a network
blip: its loop reconnects with the same dead token forever, five seconds
apart, and the only trace is `Cannot write to closing transport` from the
handshake write. Observed on a live account on 2026-09-06: 339 attempts in
one hour, and nothing that said "you are logged out".

`GuardedSession` is the stock `AiohttpSession` with three additions:

* after `ws_connect` it *peeks* briefly at the socket. A close frame that
  arrives before anything was sent is a rejection, not a hiccup — 4401 says so
  outright; any other code counts as a strike, and three strikes in a row are
  treated the same way;
* once rejected it holds every further attempt back, doubling from
  `HOLD_BASE` up to `HOLD_CAP`, so a revoked token is tried a few times an
  hour rather than seven hundred;
* before each attempt it re-reads the session file. A new login written by
  `python -m bale_userbot login --replace` is picked up on the next attempt
  without restarting the process.

The state it keeps (`rejected`, `reject_code`, `rejected_since`,
`reject_count`, `next_attempt_at`) is plain attributes, so a host can show
"logged out since 09:30, next try in 12 minutes" and alert its operator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from baleclient.client.session import AiohttpSession
from baleclient.exceptions import BaleClientError

logger = logging.getLogger(__name__)

#: Bale's own close code for a token it no longer accepts.
CLOSE_UNAUTHORIZED = 4401

#: How long to listen for an immediate close after the upgrade. A healthy
#: server sends nothing before the client's handshake, so this is the price
#: of every successful connect.
PEEK_SECONDS = 0.5

#: Immediate closes with a code other than 4401 needed before the login is
#: declared rejected. One is a blip; three in a row are not.
IMMEDIATE_CLOSE_STRIKES = 3

#: Hold-back before reconnecting a rejected login: base, doubling, and cap.
HOLD_BASE = 30.0
HOLD_CAP = 900.0

#: How finely the hold-back sleeps, so a stop request or a new session file
#: is noticed promptly.
_HOLD_SLICE = 1.0

_CLOSED_TYPES = frozenset(
    {
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.ERROR,
    }
)


class SessionRejected(BaleClientError):
    """Bale closed the websocket straight after accepting the upgrade."""

    def __init__(self, code: int | None, strikes: int, *, revoked: bool) -> None:
        self.code = code
        self.strikes = strikes
        self.revoked = revoked
        if revoked and code == CLOSE_UNAUTHORIZED:
            what = (
                f"Bale rejected the login (close code {code}): the session was "
                "revoked — log in again with `python -m bale_userbot login --replace`"
            )
        elif revoked:
            what = (
                f"Bale closes the websocket right after connecting ({strikes} times "
                f"in a row, last code {code}); treating the login as rejected"
            )
        else:
            what = (
                f"Bale closed the websocket right after connecting (code {code}, "
                f"strike {strikes}/{IMMEDIATE_CLOSE_STRIKES})"
            )
        super().__init__(what)


def hold_back_seconds(
    attempt: int, base: float = HOLD_BASE, cap: float = HOLD_CAP
) -> float:
    """Delay before the `attempt`-th reconnect of a rejected login (1-based)."""
    if attempt <= 0:
        return 0.0
    return min(cap, base * (2 ** (attempt - 1)))


class GuardedSession(AiohttpSession):
    """`AiohttpSession` that recognises a revoked login and backs off."""

    def __init__(
        self,
        *args: Any,
        peek_seconds: float = PEEK_SECONDS,
        hold_base: float = HOLD_BASE,
        hold_cap: float = HOLD_CAP,
        strikes: int = IMMEDIATE_CLOSE_STRIKES,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.peek_seconds = peek_seconds
        self.hold_base = hold_base
        self.hold_cap = hold_cap
        self.strike_limit = strikes

        #: True once the login is considered dead: 4401, or repeated
        #: immediate closes. Cleared by the next successful connect or by a
        #: changed session file.
        self.rejected: bool = False
        self.reject_code: int | None = None
        #: `time.time()` of the first rejection of the current streak.
        self.rejected_since: float | None = None
        #: Rejections in a row, including the first.
        self.reject_count: int = 0
        #: `time.time()` before which no reconnect will be attempted.
        self.next_attempt_at: float = 0.0
        self.last_error: str | None = None

        self._strikes = 0
        self._loaded_session: bytes | None = None

    # -- the connect path --------------------------------------------------
    async def connect(self, token: str) -> None:
        token = self._refresh_token(token)
        token = await self._hold_back(token)
        await super().connect(token)
        await self._peek()

    async def _peek(self) -> None:
        """Catch a server that upgrades and closes in the same breath."""
        ws = self.ws
        if ws is None:
            return
        try:
            message = await ws.receive(timeout=self.peek_seconds)
        except TimeoutError:
            # Nothing arrived: the socket is up and quiet, as it should be.
            self._clear_rejection()
            return

        if message.type in _CLOSED_TYPES:
            code = ws.close_code
            if code is None and isinstance(message.data, int):
                code = message.data
            self._running = False
            await self._drop_socket()
            raise self._note_rejection(code)

        # A real frame before the handshake: hand it to the usual path.
        if message.type == aiohttp.WSMsgType.BINARY:
            asyncio.create_task(self._handle_received_data(message.data))
        self._clear_rejection()

    async def _drop_socket(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - it is already gone
                pass

    # -- rejection bookkeeping --------------------------------------------
    def _note_rejection(self, code: int | None) -> SessionRejected:
        self._strikes += 1
        # Once the login is known to be dead, a bare TCP drop (1006) on the
        # next try is the same rejection wearing a different code — seen live:
        # Bale alternates the two — and must keep the hold-back growing.
        declared = (
            self.rejected
            or code == CLOSE_UNAUTHORIZED
            or self._strikes >= self.strike_limit
        )
        if declared:
            now = time.time()
            if not self.rejected:
                self.rejected_since = now
            self.rejected = True
            self.reject_code = code
            self.reject_count += 1
            self.next_attempt_at = now + hold_back_seconds(
                self.reject_count, self.hold_base, self.hold_cap
            )
        error = SessionRejected(code, self._strikes, revoked=declared)
        self.last_error = str(error)
        if declared:
            logger.error(
                "%s; next attempt in %.0fs",
                error,
                max(0.0, self.next_attempt_at - time.time()),
            )
        else:
            logger.warning("%s", error)
        return error

    def _clear_rejection(self, why: str | None = None) -> None:
        if self.rejected and why:
            logger.info("login rejection cleared: %s", why)
        self.rejected = False
        self.reject_code = None
        self.rejected_since = None
        self.reject_count = 0
        self.next_attempt_at = 0.0
        self.last_error = None
        self._strikes = 0

    # -- holding back ------------------------------------------------------
    async def _hold_back(self, token: str) -> str:
        """Sleep out the hold-back, unless the process stops or a new login
        appears on disk. Returns the token to connect with."""
        while True:
            remaining = self.next_attempt_at - time.time()
            if remaining <= 0:
                return token
            if self._stopped():
                raise BaleClientError("client is stopping; not reconnecting")
            await asyncio.sleep(min(_HOLD_SLICE, remaining))
            fresh = self._refresh_token(token)
            if fresh != token or not self.rejected:
                return fresh

    def _stopped(self) -> bool:
        return bool(getattr(self.client, "_stopped", False))

    # -- picking up a new login --------------------------------------------
    def _refresh_token(self, token: str) -> str:
        """Reload the token if the session file changed since it was read."""
        client = self.client
        if client is None:
            return token
        try:
            content = client._get_session_content()
        except OSError as exc:
            logger.warning("could not read the session file: %s", exc)
            return token
        if self._loaded_session is None:
            self._loaded_session = content
            return token
        if content is None or content == self._loaded_session:
            return token

        try:
            client._parse_session_content(content)
        except Exception as exc:  # noqa: BLE001 - a half-written file, most likely
            logger.warning("new session file could not be parsed: %s", exc)
            return token
        self._loaded_session = content
        self._clear_rejection("the session file changed on disk")
        logger.info("session file changed; connecting with the new login")
        return client.token or token
