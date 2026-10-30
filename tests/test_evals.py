"""P6: the scorer, the LLM judge (Gemini REST, mock transport) and the CI gate. No network."""

import json
from typing import Any

import httpx2
import pytest

from evals.gate import check
from evals.judge import IndustryJudge
from evals.scoring import norm_country, norm_name, score_record, summarize

EXPECTED = {
    "name": "Razorpay Software Pvt Ltd",
    "hq_country": "India",
    "founded_year": 2014,
    "industry": "Fintech / Payments",
    "employee_range": "1001-5000",
    "funding_stage": None,  # not knowable from public sources
}


def predicted(**over: Any) -> dict[str, Any]:
    base = {
        "name": "Razorpay Software",  # the label's legal name minus its suffixes
        "hq_country": "india",
        "founded_year": 2014,
        "industry": "Fintech / Payments",
        "employee_range": "1001-5000",
        "funding_stage": None,
        "flagged_fields": [],
    }
    return {**base, **over}


def outcomes(scored: dict[str, Any]) -> dict[str, str]:
    return {f: v["outcome"] for f, v in scored.items()}


# ── scoring ──────────────────────────────────────────────────────────────
def test_normalization_is_honest_not_generous() -> None:
    assert norm_name("Razorpay Software Pvt. Ltd.") == "razorpay software"
    assert norm_name("Stripe, Inc.") == norm_name("stripe")
    assert norm_name("Acme Payments") != norm_name("Acme")  # a real difference stays one
    assert norm_country("USA") == norm_country("United States of America")
    assert norm_country("U.K.") != norm_country("United Kingdom")  # no guessing beyond the table


def test_a_right_record_scores_all_correct() -> None:
    scored = score_record(EXPECTED, predicted())
    assert set(outcomes(scored).values()) == {"correct"}


def test_flagging_is_abstaining_not_being_wrong() -> None:
    scored = score_record(
        EXPECTED, predicted(employee_range="51-200", flagged_fields=["employee_range"])
    )
    assert outcomes(scored)["employee_range"] == "abstained"


def test_inventing_an_unknowable_value_is_wrong() -> None:
    scored = score_record(EXPECTED, predicted(funding_stage="series_c"))
    assert scored["funding_stage"] == {
        "outcome": "wrong", "expected": None, "predicted": "series_c", "method": "unknowable"
    }  # fmt: skip


def test_a_failed_run_scores_every_field_wrong() -> None:
    scored = score_record(EXPECTED, None)
    assert set(outcomes(scored).values()) == {"wrong"}
    assert {v["method"] for v in scored.values()} == {"no_record"}


def test_industry_uses_the_judge_and_says_so() -> None:
    scored = score_record(EXPECTED, predicted(industry="Payment processing"), lambda e, p: True)
    assert scored["industry"]["outcome"] == "correct" and scored["industry"]["method"] == "judge"

    unsure = score_record(EXPECTED, predicted(industry="Payments tech"), lambda e, p: None)
    assert unsure["industry"]["method"] == "heuristic"  # an unsure judge falls back, visibly


def test_summary_separates_accuracy_from_precision() -> None:
    right = score_record(EXPECTED, predicted())
    flagged = score_record(
        EXPECTED, predicted(employee_range="x", flagged_fields=["employee_range"])
    )
    wrong = score_record(EXPECTED, predicted(founded_year=1999))
    o = summarize([right, flagged, wrong])["overall"]
    assert (o["scored"], o["correct"], o["wrong"], o["abstained"]) == (18, 16, 1, 1)
    assert o["accuracy"] == round(16 / 18, 3)
    assert o["precision"] == round(16 / 17, 3)  # abstentions don't count against it


# ── the judge, through a mock Gemini transport ───────────────────────────
JUDGE_MODEL = "gemini-3.8-flash"


def judge_with(
    answer: dict[str, Any] | int, finish: str = "STOP"
) -> tuple[IndustryJudge, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if isinstance(answer, int):
            return httpx2.Response(answer, json={"error": {"code": answer, "message": "x"}})
        return httpx2.Response(
            200,
            json={
                "candidates": [{
                    "content": {"role": "model", "parts": [
                        {"text": "(weighing)", "thought": True, "thoughtSignature": "s"},
                        {"text": json.dumps(answer)},
                    ]},
                    "finishReason": finish,
                }],
                "usageMetadata": {
                    "promptTokenCount": 300, "candidatesTokenCount": 30, "thoughtsTokenCount": 10,
                },
            },
        )  # fmt: skip

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return IndustryJudge(JUDGE_MODEL, client=client, api_key="test-not-a-real-key"), seen


def test_judge_asks_for_a_schema_constrained_verdict_and_prices_it() -> None:
    judge, seen = judge_with({"verdict": "same", "reason": "both payments"})
    assert judge.same("Fintech / Payments", "Payment processing") is True
    request = seen[0]
    assert str(request.url).endswith(f"/models/{JUDGE_MODEL}:generateContent")
    assert request.headers["x-goog-api-key"] == "test-not-a-real-key"
    assert "test-not-a-real-key" not in str(request.url)  # the key never rides in the URL
    config = json.loads(request.content)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["properties"]["verdict"]["enum"] == [
        "same",
        "different",
        "unsure",
    ]
    # priced at 3.8 Flash's paid rate; thinking counts as output
    assert judge.calls == 1 and judge.cost_usd == pytest.approx((300 * 0.75 + 40 * 3.75) / 1e6)


def test_judge_without_a_key_refuses_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        IndustryJudge(JUDGE_MODEL)


def test_a_judge_cut_off_by_max_tokens_gives_no_verdict_but_is_still_priced() -> None:
    judge, _ = judge_with({"verdict": "same", "reason": "x"}, finish="MAX_TOKENS")
    assert judge.same("a", "b") is None
    assert judge.calls == 1 and judge.cost_usd > 0
    assert judge.errors == ["no verdict: MAX_TOKENS"]


def test_a_refused_judge_call_says_why_without_quoting_the_key() -> None:
    judge, _ = judge_with(400)
    assert judge.same("a", "b") is None
    assert judge.calls == 0 and judge.cost_usd == 0  # the API refused: nothing was metered
    assert judge.errors[0].startswith("400") and "test-not-a-real-key" not in judge.errors[0]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"verdict": "different", "reason": "x"}, False),
        ({"verdict": "unsure", "reason": "x"}, None),
        (500, None),  # an unavailable judge is "no verdict", never a crash
    ],
)
def test_judge_verdicts(answer: dict[str, Any] | int, expected: bool | None) -> None:
    judge, _ = judge_with(answer)
    assert judge.same("a", "b") is expected


# ── the CI gate ──────────────────────────────────────────────────────────
THRESHOLDS = {"min_labels": 20, "min_precision": 0.85, "min_accuracy": 0.6}


def results(mode: str = "live", labels: int = 25, acc: float = 0.7, prec: float = 0.9) -> dict:
    return {
        "mode": mode,
        "labels": labels,
        "summary": {"overall": {"accuracy": acc, "precision": prec}},
    }


def test_gate_passes_good_live_results() -> None:
    assert check(results(), THRESHOLDS) == []


@pytest.mark.parametrize(
    ("res", "needle"),
    [
        (results(mode="demo"), "not produced by a live run"),
        (results(prec=0.8), "precision"),
        (results(acc=0.5), "accuracy"),
        (results(labels=5), "labels"),
    ],
)
def test_gate_fails_what_it_should(res: dict, needle: str) -> None:
    assert any(needle in f for f in check(res, THRESHOLDS))
