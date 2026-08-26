"""Per-chat ordering — the guarantee `serialize_per_chat` promises."""

import asyncio

from bale_userbot.routing import ChatSerializer, wrap_handler
from tests import factories as f


def make_recorder(order: list[str], delay: float = 0.02):
    async def handler(message, client):
        order.append(f"start{message.message_id}")
        await asyncio.sleep(delay)
        order.append(f"end{message.message_id}")

    return handler


async def test_three_messages_in_one_chat_stay_ordered():
    # Two messages cannot catch this: the lock is only dropped with a waiter
    # queued when a *third* arrives while the second is still waiting.
    order: list[str] = []
    serializer = ChatSerializer()
    wrapped = wrap_handler(make_recorder(order), serializer=serializer)

    tasks = []
    for message_id in (1, 2, 3):
        message = f.message(f.text_content(), message_id=message_id)
        tasks.append(asyncio.create_task(wrapped(message, None)))
        await asyncio.sleep(0.005)  # each arrives while the previous runs

    await asyncio.gather(*tasks)

    assert order == [
        "start1", "end1",
        "start2", "end2",
        "start3", "end3",
    ]


async def test_locks_are_released_once_a_chat_drains():
    serializer = ChatSerializer()
    wrapped = wrap_handler(make_recorder([]), serializer=serializer)

    await asyncio.gather(
        *(
            wrapped(f.message(f.text_content(), message_id=i), None)
            for i in range(1, 6)
        )
    )
    assert serializer.tracked_chats == 0


async def test_different_chats_do_not_block_each_other():
    order: list[str] = []
    serializer = ChatSerializer()

    async def handler(message, client):
        order.append(f"start{message.chat.id}")
        await asyncio.sleep(0.02)
        order.append(f"end{message.chat.id}")

    wrapped = wrap_handler(handler, serializer=serializer)
    await asyncio.gather(
        wrapped(f.message(f.text_content(), chat_id=1, message_id=1), None),
        wrapped(f.message(f.text_content(), chat_id=2, message_id=2), None),
    )
    # Interleaved, not serialized: both start before either ends.
    assert order[:2] == ["start1", "start2"]


async def test_a_failing_handler_still_releases_its_lock():
    serializer = ChatSerializer()

    async def boom(message, client):
        raise RuntimeError("handler exploded")

    wrapped = wrap_handler(boom, serializer=serializer)
    await wrapped(f.message(f.text_content()), None)  # logged, not raised
    assert serializer.tracked_chats == 0
