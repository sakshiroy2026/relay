"""Retries with full-jitter backoff: try again on transient failures, never on permanent ones.

WHAT IT IS  The "try again politely" rule. A flaky call (a 503, a timeout) is
            retried up to `attempts` times, sleeping a random, growing delay
            between tries so three workers don't hammer a struggling service
            in lockstep. A permanent failure is never retried.
GOES IN     call_with_retries: a zero-argument function to run, how many
            attempts it gets, and (for tests) injectable sleep and rng.
            Optionally on_failure, called once per failed try (the circuit
            breaker counts failures there).
COMES OUT   (the function's result, how many attempts it took), or the last
            RetryableError re-raised when every attempt failed.
TOUCHES     Nothing: no database, no config. Pure, so it's testable with a
            fake clock.
FAILS WHEN  FatalError or any other exception -> propagates immediately,
            unretried. RetryableError on the final attempt -> re-raised.
            Worst-case total sleep with the defaults is 1.0 + 2.0 = 3.0 s,
            well inside the 15 s test lease (90 s default), so no heartbeat
            is needed mid-retry.
"""

import random
import time
from collections.abc import Callable


class RetryableError(Exception):
    """Transient: the same call may well succeed in a moment (503, timeout)."""


class FatalError(Exception):
    """Permanent: the same call will fail the same way again (400 bad request)."""


def backoff_delay(
    failures_so_far: int,
    base: float = 0.5,
    cap: float = 4.0,
    rng: Callable[[], float] = random.random,
) -> float:
    """Full jitter: a random sleep in [0, min(cap, base * 2^failures_so_far)).

    After 1 failure the ceiling is 1.0 s, after 2 it is 2.0 s, and `cap`
    stops it growing forever. The randomness is the point: three workers
    retrying a struggling service must not all come back at the same instant.
    """
    return rng() * min(cap, base * (2**failures_so_far))


def call_with_retries[T](
    fn: Callable[[], T],
    attempts: int = 3,
    base: float = 0.5,
    cap: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    on_failure: Callable[[RetryableError], None] | None = None,
) -> tuple[T, int]:
    """Run fn, retrying only RetryableError. Returns (result, attempts used)."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        try:
            return fn(), attempt
        except RetryableError as exc:
            if on_failure is not None:
                on_failure(exc)
            if attempt == attempts:
                raise  # out of attempts: the caller decides what the failure means
            sleep(backoff_delay(attempt, base, cap, rng))

    raise AssertionError("unreachable: the loop returns or raises")
