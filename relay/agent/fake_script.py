"""The fake model's lines for one enrichment run: search → fetch x2 → final record."""

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
        content=json.dumps(_FINAL_RECORD),
        stop_reason="end_turn",
        tool_calls=[],
    ),
]
