"""The repair ladder's pure rungs (relay/core/repair.py). No database, no model."""

import json

from relay.agent.fake_script import _FINAL_RECORD, enrich_script
from relay.core.repair import correction_message, extract_json, parse_final

GOOD = json.dumps(_FINAL_RECORD)


def test_rung_0_a_clean_answer_needs_no_repair() -> None:
    record, repaired_by, errors = parse_final(GOOD)
    assert record is not None and repaired_by is None and errors == ""


def test_rung_1_strips_code_fences() -> None:
    record, repaired_by, _ = parse_final(f"```json\n{GOOD}\n```")
    assert record is not None and record.name == "Acme Payments"
    assert repaired_by == "extracted_json"


def test_rung_1_extracts_from_prose() -> None:
    record, repaired_by, _ = parse_final(f"Sure! Here it is:\n{GOOD}\nHope that helps.")
    assert record is not None and repaired_by == "extracted_json"


def test_rung_1_cannot_fix_a_bad_value() -> None:
    bad = json.dumps({**_FINAL_RECORD, "founded_year": 1599})  # valid JSON, invalid value
    record, repaired_by, errors = parse_final(bad)
    assert record is None and repaired_by is None
    assert "founded_year" in errors  # the re-ask message will carry this


def test_no_json_at_all_fails_with_the_original_errors() -> None:
    record, repaired_by, errors = parse_final("I could not assemble the record.")
    assert record is None and repaired_by is None and errors


def test_extract_json_ignores_braces_inside_strings() -> None:
    text = 'noise {"description": "curly {brace} inside", "n": 1} noise'
    assert extract_json(text) == '{"description": "curly {brace} inside", "n": 1}'


def test_extract_json_skips_an_unbalanced_opener() -> None:
    assert extract_json('broken { then good {"a": 1}') == '{"a": 1}'
    assert extract_json("no object here") is None


def test_correction_message_carries_the_errors() -> None:
    message = correction_message("founded_year must be >= 1600")
    assert "founded_year must be >= 1600" in message
    assert "JSON" in message


def test_broken_scripts_stay_deterministic() -> None:
    """Same conversation length -> same reply, so a resumed run replays cleanly."""
    for kind in ("fenced", "prose", "bad_year", "hopeless"):
        script = enrich_script(kind)
        assert len(script) == 5
        assert script[:3] == enrich_script(kind)[:3]
        assert script[3].stop_reason == "end_turn"
    assert enrich_script("") == enrich_script()  # the default is the unbroken script
