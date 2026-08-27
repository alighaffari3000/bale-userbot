"""Pacing, backoff, and which failures are worth another attempt."""

import asyncio

import pytest
from baleclient.exceptions import BaleError

from bale_userbot.limiter import RateLimiter, is_rate_limit, is_retryable


def rate_limited() -> BaleError:
    return BaleError("user_rate_limited", 8)


def refused() -> BaleError:
    return BaleError("group_not_found", 3)


def fast() -> RateLimiter:
    """A limiter with the waiting taken out, so tests stay quick."""
    return RateLimiter(min_interval=0, base_backoff=0, max_backoff=0)


def test_rate_limits_are_recognised_whatever_the_topic():
    assert is_rate_limit(rate_limited())
    assert not is_rate_limit(refused())
    assert not is_rate_limit(ValueError("nope"))


def test_only_transient_failures_are_retryable():
    assert is_retryable(rate_limited())
    assert is_retryable(TimeoutError())
    # A real answer from the service is not worth asking again.
    assert not is_retryable(refused())
    assert not is_retryable(ValueError("nope"))


async def test_a_successful_call_passes_its_value_through():
    async def call():
        return 42

    assert await fast().run(call) == 42


async def test_a_rate_limit_is_retried_until_it_clears():
    attempts = []

    async def call():
        attempts.append(1)
        if len(attempts) < 3:
            raise rate_limited()
        return "ok"

    limiter = fast()
    assert await limiter.run(call) == "ok"
    assert len(attempts) == 3
    assert limiter.rate_limited == 2
    assert limiter.calls == 3


async def test_retries_give_up_and_re_raise():
    async def call():
        raise rate_limited()

    limiter = RateLimiter(min_interval=0, max_retries=2, base_backoff=0, max_backoff=0)
    with pytest.raises(BaleError):
        await limiter.run(call)
    assert limiter.calls == 3  # the first attempt plus two retries


async def test_a_service_refusal_is_not_retried():
    attempts = []

    async def call():
        attempts.append(1)
        raise refused()

    with pytest.raises(BaleError):
        await fast().run(call)
    assert len(attempts) == 1


async def test_calls_are_serialized_not_interleaved():
    running = 0
    peak = 0

    async def call():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return None

    limiter = fast()
    await asyncio.gather(*(limiter.run(call) for _ in range(5)))
    assert peak == 1


async def test_min_interval_spaces_successive_calls():
    starts = []

    async def call():
        starts.append(asyncio.get_running_loop().time())

    limiter = RateLimiter(min_interval=0.05)
    await limiter.run(call)
    await limiter.run(call)
    assert starts[1] - starts[0] >= 0.04


def test_backoff_grows_and_is_capped():
    limiter = RateLimiter(base_backoff=10, max_backoff=40)
    # Jitter halves the delay at worst, so compare against the floor.
    assert limiter.backoff_for(1) >= 5
    assert limiter.backoff_for(3) >= 20
    assert limiter.backoff_for(9) <= 40


def test_nonsense_settings_are_refused():
    with pytest.raises(ValueError):
        RateLimiter(min_interval=-1)
    with pytest.raises(ValueError):
        RateLimiter(max_retries=-1)
