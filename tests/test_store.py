"""The searchable cache: normalization, filtering, and honest counts."""

import pytest

from bale_userbot import describe
from bale_userbot.store import MessageStore, normalize
from tests import factories as f

CHAT = 900
OTHER = 901


@pytest.fixture
def store():
    with MessageStore(":memory:") as opened:
        opened.put_chat(CHAT, title="سولار تهران")
        opened.put_chat(OTHER, title="PVsolar")
        yield opened


def put(store, text, *, chat_id=CHAT, message_id=1, sender_id=f.PEER_ID, when=None):
    message = f.message(
        f.text_content(text),
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
    )
    if when is not None:
        message.date = when
    store.put_messages([describe(message)])
    return message


@pytest.mark.parametrize(
    ("written", "typed"),
    [
        ("پنل ۴۵۰ وات", "پنل 450 وات"),  # Persian digits vs ASCII
        ("پنل 450 وات", "پنل ۴۵۰ وات"),  # and the other way round
        ("کیلو‌وات", "کیلووات"),  # zero-width non-joiner
        ("ياسوج", "یاسوج"),  # Arabic yeh vs Persian yeh
        ("كرمان", "کرمان"),  # Arabic kaf vs Persian keheh
        ("Jinko 620", "jinko 620"),  # Latin case
    ],
)
def test_a_search_finds_the_other_spelling(store, written, typed):
    put(store, written)
    assert len(store.search(typed)) == 1


def test_terms_are_anded_not_ored(store):
    put(store, "پنل خورشیدی موجود است", message_id=1)
    put(store, "باتری لیتیوم موجود است", message_id=2)

    assert len(store.search("پنل موجود")) == 1
    assert len(store.search("موجود")) == 2


def test_forwarded_text_is_searchable(store):
    # The whole reason the cache stores `searchable_text` and not `text`.
    forward = f.forwarded(f.text_content("پنل ۵۵۰ وات"), chat_id=CHAT)
    store.put_messages([describe(forward)])

    hits = store.search("پنل 550")
    assert len(hits) == 1
    assert hits.hits[0].is_forward
    assert hits.hits[0].origin_chat_id == f.ORIGIN_ID


def test_a_reply_does_not_inherit_its_parents_words(store):
    quoted = f.replying(f.text_content("ممنون"), f.text_content("پنل ۴۵۰ وات"))
    quoted.chat.id = CHAT
    store.put_messages([describe(quoted)])

    assert len(store.search("پنل 450")) == 0
    assert len(store.search("ممنون")) == 1


def test_regex_answers_what_keywords_cannot(store):
    put(store, "پنل ۴۵۰ وات", message_id=1)
    put(store, "پنل ۷۱۵ وات", message_id=2)
    put(store, "پنل ۵۰۰ وات", message_id=3)

    hits = store.search(r"پنل\s*(4\d\d|500)", regex=True)
    assert {hit.message_id for hit in hits} == {1, 3}


def test_a_broken_pattern_matches_nothing_rather_than_raising(store):
    put(store, "پنل")
    assert len(store.search("پنل (", regex=True)) == 0


def test_wildcards_in_a_query_are_literal(store):
    put(store, "قیمت 100% نقدی", message_id=1)
    put(store, "قیمت نقدی", message_id=2)

    assert {hit.message_id for hit in store.search("100%")} == {1}


def test_filters_narrow_by_chat_sender_and_date(store):
    put(store, "پنل", chat_id=CHAT, message_id=1, sender_id=11, when=1_000_000)
    put(store, "پنل", chat_id=OTHER, message_id=2, sender_id=22, when=2_000_000)

    assert {h.chat_id for h in store.search("پنل", chat_ids=[OTHER])} == {OTHER}
    assert {h.sender_id for h in store.search("پنل", sender_id=11)} == {11}
    assert len(store.search("پنل", since=1_500_000)) == 1


def test_total_is_reported_even_when_hits_are_capped(store):
    for message_id in range(10):
        put(store, "پنل خورشیدی", message_id=message_id)

    result = store.search("پنل", limit=3)
    assert (len(result), result.total, result.truncated) == (3, 10, True)


def test_an_empty_query_is_refused(store):
    # "" would match the whole archive, which is never what was meant.
    with pytest.raises(ValueError):
        store.search("   ")


def test_resyncing_a_message_updates_it_in_place(store):
    put(store, "پنل ۴۵۰", message_id=7)
    put(store, "پنل ۵۵۰ ویرایش شد", message_id=7)

    assert len(store.search("450")) == 0
    assert len(store.search("550")) == 1


def test_sync_state_records_the_window_held(store):
    put(store, "اول", message_id=1, when=1_000)
    put(store, "دوم", message_id=2, when=9_000)

    state = store.record_sync(CHAT)
    assert (state.oldest_date, state.newest_date, state.messages) == (1_000, 9_000, 2)
    assert store.newest_date(CHAT) == 9_000
    assert [s.chat_id for s in store.states()] == [CHAT]


def test_snippet_trims_a_long_message_around_the_hit(store):
    body = "مقدمه " * 60 + "پنل ۴۵۰ وات" + " ادامه" * 60
    put(store, body)

    snippet = store.search("پنل 450").hits[0].snippet
    assert "پنل ۴۵۰ وات" in snippet
    assert len(snippet) < len(body)


def test_messages_without_text_are_never_matched(store):
    store.put_messages([describe(f.message(f.photo(), chat_id=CHAT, message_id=3))])
    assert len(store.search("عکس")) == 0


def test_normalize_is_idempotent():
    once = normalize("پنل ۴۵۰ کیلو‌وات")
    assert normalize(once) == once
