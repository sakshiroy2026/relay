"""The schema repair ladder: salvage an almost-right final answer before failing the run.

WHAT IT IS  Three rungs, cheapest first. Rung 0: the answer parses as-is.
            Rung 1 (free, deterministic): the answer is JSON wrapped in noise —
            code fences or prose — so extract the first balanced JSON object
            and try again. Rung 2 (one model call, decided by the loop): send
            the model its own validation errors and ask for corrected JSON.
            Rung 3 (escalate to a stronger model) is deferred until a second
            real model exists; see learn/DAY9.md.
GOES IN     parse_final: the model's final text. correction_message: the
            validation errors of the failed answer.
COMES OUT   parse_final -> (record, how_it_was_repaired | None, errors):
            a validated CompanyRecord with how it was salvaged (None = rung 0),
            or (None, None, the errors) when rungs 0-1 both fail.
TOUCHES     Nothing: pure functions, no database, no model. The loop owns
            rung 2, because only it can spend money.
FAILS WHEN  Never raises on bad input; bad input is the whole point.
"""

from pydantic import ValidationError

from relay.agent.schemas import CompanyRecord


def extract_json(text: str) -> str | None:
    """The first balanced {...} object in the text, or None.

    Handles the two ways models wrap good JSON in noise: code fences and
    surrounding prose. Brace counting skips braces inside JSON strings, so a
    description containing '{' doesn't break it.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
        start = text.find("{", start + 1)  # unbalanced from here; try the next opener
    return None


def parse_final(content: str) -> tuple[CompanyRecord | None, str | None, str]:
    """Rungs 0 and 1. Returns (record, repair | None, errors_if_both_failed)."""
    try:
        return CompanyRecord.model_validate_json(content), None, ""  # rung 0
    except ValidationError as strict_error:
        candidate = extract_json(content)
        if candidate is not None and candidate.strip() != content.strip():
            try:
                return CompanyRecord.model_validate_json(candidate), "extracted_json", ""  # rung 1
            except ValidationError:
                pass  # the extracted object is broken too; report the original errors
        return None, None, str(strict_error)


def correction_message(errors: str) -> str:
    """The rung-2 re-ask. Written to the ledger, so replay rebuilds it verbatim."""
    return (
        "Your final answer did not match the required record schema. "
        f"The validation errors were:\n{errors}\n"
        "Reply with the corrected JSON record only — no prose, no code fences."
    )
