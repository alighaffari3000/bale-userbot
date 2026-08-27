"""Pacing and retries for a personal account's request budget.

Bale rate-limits per *account*, not per connection, and it says so late: a
burst of `LoadHistory` calls succeeds until the service answers
`user_rate_limited` on topic 8, after which every call fails for a while. A
script written for one question survives that; an agent that decides to sweep
twenty groups does not, and it takes the account's other clients down with it.

So everything the sync layer sends goes through one `RateLimiter`: a single
in-flight request at a time, a floor on the gap between them, and exponential
backoff when the service pushes back. It is deliberately *not* installed on
the client — interactive `send()` calls should stay immediate — but it is
shared by every sweep, which is where the volume comes from.

Transport faults are retried on the same ladder. On a personal account the
websocket drops routinely (`ServerDisconnectedError`, then `Connector is
closed` on the retry): waiting and asking again is the whole remedy.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from baleclient.exceptions import BaleError

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: What the service calls the "you are going too fast" error.
RATE_LIMIT_MARKERS = ("user_rate_limited", "too_many_requests", "flood")
#: Transport faults worth another attempt. Matched on class name so a
#: BaleClient version bump cannot break the import.
RETRYABLE_ERRORS = (
    "TimeoutError",
    "ServerDisconnectedError",
    "ClientConnectorError",
    "ClientConnectionError",
    "ClientOSError",
    "ConnectionResetError",
)


def is_rate_limit(exc: BaseException) -> bool:
    """True when the service is telling us to slow down."""
    if isinstance(exc, BaleError):
        message = (exc.message or "").lower()
        return any(marker in message for marker in RATE_LIMIT_MARKERS)
    return False


def is_retryable(exc: BaseException) -> bool:
    """True for faults that another attempt could plausibly get past."""
    if is_rate_limit(exc):
        return True
    if isinstance(exc, BaleError):
        # Any other service-level error is a real answer: retrying a
        # "no such group" a dozen times only wastes the budget.
        return False
    return type(exc).__name__ in RETRYABLE_ERRORS


class RateLimiter:
    """One account's outbound budget: paced, serialized, and retried.

    `min_interval` is measured between the *starts* of successive calls, so a
    slow request does not earn a second one for free.
    """

    def __init__(
        self,
        *,
        min_interval: float = 0.5,
        max_retries: int = 4,
        base_backoff: float = 5.0,
        max_backoff: float = 120.0,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval cannot be negative")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._lock = asyncio.Lock()
        self._last_start = 0.0
        #: Set when the service pushes back, so callers that were queued
        #: behind the offender wait it out too instead of piling on.
        self._cooldown_until = 0.0
        #: Requests that reached the service, retries included.
        self.calls = 0
        #: How many of those came back as "slow down".
        self.rate_limited = 0

    def backoff_for(self, attempt: int) -> float:
        """Seconds to wait before attempt number `attempt` (1 = first retry).

        Jittered, because a fleet of agents that all back off by exactly the
        same amount simply collides again at the end of it.
        """
        delay = min(self.base_backoff * (2 ** (attempt - 1)), self.max_backoff)
        return delay * (0.5 + random.random() / 2)

    async def _pace(self) -> None:
        now = time.monotonic()
        since_last = now - self._last_start
        gap = max(self.min_interval - since_last, self._cooldown_until - now)
        if gap > 0:
            await asyncio.sleep(gap)
        self._last_start = time.monotonic()

    async def run(
        self, call: Callable[[], Awaitable[T]], *, what: str = "request"
    ) -> T:
        """Await `call()`, paced and retried. Re-raises the last failure.

        `call` is a factory, not a coroutine: a coroutine object cannot be
        awaited twice, and a retry has to be a fresh one.
        """
        attempt = 0
        while True:
            async with self._lock:
                await self._pace()
                self.calls += 1
                try:
                    return await call()
                except Exception as exc:
                    limited = is_rate_limit(exc)
                    self.rate_limited += int(limited)
                    if attempt >= self.max_retries or not is_retryable(exc):
                        raise
                    attempt += 1
                    delay = self.backoff_for(attempt)
                    if limited:
                        self._cooldown_until = time.monotonic() + delay
                    logger.warning(
                        "%s failed (%s%s); retry %s/%s in %.1fs",
                        what,
                        type(exc).__name__,
                        ": rate limited" if limited else "",
                        attempt,
                        self.max_retries,
                        delay,
                    )
            # Slept outside the lock so a queued caller is not woken early
            # into the same wall — but it will re-pace before it sends.
            await asyncio.sleep(delay)


def limiter_stats(limiter: RateLimiter | None) -> dict[str, Any]:
    """A small dict for a report or a log line."""
    if limiter is None:
        return {}
    return {"calls": limiter.calls, "rate_limited": limiter.rate_limited}
