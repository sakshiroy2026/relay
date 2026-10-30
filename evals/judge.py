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
TOUCHES     The Gemini API over httpx2, the same way relay/llm/gemini_client.py
            does: the key comes from GEMINI_API_KEY and goes in the
            `x-goog-api-key` header, never the URL, never a log.
FAILS WHEN  No GEMINI_API_KEY -> RuntimeError at construction, before any call.
            Never raises for API trouble: an unavailable judge returns None.
"""

import json
import os
from typing import Any

import httpx2

from relay.llm.client import Response
from relay.llm.gemini_client import API_ROOT, error_text, usage_tokens
from relay.llm.pricing import assert_priced, price

DEFAULT_MODEL = "gemini-3.8-flash"  # stronger than the agent's flash-lite, still free tier
MAX_OUTPUT_TOKENS = 4000  # Gemini counts thinking inside this; a verdict is ~50 tokens

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
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: httpx2.Client | None = None,
        api_key: str | None = None,
    ) -> None:
        assert_priced(model)  # a judge whose calls can't be priced can't be budgeted
        key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set: the judge cannot run")
        self.model = model
        self.client = client or httpx2.Client(timeout=60)
        self._headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
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
            response = self.client.post(
                f"{API_ROOT}/models/{self.model}:generateContent",
                json=payload,
                headers=self._headers,
            )
        except httpx2.RequestError as exc:
            self.errors.append(type(exc).__name__)
            return None
        if response.status_code != 200:  # never metered when the API refused the request
            self.errors.append(error_text(response))
            return None
        body: dict[str, Any] = response.json()
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
            verdict = json.loads(text)["verdict"]
        except (ValueError, KeyError, TypeError):
            return None
        self.reasons.append({"expected": expected, "predicted": predicted, "verdict": verdict})
        return {"same": True, "different": False}.get(verdict)
