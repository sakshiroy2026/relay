"""LLM-as-judge for the one free-text field: do two industry labels mean the same?

WHAT IT IS  A narrow judge. It never sees the web or the agent's reasoning,
            only the hand label and the agent's value, and answers with a
            schema-constrained JSON verdict (Gemini's `responseJsonSchema`), so
            the parsing can't drift. Everything checkable without a model is
            checked without one (evals/scoring.py); this is only for
            "Fintech / Payments" vs "Payment processing".
GOES IN     IndustryJudge(model).same(expected, predicted).
COMES OUT   True / False, or None when the judge is unsure or unavailable (the
            scorer then falls back to its heuristic, and says so). `calls` and
            `cost_usd` accumulate, for the results file — priced at Google's
            PAID rate like every Gemini call, though the free tier bills $0.
            `errors` says why a call gave no verdict (never a header or the key).
TOUCHES     The Gemini API through GeminiLLMClient.generate(), so the key handling
            (GEMINI_API_KEY, `x-goog-api-key` header) and the error mapping are
            the adapter's own. Transient trouble is retried like any model call.
FAILS WHEN  No GEMINI_API_KEY -> RuntimeError at construction, before any call.
            Never raises for API trouble: an unavailable judge returns None.
"""

import json
from typing import Any

import httpx2

from relay.core.retry import FatalError, RetryableError, call_with_retries
from relay.llm.client import Response
from relay.llm.gemini_client import GeminiLLMClient, usage_tokens
from relay.llm.pricing import assert_priced, price

# Measured 30 Oct 2026: Flash-Lite answered in 1.2 s; 3.8 Flash returned 503 "high
# demand" on the same request. Comparing two labels doesn't need the stronger model.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_OUTPUT_TOKENS = 4000  # Gemini counts thinking inside this; a verdict is ~50 tokens
RETRY_ATTEMPTS = 3

# Clearer since 30 Oct 2026, after the first live eval: the old wording asked for "the same or a
# reasonably close level of specificity", and "Financial Services / Software" against "Financial
# Services" came back different. evals/results.json keeps the verdicts and score of both rules.
PROMPT = """You compare two short labels describing a company's industry.
Hand label (ground truth): {expected}
Agent's value: {predicted}

Answer "same" if the agent's value describes the same industry as the hand
label, or a more specific part of it, even if it adds a second detail (e.g.
"Fintech / Payments" and "Payment processing" are the same, and so are "Retail"
and "Online grocery retail / Delivery"). Answer "different" if it names another
industry or is so broad that it would also fit very different companies.
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
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: httpx2.Client | None = None,
        api_key: str | None = None,
    ) -> None:
        assert_priced(model)  # a judge whose calls can't be priced can't be budgeted
        self.model = model
        self.llm = GeminiLLMClient(client=client, api_key=api_key)
        self.calls = 0
        self.cost_usd = 0.0
        self.reasons: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def same(self, expected: str, predicted: str) -> bool | None:
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": PROMPT.format(expected=expected, predicted=predicted)}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseJsonSchema": SCHEMA,
            },
        }
        try:
            body, _ = call_with_retries(
                lambda: self.llm.generate(self.model, payload), attempts=RETRY_ATTEMPTS
            )
        except (RetryableError, FatalError) as exc:  # never metered: no reply came back
            self.errors.append(str(exc))
            return None
        self.calls += 1
        uncached, cached, output = usage_tokens(body)
        self.cost_usd += price(
            Response(
                model=self.model,
                content="",
                stop_reason="end_turn",
                input_tokens=uncached,
                output_tokens=output,
                cache_read_tokens=cached,
            )
        )
        candidates = body.get("candidates") or []
        if not candidates or candidates[0].get("finishReason") != "STOP":
            finish = candidates[0].get("finishReason") if candidates else "blocked"
            self.errors.append(f"no verdict: {finish}")
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text") or "" for p in parts if not p.get("thought"))
        try:
            answer = json.loads(text)
            verdict = answer["verdict"]
        except (ValueError, KeyError, TypeError):
            return None
        self.reasons.append(
            {
                "expected": expected,
                "predicted": predicted,
                "verdict": verdict,
                "reason": answer.get("reason"),
            }
        )
        return {"same": True, "different": False}.get(verdict)
