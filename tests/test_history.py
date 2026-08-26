"""Reading an archive between two dates."""

from datetime import UTC, date, datetime, timedelta

import pytest
from baleclient.enums import ChatType, ListLoadMode

from bale_userbot.history import (
    LATEST,
    day_range,
    export_history,
    iter_history,
    load_history,
    to_timestamp,
)
from tests import factories as f

CHAT_ID = 900

#: One day in milliseconds — the unit every Bale timestamp is in.
DAY = 86_400_000


def at(moment: datetime, message_id: int) -> object:
    message = f.message(f.text_content(f"m{message_id}"), message_id=message_id)
    message.date = int(moment.timestamp() * 1000)
    return message


def local(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour).astimezone()


class FakeClient:
    """Answers `load_history` from a fixed archive, the way the server would.

    Messages are returned newest-first starting at `offset_date`, inclusive —
    the awkward case, since an inclusive offset makes a naive pager repeat.
    """

    def __init__(self, messages=(), *, inclusive: bool = True):
        self.messages = sorted(messages, key=lambda m: m.date, reverse=True)
        self.inclusive = inclusive
        self.calls: list[dict] = []

    async def load_history(
        self, chat_id, chat_type, limit=20, offset_date=-1, load_mode=None
    ):
        self.calls.append(
            {"offset_date": offset_date, "limit": limit, "load_mode": load_mode}
        )
        if offset_date == LATEST:
            window = self.messages
        elif self.inclusive:
            window = [m for m in self.messages if m.date <= offset_date]
        else:
            window = [m for m in self.messages if m.date < offset_date]
        return window[:limit]


# --- timestamps ------------------------------------------------------------


def test_a_plain_date_means_the_whole_day():
    start, end = day_range(date(2026, 4, 26), date(2026, 4, 28))
    assert end - start == 3 * DAY - 1
    assert start == to_timestamp(datetime(2026, 4, 26, 0, 0).astimezone())


def test_a_naive_datetime_is_local_time():
    naive = to_timestamp(datetime(2026, 4, 26, 9, 0))
    aware = to_timestamp(datetime(2026, 4, 26, 9, 0).astimezone())
    assert naive == aware


def test_an_aware_datetime_keeps_its_offset():
    utc = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)
    assert to_timestamp(utc) == int(utc.timestamp() * 1000)


def test_iso_strings_are_accepted_both_ways():
    assert to_timestamp("2026-04-26") == to_timestamp(date(2026, 4, 26))
    assert to_timestamp("2026-04-26T09:00") == to_timestamp(datetime(2026, 4, 26, 9, 0))


def test_a_date_only_string_still_stretches_to_end_of_day():
    assert to_timestamp("2026-04-28", end_of_day=True) == to_timestamp(
        date(2026, 4, 28), end_of_day=True
    )


def test_an_int_is_already_a_bale_timestamp():
    assert to_timestamp(1_700_000_000_000) == 1_700_000_000_000


def test_a_datetime_until_is_taken_literally():
    # Only a bare date means "the whole day"; a time was explicitly asked for.
    exact = datetime(2026, 4, 28, 9, 0)
    assert day_range(None, exact)[1] == to_timestamp(exact)


def test_a_backwards_range_is_refused():
    with pytest.raises(ValueError):
        day_range(date(2026, 4, 28), date(2026, 4, 26))


def test_garbage_is_refused():
    with pytest.raises(ValueError):
        to_timestamp("last tuesday")
    with pytest.raises(TypeError):
        to_timestamp(True)


# --- reading ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_date_range_returns_only_those_days():
    archive = [
        at(local(2026, 4, 25), 1),
        at(local(2026, 4, 26), 2),
        at(local(2026, 4, 27), 3),
        at(local(2026, 4, 28, 23), 4),
        at(local(2026, 4, 29), 5),
    ]
    client = FakeClient(archive)
    got = await load_history(
        client,
        CHAT_ID,
        ChatType.GROUP,
        since=date(2026, 4, 26),
        until=date(2026, 4, 28),
    )
    assert [m.message_id for m in got] == [2, 3, 4]


@pytest.mark.asyncio
async def test_the_sweep_starts_at_until_not_at_the_newest_message():
    client = FakeClient([at(local(2026, 4, 26), 1)])
    await load_history(client, CHAT_ID, until=date(2026, 4, 28))
    assert client.calls[0]["offset_date"] == to_timestamp(
        date(2026, 4, 28), end_of_day=True
    )
    assert client.calls[0]["load_mode"] == ListLoadMode.BACKWARD


@pytest.mark.asyncio
async def test_with_no_range_it_starts_at_the_newest_message():
    client = FakeClient([at(local(2026, 4, 26), 1)])
    await load_history(client, CHAT_ID)
    assert client.calls[0]["offset_date"] == LATEST


@pytest.mark.asyncio
async def test_results_are_chronological_by_default():
    archive = [at(local(2026, 4, 25 + n), n) for n in range(1, 4)]
    client = FakeClient(archive)
    assert [m.message_id for m in await load_history(client, CHAT_ID)] == [1, 2, 3]
    newest_first = await load_history(client, CHAT_ID, oldest_first=False)
    assert [m.message_id for m in newest_first] == [3, 2, 1]


@pytest.mark.asyncio
async def test_an_inclusive_offset_does_not_stall_the_walk():
    # The server hands back the message named by offset_date; a pager that only
    # dedupes would stop after the first page. The one-millisecond nudge moves on.
    archive = [at(local(2026, 4, 20 + n), n) for n in range(1, 6)]
    client = FakeClient(archive, inclusive=True)
    got = await load_history(client, CHAT_ID, page_size=1)
    assert [m.message_id for m in got] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_an_exclusive_offset_needs_no_nudge():
    archive = [at(local(2026, 4, 20 + n), n) for n in range(1, 6)]
    client = FakeClient(archive, inclusive=False)
    got = await load_history(client, CHAT_ID, page_size=2)
    assert [m.message_id for m in got] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_messages_sharing_a_millisecond_are_not_split():
    moment = local(2026, 4, 26)
    archive = [at(moment, 1), at(moment, 2), at(moment, 3)]
    got = await load_history(FakeClient(archive), CHAT_ID, page_size=2)
    assert sorted(m.message_id for m in got) == [1, 2, 3]


@pytest.mark.asyncio
async def test_limit_stops_the_walk_and_never_over_asks():
    archive = [at(local(2026, 4, 20) + timedelta(days=n), n) for n in range(1, 10)]
    client = FakeClient(archive)
    got = await load_history(client, CHAT_ID, limit=3)
    assert len(got) == 3
    assert all(call["limit"] <= 3 for call in client.calls)


@pytest.mark.asyncio
async def test_an_empty_archive_is_not_an_error():
    assert await load_history(FakeClient([]), CHAT_ID) == []


@pytest.mark.asyncio
async def test_page_size_must_be_positive():
    with pytest.raises(ValueError):
        [m async for m in iter_history(FakeClient([]), CHAT_ID, page_size=0)]


@pytest.mark.asyncio
async def test_since_alone_stops_at_the_boundary():
    archive = [at(local(2026, 4, 20 + n), n) for n in range(1, 6)]
    got = await load_history(FakeClient(archive), CHAT_ID, since=date(2026, 4, 24))
    assert [m.message_id for m in got] == [4, 5]


# --- records ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_returns_json_serializable_rows():
    import json

    client = FakeClient([at(local(2026, 4, 26), 1)])
    rows = await export_history(client, CHAT_ID, ChatType.GROUP)
    (row,) = json.loads(json.dumps(rows))
    assert row["kind"] == "text"
    assert row["text"] == "m1"
    assert row["chat_type"] == int(ChatType.PRIVATE)


def test_an_explicit_midnight_is_not_stretched_to_a_whole_day():
    # "2026-04-26" asks for the day; "2026-04-26T00:00" asks for that instant.
    # fromisoformat returns the same datetime for both, so the string has to
    # be inspected before it is parsed.
    assert to_timestamp("2026-04-26T00:00", end_of_day=True) == to_timestamp(
        datetime(2026, 4, 26, 0, 0)
    )
    assert to_timestamp("2026-04-26", end_of_day=True) > to_timestamp(
        "2026-04-26T00:00", end_of_day=True
    )


@pytest.mark.asyncio
async def test_rich_message_kinds_survive_the_record_round_trip():
    # asdict() has to reach MediaInfo, StickerInfo and the rest; a dataclass
    # that slipped in as a non-serializable object would only show up here.
    import json

    archive = []
    for index, content in enumerate(
        [f.photo(caption="cap"), f.voice(), f.sticker_content(), f.gift_content()], 1
    ):
        message = f.message(content, message_id=index)
        message.date = int(local(2026, 4, 26, 10 + index).timestamp() * 1000)
        archive.append(message)

    rows = await export_history(FakeClient(archive), CHAT_ID, ChatType.GROUP)
    parsed = json.loads(json.dumps(rows, ensure_ascii=False))
    assert [r["kind"] for r in parsed] == ["photo", "voice", "sticker", "gift"]
    assert parsed[0]["media"]["mime_type"] == "image/jpeg"
    assert parsed[2]["sticker"]["image512"]["file_id"]
