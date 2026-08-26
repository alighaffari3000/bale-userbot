"""Reading a chat's archive, optionally between two dates.

`Client.load_history` returns one page and leaves the rest to you: there is no
cursor, no direction handling, and no way to say "messages from the 26th to the
28th". This module walks the pages backwards from `until`, stops at `since`,
and hands back `Message` objects — or, through `export_history`, the same flat
JSON-ready rows `bale_userbot.groups` produces for members.

Two page-walking hazards are handled here, and both look identical from the
outside — a page containing nothing we had not already seen. The offset is a
timestamp, not a message id, so either the server counted `offset_date`
inclusively (fixed by nudging the offset one millisecond earlier), or more
messages share that millisecond than the page had room for (fixed by asking for
a bigger page, since a smaller one can never get past the cluster). The pager
tries growing first and nudging second; without the first, a burst of messages
sent in the same millisecond would be silently truncated, and without the
second, an inclusive server would stop the walk after a single page.

Every Bale timestamp is milliseconds since the epoch. `to_timestamp()` accepts
whatever is convenient — a `datetime`, a `date`, an ISO string — and a bare
`date` means the whole day in *local* time, which is what someone asking for
"the 26th" means.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Any

from baleclient import Client
from baleclient.enums import ChatType, ListLoadMode
from baleclient.types import Message

from .content import MessageInfo, describe

logger = logging.getLogger(__name__)

#: Messages asked for per `LoadHistory` call.
DEFAULT_PAGE_SIZE = 100
#: Backstop against a chat that never stops handing out pages.
MAX_PAGES = 10_000
#: How far a page may grow to swallow a cluster of same-millisecond messages.
MAX_PAGE_SIZE = 1_000
#: What `LoadHistory` means by "start at the newest message".
LATEST = -1

TimeLike = datetime | date | int | float | str


# --- timestamps -------------------------------------------------------------


def to_timestamp(value: TimeLike | None, *, end_of_day: bool = False) -> int | None:
    """Milliseconds since the epoch, from whatever the caller had at hand.

    A `date` (or a date-only ISO string) covers a whole local day: the start of
    it, or with `end_of_day` the last millisecond before the next one. A naive
    `datetime` is read as local time — someone typing "2026-04-26 09:00" means
    nine in the morning where they are, not in UTC. An `int` is already a Bale
    timestamp and passes through untouched.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("a bool is not a timestamp")
    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"cannot read {value!r} as a date or datetime") from exc
        # fromisoformat keeps a date-only string as midnight; treat it as a day.
        if end_of_day and value.time() == time.min:
            value = value.date()

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        day = value + timedelta(days=1) if end_of_day else value
        moment = datetime.combine(day, time.min)
    else:
        raise TypeError(f"unsupported timestamp: {type(value).__name__}")

    if moment.tzinfo is None:
        moment = moment.astimezone()
    milliseconds = int(moment.timestamp() * 1000)
    return (
        milliseconds - 1
        if end_of_day and not isinstance(value, datetime)
        else milliseconds
    )


def day_range(
    since: TimeLike | None, until: TimeLike | None
) -> tuple[int | None, int | None]:
    """A `(since, until)` pair of Bale timestamps, both ends inclusive.

    `until` is stretched to the end of its day when it is a plain date, so
    `day_range(date(2026, 4, 26), date(2026, 4, 28))` covers all three days
    rather than stopping at midnight on the 28th.
    """
    start = to_timestamp(since)
    end = to_timestamp(until, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise ValueError("since is after until")
    return start, end


# --- reading ----------------------------------------------------------------


async def _page_with_something_new(
    client: Client,
    chat_id: int,
    chat_type: ChatType,
    *,
    offset: int,
    want: int,
    seen: set[int],
) -> tuple[list[Message], list[Message]]:
    """Fetch from `offset` until the page contains a message we have not seen.

    A page of nothing but repeats means one of two things. Either more messages
    share `offset`'s millisecond than the page had room for — so a bigger page
    is asked for, which is the only way to keep such a cluster whole — or the
    server simply counted `offset_date` inclusively and there is nothing more
    at that instant, which the caller resolves by nudging the offset back.

    Returns `(fresh, whole_page)`; an empty `fresh` means "grow no further".
    """
    size = want
    while True:
        messages = await client.load_history(
            chat_id,
            chat_type,
            limit=size,
            offset_date=offset,
            load_mode=ListLoadMode.BACKWARD,
        )
        fresh = [m for m in messages if m.message_id not in seen]
        if fresh or not messages:
            return fresh, messages
        if len(messages) < size or size >= MAX_PAGE_SIZE:
            return [], messages
        size = min(size * 2, MAX_PAGE_SIZE)


async def iter_history(
    client: Client,
    chat_id: int,
    chat_type: ChatType = ChatType.PRIVATE,
    *,
    since: TimeLike | None = None,
    until: TimeLike | None = None,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AsyncIterator[Message]:
    """Yield messages newest-first, so a long archive never has to fit in memory.

    `since`/`until` bound the walk on both ends; the sweep starts at `until`
    and stops as soon as a message older than `since` shows up, because
    `LoadHistory` returns messages in date order.
    """
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    start, end = day_range(since, until)
    offset = end if end is not None else LATEST
    #: Every message id already handled — yielded or skipped alike. Skipped
    #: ones have to count too, or a page of them would look fresh forever.
    seen: set[int] = set()
    yielded = 0
    nudged = False

    for page in range(MAX_PAGES):
        want = page_size if limit is None else min(page_size, limit - yielded)
        if want < 1:
            return

        fresh, messages = await _page_with_something_new(
            client, chat_id, chat_type, offset=offset, want=want, seen=seen
        )
        if not messages:
            return

        if not fresh:
            if nudged:
                logger.debug(
                    "chat %s: page %s added nothing new, stopping", chat_id, page
                )
                return
            # The server may count `offset_date` as part of the page it names.
            # One millisecond earlier asks for the same window, exclusively.
            nudged = True
            offset = min(m.date for m in messages) - 1
            continue

        nudged = False
        for message in sorted(fresh, key=lambda m: m.date, reverse=True):
            seen.add(message.message_id)
            if end is not None and message.date > end:
                continue
            if start is not None and message.date < start:
                return
            yield message
            yielded += 1
            if limit is not None and yielded >= limit:
                return

        offset = min(m.date for m in messages)
    else:
        logger.warning("chat %s: stopped after %s history pages", chat_id, MAX_PAGES)


async def load_history(
    client: Client,
    chat_id: int,
    chat_type: ChatType = ChatType.PRIVATE,
    *,
    since: TimeLike | None = None,
    until: TimeLike | None = None,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    oldest_first: bool = True,
) -> list[Message]:
    """A chat's messages, by default in the order they were sent.

    Paging runs backwards because that is the only direction `LoadHistory`
    supports from a date; an archive reads better forwards, so the result is
    reversed unless `oldest_first=False`.
    """
    messages = [
        message
        async for message in iter_history(
            client,
            chat_id,
            chat_type,
            since=since,
            until=until,
            limit=limit,
            page_size=page_size,
        )
    ]
    return messages[::-1] if oldest_first else messages


# --- records for the application to store -----------------------------------


def message_record(info: MessageInfo) -> dict[str, Any]:
    """One message as a JSON-serializable dict."""
    record = asdict(info)
    record["kind"] = str(info.kind)
    record["chat_type"] = int(info.chat_type)
    return record


def message_records(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """Describe and flatten messages, ready for a file or a table."""
    return [message_record(describe(message)) for message in messages]


async def export_history(
    client: Client,
    chat_id: int,
    chat_type: ChatType = ChatType.PRIVATE,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """A chat's archive as storable rows. Returns data, never a file."""
    return message_records(await load_history(client, chat_id, chat_type, **kwargs))
