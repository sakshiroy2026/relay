"""Unit tests for rebuild_messages: hand-made ledger rows in, messages out. No database."""

from dataclasses import asdict
from typing import Any

import pytest

from relay.agent.fake_script import ENRICH_SCRIPT
from relay.core.replay import rebuild_messages, user_message
from relay.llm.client import Response
from relay.llm.fake import FakeLLMClient

# ── row builders: the same payload shapes loop.py writes ─────────────────


def started(i: int, domain: str = "acmepay.example") -> dict[str, Any]:
    return {"step_index": i, "kind": "run_started", "payload": {"domain": domain}}


def model_call(i: int, turn: int) -> dict[str, Any]:
    reply = ENRICH_SCRIPT[turn]
    response = Response(
        model="fake-planner",
        content=reply.content,
        stop_reason=reply.stop_reason,
        input_tokens=100,
        output_tokens=10,
        tool_calls=list(reply.tool_calls),
    )
    return {
        "step_index": i,
        "kind": "model_call",
        "payload": {**asdict(response), "cost_usd": 0.0005},
    }


def tool_call(i: int, call_id: str, name: str = "fetch_page") -> dict[str, Any]:
    return {
        "step_index": i,
        "kind": "tool_call",
        "payload": {"tool": name, "args": {}, "tool_call_id": call_id},
    }


def tool_result(i: int, call_id: str, name: str = "fetch_page") -> dict[str, Any]:
    return {
        "step_index": i,
        "kind": "tool_result",
        "payload": {"tool": name, "tool_call_id": call_id, "ok": True, "content": f"<{call_id}>"},
    }


# rows 0-4 of a normal run: start, search turn, then the fetch x2 reply
FIRST_FIVE = [
    started(0),
    model_call(1, turn=0),
    tool_call(2, "call_1", "web_search"),
    tool_result(3, "call_1", "web_search"),
    model_call(4, turn=1),
]


# ── tests ────────────────────────────────────────────────────────────────


def test_fresh_run_is_just_the_user_message() -> None:
    state = rebuild_messages([started(0)])

    assert state.messages == [user_message("acmepay.example")]
    assert state.messages[0]["content"] == "Enrich the company with domain: acmepay.example"
    assert state.next_index == 1
    assert state.turns_used == 0
    assert state.pending_tool_calls == []
    assert state.final is None and not state.failed and state.unjudged_answer is None


def test_crash_after_a_model_call_leaves_its_tools_pending() -> None:
    state = rebuild_messages(FIRST_FIVE)

    assert [m["role"] for m in state.messages] == ["user", "assistant", "tool", "assistant"]
    assert state.messages[1]["tool_calls"] == [asdict(ENRICH_SCRIPT[0].tool_calls[0])]
    assert state.messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "web_search",
        "content": "<call_1>",
    }
    assert state.next_index == 5
    assert state.turns_used == 2
    assert [tc.id for tc in state.pending_tool_calls] == ["call_2", "call_3"]


def test_dangling_tool_call_counts_as_missing() -> None:
    # died between tool_call (row 5) and its tool_result
    state = rebuild_messages([*FIRST_FIVE, tool_call(5, "call_2")])

    assert state.next_index == 6
    assert [tc.id for tc in state.pending_tool_calls] == ["call_2", "call_3"]
    assert state.pending_tool_calls[0].name == "fetch_page"
    assert state.pending_tool_calls[0].args == {"url": "https://acmepay.example/about"}


def test_half_finished_turn_keeps_the_confirmed_result() -> None:
    rows = [*FIRST_FIVE, tool_call(5, "call_2"), tool_result(6, "call_2"), tool_call(7, "call_3")]
    state = rebuild_messages(rows)

    assert [m["role"] for m in state.messages] == ["user", "assistant", "tool", "assistant", "tool"]
    assert [tc.id for tc in state.pending_tool_calls] == ["call_3"]
    assert state.next_index == 8


def test_rerun_tool_call_row_then_result_is_confirmed() -> None:
    # edge case 1 already handled once: a second tool_call row for the re-run, then its result
    rows = [
        *FIRST_FIVE,
        tool_call(5, "call_2"),
        tool_call(6, "call_2"),
        tool_result(7, "call_2"),
        tool_call(8, "call_3"),
        tool_result(9, "call_3"),
    ]
    state = rebuild_messages(rows)

    assert state.pending_tool_calls == []
    assert [m["role"] for m in state.messages].count("tool") == 3
    assert state.next_index == 10


def test_fake_model_picks_the_next_reply_after_replay() -> None:
    # turn counting: the replayed conversation has 2 assistant turns -> reply #3 (the final JSON)
    rows = [
        *FIRST_FIVE,
        tool_call(5, "call_2"),
        tool_result(6, "call_2"),
        tool_call(7, "call_3"),
        tool_result(8, "call_3"),
    ]
    state = rebuild_messages(rows)
    reply = FakeLLMClient(ENRICH_SCRIPT).call("fake-planner", "", state.messages, [])

    assert reply.stop_reason == "end_turn"
    assert reply.content == ENRICH_SCRIPT[2].content


def test_final_present_returns_the_record() -> None:
    record = {"name": "Acme Payments"}
    rows = [
        *FIRST_FIVE,
        model_call(5, turn=2),
        {"step_index": 6, "kind": "final", "payload": record},
    ]
    state = rebuild_messages(rows)

    assert state.final == record
    assert state.next_index == 7


def test_error_present_marks_failed() -> None:
    rows = [started(0), {"step_index": 1, "kind": "error", "payload": {"reason": "max_turns"}}]
    state = rebuild_messages(rows)

    assert state.failed
    assert state.final is None


def test_end_turn_reply_without_verdict_is_unjudged() -> None:
    # died after writing the final-looking model_call, before final/error
    state = rebuild_messages([*FIRST_FIVE, model_call(5, turn=2)])

    assert state.unjudged_answer == ENRICH_SCRIPT[2].content
    assert state.pending_tool_calls == []
    assert state.turns_used == 3
    assert state.next_index == 6


def test_ledger_without_run_started_is_a_bug() -> None:
    with pytest.raises(RuntimeError):
        rebuild_messages([model_call(0, turn=0)])
