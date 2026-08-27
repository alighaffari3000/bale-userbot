"""Filling the store: incremental walks, and surviving a chat that fails."""

import pytest

from bale_userbot.history import to_timestamp
from bale_userbot.limiter import RateLimiter
from bale_userbot.store import MessageStore
from bale_userbot.sync import OVERLAP_MS, sync_chat, sync_chats
from tests import factories as f

CHAT = 900
OTHER = 901
DAY = 86_400_000
#: These fixtures are dated in 1970, so a sync has to be told to reach
#: back that far: the default window is "the last thirty days".
EPOCH = 0


def at(day: int, message_id: int, text: str = "پنل", chat_id: int = CHAT):
    message = f.message(f.text_content(text), chat_id=chat_id, message_id=message_id)
    message.date = day * DAY
    return message


class FakeGroup:
    def __init__(self, chat_id, title, is_channel=False):
        self.id = chat_id
        self.title = title
        self.username = None
        self.is_channel = is_channel
        self.members_count = 10


class FakeApp:
    """Stands in for `BaleApp`: an archive per chat, and a call log."""

    def __init__(self, archives, *, groups=(), fails=()):
        self.archives = archives
        self._groups = list(groups)
        self.fails = dict(fails)
        self.client = self
        self.history_calls = []

    async def groups(self):
        return self._groups

    async def load_history(
        self, chat_id, chat_type, limit=None, offset_date=-1, load_mode=None
    ):
        raise AssertionError("sync must go through history.load_history")


@pytest.fixture
def store():
    with MessageStore(":memory:") as opened:
        yield opened


@pytest.fixture
def patched(monkeypatch):
    """Replace `load_history` so the walk itself is not what is under test."""
    seen = []

    def install(archives, fails=()):
        failures = dict(fails)

        async def fake_load_history(client, chat_id, chat_type, **kwargs):
            seen.append({"chat_id": chat_id, **kwargs})
            if chat_id in failures:
                raise failures[chat_id]
            since = to_timestamp(kwargs.get("since"))
            messages = archives.get(chat_id, [])
            if since is not None:
                messages = [m for m in messages if m.date >= since]
            return messages

        monkeypatch.setattr("bale_userbot.sync.load_history", fake_load_history)
        return seen

    return install


def limiter():
    return RateLimiter(min_interval=0, base_backoff=0, max_backoff=0)


async def test_a_first_sync_stores_what_it_read(store, patched):
    patched({CHAT: [at(10, 1), at(11, 2)]})

    report = await sync_chat(
        FakeApp({}), store, CHAT, limiter=limiter(), title="گروه", since=EPOCH
    )

    assert (report.ok, report.fetched, report.stored) == (True, 2, 2)
    assert report.incremental is False
    assert store.newest_date(CHAT) == 11 * DAY
    assert len(store.search("پنل")) == 2
    assert store.chat_titles()[CHAT] == "گروه"


async def test_a_second_sync_starts_from_what_is_already_stored(store, patched):
    calls = patched({CHAT: [at(10, 1), at(11, 2)]})
    await sync_chat(FakeApp({}), store, CHAT, limiter=limiter(), since=EPOCH)

    report = await sync_chat(FakeApp({}), store, CHAT, limiter=limiter())

    assert report.incremental is True
    # The second walk starts at the newest stored message, less the overlap.
    assert calls[1]["since"] == 11 * DAY - OVERLAP_MS
    assert calls[0]["since"] != calls[1]["since"]


async def test_full_re_reads_the_window_instead(store, patched):
    calls = patched({CHAT: [at(10, 1)]})
    await sync_chat(FakeApp({}), store, CHAT, limiter=limiter(), since="2020-01-01")

    report = await sync_chat(
        FakeApp({}), store, CHAT, limiter=limiter(), since="2020-01-01", full=True
    )

    assert report.incremental is False
    assert calls[1]["since"] == to_timestamp("2020-01-01")


async def test_a_failing_chat_is_reported_not_raised(store, patched):
    patched({CHAT: [at(10, 1)]}, fails={CHAT: RuntimeError("bad page")})

    report = await sync_chat(FakeApp({}), store, CHAT, limiter=limiter(), since=EPOCH)

    assert report.ok is False
    assert "RuntimeError: bad page" in report.error
    assert report.stored == 0


async def test_a_sweep_carries_on_past_one_broken_chat(store, patched):
    patched(
        {CHAT: [at(10, 1)], OTHER: [at(10, 2, chat_id=OTHER)]},
        fails={CHAT: RuntimeError("bad page")},
    )
    app = FakeApp({}, groups=[FakeGroup(CHAT, "A"), FakeGroup(OTHER, "B")])

    sweep = await sync_chats(app, store, limiter=limiter(), since=EPOCH)

    assert [r.ok for r in sweep.chats] == [False, True]
    assert len(sweep.failed) == 1
    assert sweep.stored == 1
    # The good chat's messages are searchable even though its neighbour died.
    assert len(store.search("پنل")) == 1


async def test_a_sweep_records_every_chat_it_saw(store, patched):
    patched({CHAT: [], OTHER: []})
    app = FakeApp({}, groups=[FakeGroup(CHAT, "A"), FakeGroup(OTHER, "B", True)])

    await sync_chats(app, store, limiter=limiter())

    # Titles for both, but a channel is not swept unless asked for.
    assert store.chat_titles() == {CHAT: "A", OTHER: "B"}


async def test_channels_can_be_swept_too(store, patched):
    calls = patched({OTHER: [at(10, 2, chat_id=OTHER)]})
    app = FakeApp({}, groups=[FakeGroup(OTHER, "B", is_channel=True)])

    await sync_chats(app, store, limiter=limiter(), groups_only=False, since=EPOCH)

    assert [call["chat_id"] for call in calls] == [OTHER]


async def test_a_sweep_that_cannot_list_groups_says_so(store, patched):
    patched({})

    class Broken(FakeApp):
        async def groups(self):
            raise RuntimeError("no dialogs")

    sweep = await sync_chats(Broken({}), store, limiter=limiter())

    assert len(sweep.failed) == 1
    assert "no dialogs" in sweep.failed[0].error


async def test_forwarded_adverts_land_searchable(store, patched):
    forward = f.forwarded(f.text_content("پنل ۵۵۰ وات موجود"), chat_id=CHAT)
    forward.date = 10 * DAY
    patched({CHAT: [forward]})

    await sync_chat(FakeApp({}), store, CHAT, limiter=limiter(), since=EPOCH)

    assert len(store.search("پنل 550")) == 1


async def test_a_chat_synced_by_id_still_learns_its_name(store, patched):
    patched({CHAT: [at(10, 1)]})

    class Named(FakeApp):
        async def group(self, chat_id):
            return FakeGroup(chat_id, "سولار تهران")

    report = await sync_chat(Named({}), store, CHAT, limiter=limiter(), since=EPOCH)

    assert report.title == "سولار تهران"
    assert store.chat_titles()[CHAT] == "سولار تهران"


async def test_a_missing_name_does_not_fail_the_sync(store, patched):
    patched({CHAT: [at(10, 1)]})

    class Nameless(FakeApp):
        async def group(self, chat_id):
            raise RuntimeError("no access")

    report = await sync_chat(Nameless({}), store, CHAT, limiter=limiter(), since=EPOCH)

    assert (report.ok, report.stored, report.title) == (True, 1, None)
