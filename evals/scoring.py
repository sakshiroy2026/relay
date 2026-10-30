"""Score one enrichment record against a hand-made label (P6, Day 19/20).

WHAT IT IS  The deterministic half of the eval. For each field it decides
            correct / wrong / abstained, with honest normalization (company
            suffixes, country aliases) and nothing fuzzier than that. A field
            the agent put in `flagged_fields` instead of guessing is ABSTAINED,
            not wrong: the system prompt asks it to flag what sources don't
            settle, and the metric must reward doing so. `funding_stage: "unknown"`
            is the schema's way of giving no answer, so it counts as empty rather
            than as a claim. `industry` is free
            text, so it can be handed to the LLM judge (evals/judge.py); without
            a judge a word-overlap heuristic stands in, labelled as such.
GOES IN     score_record(expected, predicted, judge=None): the label's
            `expected` dict (a null value = "not knowable from public sources";
            `hq_country` may be a list when a company is dual-headquartered, and
            then any of them, or several of them together, is right) and the
            run's result record (or None for a failed run).
COMES OUT   {field: {"outcome", "expected", "predicted", "method"}} per record;
            summarize() -> accuracy, precision, abstention per field and overall.
TOUCHES     Nothing (the judge, if passed, makes its own calls).
FAILS WHEN  Never: a missing record scores every field `wrong` (`no_record`).
"""

import re
from collections.abc import Callable
from typing import Any

FIELDS = ("name", "hq_country", "founded_year", "industry", "employee_range", "funding_stage")

_SUFFIXES = {
    "inc", "incorporated", "ltd", "limited", "llc", "plc", "pvt", "private", "corp",
    "corporation", "co", "company", "gmbh", "sa", "ag", "bv", "pte", "pty", "technologies",
}  # fmt: skip
_COUNTRY_ALIASES = {
    "us": "united states", "usa": "united states", "united states of america": "united states",
    "uk": "united kingdom", "great britain": "united kingdom", "england": "united kingdom",
    "uae": "united arab emirates", "the netherlands": "netherlands", "holland": "netherlands",
}  # fmt: skip

Judge = Callable[[str, str], bool | None]  # (expected, predicted) -> same meaning? None = unsure


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def norm_name(name: str) -> str:
    words = _words(name)
    while words and words[-1] in _SUFFIXES:  # "Razorpay Software Pvt Ltd" keeps "razorpay software"
        words.pop()
    return " ".join(words)


def norm_country(country: str) -> str:
    c = " ".join(_words(country))
    return _COUNTRY_ALIASES.get(c, c)


def _industry_heuristic(expected: str, predicted: str) -> bool:
    a, b = set(_words(expected)), set(_words(predicted))
    return bool(a and b) and len(a & b) / len(a | b) >= 0.5


def _hq_matches(expected: Any, predicted: Any) -> bool:
    """One country must match exactly. A dual HQ accepts either, or both named together."""
    if not isinstance(expected, list):
        return norm_country(str(expected)) == norm_country(str(predicted))
    accepted = {norm_country(str(country)) for country in expected}
    parts = re.split(r"[,;/&]| and ", str(predicted))
    return all(norm_country(part) in accepted for part in parts)


def _same(field: str, expected: Any, predicted: Any, judge: Judge | None) -> tuple[bool, str]:
    if field == "name":
        return norm_name(str(expected)) == norm_name(str(predicted)), "normalized"
    if field == "hq_country":
        return _hq_matches(expected, predicted), "normalized"
    if field == "founded_year":
        try:
            return int(expected) == int(predicted), "exact"
        except (TypeError, ValueError):
            return False, "exact"
    if field == "industry":
        if str(expected).casefold().strip() == str(predicted).casefold().strip():
            return True, "exact"
        if judge is not None:
            verdict = judge(str(expected), str(predicted))
            if verdict is not None:
                return verdict, "judge"
        return _industry_heuristic(str(expected), str(predicted)), "heuristic"
    return expected == predicted, "exact"  # the enums: employee_range, funding_stage


def score_record(
    expected: dict[str, Any], predicted: dict[str, Any] | None, judge: Judge | None = None
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    flagged = set((predicted or {}).get("flagged_fields") or [])
    for field in FIELDS:
        if field not in expected:
            continue  # not labelled: not scored
        exp = expected[field]
        got = None if predicted is None else predicted.get(field)
        wanted = None if field == "funding_stage" and exp == "unknown" else exp
        answer = None if field == "funding_stage" and got == "unknown" else got
        if predicted is None:
            outcome, method = "wrong", "no_record"
        elif wanted is None:  # the label says: not knowable from public sources
            outcome = "correct" if answer is None or field in flagged else "wrong"
            method = "unknowable"  # a confident answer here was made up
        elif field in flagged:
            outcome, method = "abstained", "flagged"
        elif answer is None:
            outcome, method = "wrong", "missing"
        else:
            same, method = _same(field, wanted, answer, judge)
            outcome = "correct" if same else "wrong"
        out[field] = {"outcome": outcome, "expected": exp, "predicted": got, "method": method}
    return out


def summarize(scored: list[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """accuracy = correct / scored; precision = correct / answered (correct + wrong)."""

    def stats(outcomes: list[str]) -> dict[str, Any]:
        n = len(outcomes)
        c, w, a = (outcomes.count(k) for k in ("correct", "wrong", "abstained"))
        return {
            "scored": n,
            "correct": c,
            "wrong": w,
            "abstained": a,
            "accuracy": round(c / n, 3) if n else None,
            "precision": round(c / (c + w), 3) if c + w else None,
            "abstention_rate": round(a / n, 3) if n else None,
        }

    per_field = {f: stats([rec[f]["outcome"] for rec in scored if f in rec]) for f in FIELDS}
    overall = stats([v["outcome"] for rec in scored for v in rec.values()])
    return {"overall": overall, "per_field": per_field, "records": len(scored)}
