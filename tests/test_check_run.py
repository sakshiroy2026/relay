"""check_run's ledger-shape checks and the fencing harness's next_kind. No database.

The ledgers are built the way loop.py writes them: run the real fake script
through hand-made rows, then break them in the ways a failed fence would.
"""

from typing import Any

from chaos.fencing_test import next_kind
from relay.agent.fake_script import ENRICH_SCRIPT
from scripts.check_run import find_problems


def row(i: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"step_index": i, "kind": kind, "payload": payload}


def model_call(i: int, turn: int) -> dict[str, Any]:
    reply = ENRICH_SCRIPT[turn]
    tool_calls = [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in reply.tool_calls]
    payload = {
        "content": reply.content,
        "tool_calls": tool_calls,
        "stop_reason": reply.stop_reason,
        "cost_usd": 0.001,
    }
    return row(i, "model_call", payload)


def tool(i: int, kind: str, call_id: str) -> dict[str, Any]:
    return row(i, kind, {"tool": "web_search", "tool_call_id": call_id, "content": "[]"})


def full_run() -> list[dict[str, Any]]:
    """The 14 rows of a normal run: 4 model calls, 4 tool calls, final."""
    rows = [row(0, "run_started", {"domain": "acmepay.example"}), model_call(1, 0)]
    rows += [tool(2, "tool_call", "call_1"), tool(3, "tool_result", "call_1"), model_call(4, 1)]
    rows += [tool(5, "tool_call", "call_2"), tool(6, "tool_result", "call_2")]
    rows += [tool(7, "tool_call", "call_3"), tool(8, "tool_result", "call_3"), model_call(9, 2)]
    rows += [tool(10, "tool_call", "call_4"), tool(11, "tool_result", "call_4")]
    rows += [model_call(12, 3), row(13, "final", {"name": "Acme Payments"})]
    return rows


def test_a_normal_run_is_clean() -> None:
    assert find_problems(full_run()) == []


def test_a_rerun_tool_call_after_a_crash_is_allowed() -> None:
    # Day 6 edge case 1: a dangling tool_call, then a new tool_call for the same id
    rows = full_run()[:3] + [tool(3, "tool_call", "call_1"), tool(4, "tool_result", "call_1")]
    assert find_problems(rows) == []


def test_gap_is_reported() -> None:
    rows = full_run()
    del rows[5]
    assert any("gaps" in p for p in find_problems(rows))


def test_repeated_model_call_is_reported() -> None:
    rows = full_run()[:4] + [model_call(4, 0)]  # turn 0's reply paid for again
    assert any("repeats step 1" in p for p in find_problems(rows))


def test_tool_result_nobody_asked_for_is_reported() -> None:
    rows = full_run()[:4] + [tool(4, "tool_result", "call_9")]
    assert any("never asked for" in p for p in find_problems(rows))


def test_tool_confirmed_twice_is_reported() -> None:
    rows = full_run()[:4] + [tool(4, "tool_result", "call_1")]
    assert any("confirmed twice" in p for p in find_problems(rows))


def test_anything_after_the_verdict_is_reported() -> None:
    rows = full_run() + [model_call(14, 0)]
    assert any("after the verdict" in p for p in find_problems(rows))


def test_ledger_must_start_with_run_started() -> None:
    rows = full_run()
    rows[0] = row(0, "model_call", rows[1]["payload"])
    assert any("not run_started" in p for p in find_problems(rows))


def test_next_kind_follows_the_loop() -> None:
    rows = full_run()
    assert next_kind(rows[:1]) == "model_call"  # fresh run
    assert next_kind(rows[:2]) == "tool_call"  # reply asked for a tool
    assert next_kind(rows[:3]) == "tool_result"  # mid-tool
    assert next_kind(rows[:7]) == "tool_call"  # second of two fetches
    assert next_kind(rows[:9]) == "model_call"  # all tools of the turn confirmed
    assert next_kind(rows[:13]) == "final"  # end_turn reply, not judged yet
    assert next_kind(rows) == "nothing (already decided)"


def test_the_same_answer_after_a_reask_is_not_a_repeat() -> None:
    """A re-ask (Day 9) or escalation (P5) may legitimately get the same reply back."""
    reask = row(13, "repair_requested", {"message": "fix it", "errors": "x"})
    rows = [*full_run()[:13], reask, model_call(14, 3)]
    assert find_problems(rows) == []
    # ...but the same reply twice with no re-ask between them is still caught
    assert any("repeats" in p for p in find_problems([*full_run()[:13], model_call(13, 3)]))
