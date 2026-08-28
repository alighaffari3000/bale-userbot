"""Filling the store from Bale: paced, incremental, and honest about failure.

This is the only module that reads from the network and writes to the cache,
and it exists because doing that well is fiddly in three ways a caller should
not have to re-solve.

*Paced.* Every request goes through one `RateLimiter`, so a sweep of twenty
groups cannot spend the account's whole budget in the first four.

*Incremental.* A chat already in the store is refetched only from its newest
stored message, not from `since`. The second sweep of a quiet group is one
page; a naive re-sync is four thousand messages.

*Honest.* One group in a real account will fail — a page the library cannot
parse, a websocket that drops mid-walk — and a sweep that raises on it leaves
the caller with nothing and no idea how far it got. Each chat is reported
separately: what synced, what did not, and why.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from baleclient.enums import ChatType

from .content import describe
from .groups import load_user_profiles
from .history import TimeLike, load_history, to_timestamp
from .limiter import RateLimiter
from .store import MessageStore

if TYPE_CHECKING:
    from .app import BaleApp

logger = logging.getLogger(__name__)

#: Messages per request while syncing. Larger than the interactive default:
#: a sweep is bounded by round trips, and each one costs rate-limit budget.
SYNC_PAGE_SIZE = 200
#: How far back a first sync of a chat reaches when the caller names no date.
DEFAULT_WINDOW_DAYS = 30
#: Re-read this much of what we already have, in case the last page was
#: written while messages were still arriving in the same millisecond.
OVERLAP_MS = 1_000


@dataclass
class SyncReport:
    """What one chat's sync did. `error` set means nothing was written."""

    chat_id: int
    title: str | None = None
    fetched: int = 0
    stored: int = 0
    #: New senders named during this sync — cosmetic, never a failure cause.
    users: int = 0
    oldest_date: int | None = None
    newest_date: int | None = None
    incremental: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "fetched": self.fetched,
            "stored": self.stored,
            "users": self.users,
            "oldest_date": self.oldest_date,
            "newest_date": self.newest_date,
            "incremental": self.incremental,
            "error": self.error,
        }


@dataclass
class SweepReport:
    """Every chat a sweep touched, plus what the pacing cost."""

    chats: list[SyncReport] = field(default_factory=list)
    calls: int = 0
    rate_limited: int = 0

    @property
    def failed(self) -> list[SyncReport]:
        return [report for report in self.chats if not report.ok]

    @property
    def stored(self) -> int:
        return sum(report.stored for report in self.chats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chats": [report.as_dict() for report in self.chats],
            "stored": self.stored,
            "failed": len(self.failed),
            "calls": self.calls,
            "rate_limited": self.rate_limited,
        }


def _default_since() -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()


async def sync_chat(
    app: BaleApp,
    store: MessageStore,
    chat_id: int,
    chat_type: ChatType = ChatType.GROUP,
    *,
    since: TimeLike | None = None,
    limit: int | None = None,
    limiter: RateLimiter | None = None,
    title: str | None = None,
    full: bool = False,
) -> SyncReport:
    """Bring one chat's stored history up to date. Never raises.

    `since` bounds a first sync; afterwards the walk starts at what the store
    already has, unless `full=True` asks for the whole window again. A failure
    is returned in `SyncReport.error`, because a sweep must survive it.
    """
    limiter = limiter or RateLimiter()
    report = SyncReport(chat_id=chat_id, title=title)

    # An explicit `since` is a floor the caller chose; the default window is
    # only a stand-in for one, so resuming is allowed to ignore it. Without
    # that distinction a chat quiet for longer than the window would be
    # re-walked from the window's edge on every sync, forever.
    floor = to_timestamp(since)
    start = floor if floor is not None else to_timestamp(_default_since())
    stored_newest = None if full else store.newest_date(chat_id)
    if stored_newest is not None:
        # Overlap slightly: the last page may have been cut mid-millisecond.
        resume = stored_newest - OVERLAP_MS
        start = max(resume, floor) if floor is not None else resume
        report.incremental = True

    try:
        messages = await limiter.run(
            lambda: load_history(
                app.client,
                chat_id,
                chat_type,
                since=start,
                limit=limit,
                page_size=SYNC_PAGE_SIZE,
            ),
            what=f"history({chat_id})",
        )
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        logger.warning("chat %s: sync failed — %s", chat_id, report.error)
        return report

    infos = [describe(message) for message in messages]
    report.fetched = len(infos)
    report.stored = store.put_messages(infos)
    report.users = await _resolve_new_senders(app, store, infos, limiter)
    if title:
        store.put_chat(chat_id, title=title)
    else:
        title = await _remember_title(app, store, chat_id, limiter)
    report.title = title
    state = store.record_sync(chat_id)
    report.oldest_date, report.newest_date = state.oldest_date, state.newest_date
    logger.info(
        "chat %s: %s fetched, %s stored%s",
        chat_id,
        report.fetched,
        report.stored,
        " (incremental)" if report.incremental else "",
    )
    return report


async def _resolve_new_senders(
    app: BaleApp,
    store: MessageStore,
    infos: Sequence[Any],
    limiter: RateLimiter,
) -> int:
    """Look up the senders this page introduced. Returns how many were named.

    Sync is where the network already is, so this is where a sender id turns
    into a name — search stays offline and still reads like people, not
    numbers. Only *unknown* senders are asked about, so a busy group costs
    one request on the first sweep and none on the next.

    Cosmetic by nature: a failure here must not fail a sync that already
    stored its messages.
    """
    senders = {info.sender_id for info in infos if info.sender_id}
    unknown = sorted(senders - store.known_user_ids())
    if not unknown:
        return 0

    try:
        profiles = await limiter.run(
            lambda: load_user_profiles(app.client, unknown),
            what=f"users({len(unknown)})",
        )
    except Exception as exc:
        logger.debug("sender lookup failed (%s); ids stay unnamed", type(exc).__name__)
        return 0

    # Ids the server would not describe are recorded as nameless rather than
    # left out: without that, every later sweep asks about them again.
    rows = [
        (
            user_id,
            getattr(profiles.get(user_id), "name", None),
            getattr(profiles.get(user_id), "username", None),
        )
        for user_id in unknown
    ]
    store.put_users(rows)
    return sum(1 for _, name, _ in rows if name)


async def _remember_title(
    app: BaleApp, store: MessageStore, chat_id: int, limiter: RateLimiter
) -> str | None:
    """Look a chat's name up once, so results read as names and not ids.

    Syncing chats by id skips the group listing that would have supplied the
    titles, and a search result reading `1330049202` helps nobody. One extra
    request per chat, and only the first time — after that the store has it.
    A failure here is cosmetic, so it is swallowed.
    """
    known = store.chat_titles().get(chat_id)
    if known:
        return known
    try:
        group = await limiter.run(lambda: app.group(chat_id), what=f"group({chat_id})")
    except Exception as exc:
        logger.debug("chat %s: no title (%s)", chat_id, type(exc).__name__)
        return None
    store.put_chat(
        chat_id,
        title=group.title,
        username=group.username,
        is_channel=group.is_channel,
        members_count=group.members_count,
    )
    return group.title


async def sync_chats(
    app: BaleApp,
    store: MessageStore,
    chat_ids: Sequence[int] | None = None,
    *,
    since: TimeLike | None = None,
    limit: int | None = None,
    limiter: RateLimiter | None = None,
    full: bool = False,
    groups_only: bool = True,
) -> SweepReport:
    """Sync several chats in turn, one request at a time.

    With no `chat_ids`, every group and channel the account is in gets synced,
    and their titles are recorded on the way past so search results can be
    read without looking ids up. Chats are walked sequentially on purpose:
    they share one account's rate limit, so concurrency buys nothing but 429s.
    """
    limiter = limiter or RateLimiter()
    sweep = SweepReport()
    titles: dict[int, str | None] = {}

    if chat_ids is None:
        try:
            groups = await limiter.run(app.groups, what="groups")
        except Exception as exc:
            sweep.chats.append(
                SyncReport(chat_id=0, error=f"{type(exc).__name__}: {exc}")
            )
            return sweep
        for group in groups:
            store.put_chat(
                group.id,
                title=group.title,
                username=group.username,
                is_channel=group.is_channel,
                members_count=group.members_count,
            )
            titles[group.id] = group.title
        chat_ids = [
            group.id for group in groups if not (groups_only and group.is_channel)
        ]
    else:
        titles = store.chat_titles()

    for chat_id in chat_ids:
        sweep.chats.append(
            await sync_chat(
                app,
                store,
                chat_id,
                since=since,
                limit=limit,
                limiter=limiter,
                title=titles.get(chat_id),
                full=full,
            )
        )

    sweep.calls, sweep.rate_limited = limiter.calls, limiter.rate_limited
    return sweep
