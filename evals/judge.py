"""LLM-as-judge for the one free-text field: do two industry labels mean the same?

WHAT IT IS  A narrow judge. It never sees the web or the agent's reasoning,
            only the hand label and the agent's value, and answers with a
            schema-constrained JSON verdict (structured outputs), so the
            parsing can't drift. Everything checkable without a model is
            checked without one (evals/scoring.py); this is only for
            "Fintech / Payments" vs "Payment processing".
GOES IN     IndustryJudge(model).same(expected, predicted).
COMES OUT   True / False, or None when the judge is unsure or unavailable (the
            scorer then falls back to its heuristic, and says so). `calls` and
            `cost_usd` accumulate, for the results file.
TOUCHES     The Anthropic API (ANTHROPIC_API_KEY, read by the SDK).
FAILS WHEN  Never raises for API trouble: an unavailable judge returns None.
"""

import json
from typing import Any

import anthropic

from relay.llm.client import Response
from relay.llm.pricing import assert_priced, price

PROMPT = """You compare two short labels describing a company's industry.
Hand label (ground truth): {expected}
Agent's value: {predicted}

Answer "same" if the agent's value describes the same industry as the hand
label, at the same or a reasonably close level of specificity (e.g. "Fintech /
Payments" and "Payment processing" are the same). Answer "different" if it names
another industry or is so broad that it would also fit very different companies.
Answer "unsure" only if you genuinely cannot tell."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["same", "different", "unsure"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


class IndustryJudge:
    def __init__(self, model: str, client: anthropic.Anthropic | None = None) -> None:
        assert_priced(model)  # a judge whose calls can't be priced can't be budgeted
        self.model = model
        self.client = client or anthropic.Anthropic(max_retries=2, timeout=60)
        self.calls = 0
        self.cost_usd = 0.0
        self.reasons: list[dict[str, Any]] = []

    def same(self, expected: str, predicted: str) -> bool | None:
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                output_config={
                    "effort": "low",  # a two-label comparison, not a research task
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                messages=[
                    {
                        "role": "user",
                        "content": PROMPT.format(expected=expected, predicted=predicted),
                    }
                ],
            )
        except anthropic.APIError:
            return None
        self.calls += 1
        self.cost_usd += price(
            Response(
                model=self.model,
                content="",
                stop_reason="end_turn",
                input_tokens=int(msg.usage.input_tokens or 0),
                output_tokens=int(msg.usage.output_tokens or 0),
            )
        )
        if msg.stop_reason != "end_turn":
            return None  # refusal / max_tokens: no verdict
        try:
            text = next(b.text for b in msg.content if b.type == "text")
            verdict = json.loads(text)["verdict"]
        except (StopIteration, ValueError, KeyError):
            return None
        self.reasons.append({"expected": expected, "predicted": predicted, "verdict": verdict})
        return {"same": True, "different": False}.get(verdict)
