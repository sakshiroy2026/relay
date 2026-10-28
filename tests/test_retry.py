"""Unit tests for call_with_retries. Sleep and randomness are injected: no real waiting."""

import pytest

from relay.core.retry import FatalError, RetryableError, backoff_delay, call_with_retries


class Flaky:
    """Fails `failures` times with RetryableError, then returns 'done'."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise RetryableError(f"injected failure #{self.calls}")
        return "done"


def test_success_first_try_sleeps_never() -> None:
    sleeps: list[float] = []
    result, attempts = call_with_retries(Flaky(0), sleep=sleeps.append)
    assert (result, attempts) == ("done", 1)
    assert sleeps == []


def test_two_failures_then_success_uses_three_attempts() -> None:
    sleeps: list[float] = []
    # rng pinned to 1.0: each sleep is exactly its ceiling, min(cap, 0.5 * 2^failures)
    result, attempts = call_with_retries(Flaky(2), sleep=sleeps.append, rng=lambda: 1.0)
    assert (result, attempts) == ("done", 3)
    assert sleeps == [1.0, 2.0]  # ceilings after failure 1 and failure 2


def test_gives_up_after_the_last_attempt() -> None:
    sleeps: list[float] = []
    flaky = Flaky(99)
    with pytest.raises(RetryableError, match="failure #3"):
        call_with_retries(flaky, attempts=3, sleep=sleeps.append, rng=lambda: 0.0)
    assert flaky.calls == 3  # no fourth try
    assert len(sleeps) == 2  # no sleep after the final failure


def test_fatal_error_is_never_retried() -> None:
    calls = []

    def fatal() -> None:
        calls.append(1)
        raise FatalError("400 bad request")

    with pytest.raises(FatalError):
        call_with_retries(fatal, sleep=lambda _s: pytest.fail("slept on a fatal error"))
    assert len(calls) == 1


def test_unknown_exceptions_propagate_unretried() -> None:
    def boom() -> None:
        raise ValueError("a code bug, not a transient failure")

    with pytest.raises(ValueError):
        call_with_retries(boom, sleep=lambda _s: pytest.fail("slept on a bug"))


def test_on_failure_is_called_once_per_failed_try() -> None:
    seen: list[str] = []
    with pytest.raises(RetryableError):
        call_with_retries(
            Flaky(99),
            attempts=3,
            sleep=lambda _s: None,
            on_failure=lambda exc: seen.append(str(exc)),
        )
    assert seen == ["injected failure #1", "injected failure #2", "injected failure #3"]


def test_backoff_delay_is_capped_and_jittered() -> None:
    # ceiling grows 1.0 -> 2.0 -> 4.0 and then the cap holds it at 4.0
    assert [backoff_delay(n, rng=lambda: 1.0) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 4.0]
    # rng scales the ceiling: half the ceiling at rng = 0.5
    assert backoff_delay(1, rng=lambda: 0.5) == 0.5
    # and real jitter stays inside [0, ceiling)
    assert all(0.0 <= backoff_delay(1) < 1.0 for _ in range(50))


def test_zero_attempts_is_rejected() -> None:
    with pytest.raises(ValueError):
        call_with_retries(lambda: "x", attempts=0)
