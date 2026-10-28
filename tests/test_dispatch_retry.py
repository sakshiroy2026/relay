"""dispatch_tool with retries and the breaker, tested without a database.

The breaker module is replaced by a recording fake, and retry sleeps are
silenced, so these tests check the WIRING: who gets retried, what the
breaker is told, and what the model ends up reading.
"""

from typing import Any

import pytest
from pydantic import BaseModel

import relay.agent.tools as tools_mod
import relay.core.retry as retry_mod
from relay.agent.tools import Tool, ToolContext, ToolError, dispatch_tool
from relay.core.retry import RetryableError


class FakeBreaker:
    """Stands in for relay.core.breaker; records every call."""

    def __init__(self, allow: bool = True) -> None:
        self.allow_result = allow
        self.failures: list[str] = []
        self.successes: list[str] = []

    def allow(self, tool: str) -> bool:
        return self.allow_result

    def record_failure(self, tool: str) -> None:
        self.failures.append(tool)

    def record_success(self, tool: str) -> None:
        self.successes.append(tool)


class NoInput(BaseModel):
    pass


@pytest.fixture
def breaker(monkeypatch: pytest.MonkeyPatch) -> FakeBreaker:
    fake = FakeBreaker()
    monkeypatch.setattr(tools_mod, "breaker", fake)
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)  # no real backoff waits
    return fake


def _register(monkeypatch: pytest.MonkeyPatch, name: str, run: Any) -> None:
    monkeypatch.setitem(
        tools_mod.REGISTRY, name, Tool(name=name, description="test", input_model=NoInput, run=run)
    )


def test_flaky_tool_succeeds_on_the_third_try(
    monkeypatch: pytest.MonkeyPatch, breaker: FakeBreaker
) -> None:
    calls = []

    def run(_args: NoInput, _ctx: ToolContext | None) -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RetryableError("503")
        return "finally"

    _register(monkeypatch, "flaky", run)
    result = dispatch_tool("flaky", {})
    assert (result.ok, result.content, result.attempts) == (True, "finally", 3)
    assert breaker.failures == ["flaky", "flaky"]  # both failed tries were counted
    assert breaker.successes == ["flaky"]


def test_tool_that_never_recovers_reports_exhaustion(
    monkeypatch: pytest.MonkeyPatch, breaker: FakeBreaker
) -> None:
    def run(_args: NoInput, _ctx: ToolContext | None) -> str:
        raise RetryableError("503 every time")

    _register(monkeypatch, "down", run)
    result = dispatch_tool("down", {})
    assert result.ok is False
    assert "failed after 3 attempts" in result.content
    assert result.attempts == 3
    assert breaker.failures == ["down"] * 3
    assert breaker.successes == []


def test_open_breaker_short_circuits_without_running(
    monkeypatch: pytest.MonkeyPatch, breaker: FakeBreaker
) -> None:
    breaker.allow_result = False

    def run(_args: NoInput, _ctx: ToolContext | None) -> str:
        pytest.fail("the tool ran although its circuit was open")

    _register(monkeypatch, "gated", run)
    result = dispatch_tool("gated", {})
    assert result.ok is False
    assert result.breaker_open is True
    assert "circuit open" in result.content
    assert "flag_for_review" in result.content  # the model is told how to adapt


def test_tool_error_is_not_retried_and_not_counted(
    monkeypatch: pytest.MonkeyPatch, breaker: FakeBreaker
) -> None:
    calls = []

    def run(_args: NoInput, _ctx: ToolContext | None) -> str:
        calls.append(1)
        raise ToolError("404 not found")  # the model's mistake, not an outage

    _register(monkeypatch, "notfound", run)
    result = dispatch_tool("notfound", {})
    assert (result.ok, result.content) == (False, "404 not found")
    assert len(calls) == 1  # a 404 will 404 again: no retry
    assert breaker.failures == []  # and the tool is healthy: breaker untouched
