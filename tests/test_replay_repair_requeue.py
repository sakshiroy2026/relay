"""Replay of the Day 9 rows: repair_requested and requeued. No database."""

from typing import Any

from relay.core.repair import correction_message
from relay.core.replay import rebuild_messages
from tests.test_check_run import full_run, model_call, row

REASK = correction_message("founded_year must be >= 1600")


def repair_row(i: int) -> dict[str, Any]:
    return row(i, "repair_requested", {"message": REASK, "errors": "founded_year"})


def test_repair_requested_becomes_the_user_message_and_counts() -> None:
    rows = full_run()[:13] + [repair_row(13)]  # judged broken, re-ask written, then died
    state = rebuild_messages(rows)
    assert state.repairs_used == 1
    assert state.messages[-1] == {"role": "user", "content": REASK}
    # the broken end_turn reply was already judged: nothing is unjudged or pending
    assert state.unjudged_answer is None and state.pending_tool_calls == []
    assert state.next_index == 14


def test_the_reask_reply_is_unjudged_if_the_worker_dies_after_it() -> None:
    rows = full_run()[:13] + [repair_row(13), model_call(14, 3)]
    state = rebuild_messages(rows)
    assert state.repairs_used == 1
    assert state.unjudged_answer is not None  # judged with repairs_used=1: no second re-ask


def test_error_without_requeue_is_still_failed() -> None:
    rows = full_run()[:13] + [row(13, "error", {"reason": "invalid_final_answer"})]
    state = rebuild_messages(rows)
    assert state.failed is True
    assert state.next_index == 14


def test_requeued_clears_the_verdict_and_the_run_continues() -> None:
    rows = full_run()[:9]  # died before turn 2's model call
    rows += [
        row(9, "error", {"reason": "model_unavailable", "detail": "503"}),
        row(10, "requeued", {"from_status": "failed", "requeued_by": "scripts.dlq"}),
    ]
    state = rebuild_messages(rows)
    assert state.failed is False
    assert state.final is None
    assert state.next_index == 11
    assert state.turns_used == 2  # the two paid turns are kept, not re-paid
    assert state.pending_tool_calls == []  # the error ended that conversation turn


def test_requeued_after_budget_exceeded_keeps_the_spend() -> None:
    rows = full_run()[:9] + [
        row(9, "budget_exceeded", {"spent_usd": 0.002, "budget_usd": 0.001}),
        row(10, "requeued", {"from_status": "failed", "new_budget_usd": 0.5}),
    ]
    state = rebuild_messages(rows)
    assert state.failed is False
    assert state.spent_usd > 0  # the ledger's spend still counts against the new budget
