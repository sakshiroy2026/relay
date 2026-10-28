"""The fake model's lines for one enrichment run: search → fetch x2 → save → final record.

enrich_script(broken_final) can break the final answer on purpose (Day 9 proofs):
the record is wrapped in noise or given an invalid value, and one corrected reply
is appended for the repair ladder's re-ask. Everything stays deterministic and
turn-counted, so a resumed run gets the same replies the dead worker would have.
"""

import json

from relay.llm.client import ToolCall
from relay.llm.fake import ScriptedReply

_FINAL_RECORD = {
    "name": "Acme Payments",
    "hq_country": "India",
    "founded_year": 2016,
    "industry": "Fintech / Payments",
    "employee_range": "201-1000",
    "funding_stage": "series_b",
    "description": "Acme Payments builds payment infrastructure for small online businesses.",
    "sources": [
        "https://acmepay.example/about",
        "https://news.example/acme-series-b",
    ],
    "confidence": {"founded_year": 0.9, "employee_range": 0.6, "funding_stage": 0.9},
    "flagged_fields": [],
}

ENRICH_SCRIPT: list[ScriptedReply] = [
    ScriptedReply(
        content="I'll start by searching for the company.",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="call_1", name="web_search", args={"query": "Acme Payments"})],
    ),
    ScriptedReply(
        content="Two useful results. Reading both pages.",
        stop_reason="tool_use",
        tool_calls=[
            ToolCall(id="call_2", name="fetch_page", args={"url": "https://acmepay.example/about"}),
            ToolCall(
                id="call_3", name="fetch_page", args={"url": "https://news.example/acme-series-b"}
            ),
        ],
    ),
    ScriptedReply(
        content="Both pages agree. Saving the record.",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="call_4", name="save_company", args=_FINAL_RECORD)],
    ),
    ScriptedReply(
        content=json.dumps(_FINAL_RECORD),
        stop_reason="end_turn",
        tool_calls=[],
    ),
]


def enrich_script(broken_final: str = "") -> list[ScriptedReply]:
    """The script, optionally with a broken final answer (settings.fake_broken_final).

    fenced    — the right record inside a markdown code fence (rung 1 fixes it)
    prose     — the right record wrapped in chat prose (rung 1 fixes it)
    bad_year  — valid JSON, founded_year 1599 fails validation (only rung 2 can fix it)
    hopeless  — no JSON at all, and the re-ask reply is no better (the ladder gives up)
    """
    if not broken_final:
        return ENRICH_SCRIPT

    good = json.dumps(_FINAL_RECORD)
    broken = {
        "fenced": f"```json\n{good}\n```",
        "prose": f"Here is the finished record for you:\n{good}\nLet me know if anything is off.",
        "bad_year": json.dumps({**_FINAL_RECORD, "founded_year": 1599}),
        "hopeless": "I was unable to assemble the record from the sources.",
    }[broken_final]
    reask_reply = (
        "I still cannot produce a valid record."  # distinct text: two identical end_turn
        if broken_final == "hopeless"  # replies would (rightly) trip check_run
        else good
    )
    return [*ENRICH_SCRIPT[:3], ScriptedReply(content=broken), ScriptedReply(content=reask_reply)]
