# Evals — the golden set and how it is scored

`golden.jsonl` (Day 18, hand-labelled by Sakshi; not in the repo yet) holds one
company per line:

```json
{"domain": "razorpay.com", "expected": {"name": "Razorpay", "hq_country": "India", "founded_year": 2014, "industry": "Fintech / Payments", "employee_range": "1001-5000", "funding_stage": null}, "notes": "where each value came from"}
```

- Label only what you verified yourself; leave a field out to skip scoring it.
- `null` means "not knowable from public sources": the agent is right to leave it
  empty or flag it, and wrong to invent a value.
- `employee_range` and `funding_stage` must use the schema's values
  (`relay/agent/schemas.py`).

Scoring (`scoring.py`): `name` and `hq_country` are normalized (legal suffixes,
country aliases), `founded_year` and the two enums must match exactly, `industry`
is judged by an LLM (`judge.py`, Gemini 3.5 Flash-Lite, free tier) or, without a judge, by word overlap
(the results say which). A field the agent put in `flagged_fields` counts as
**abstained**, not wrong. **Accuracy** = correct / scored; **precision** =
correct / answered (the "when it answers, is it right?" number).

```
python -m evals.run_eval --golden evals/golden.example.jsonl --mode demo      # $0 plumbing check
python -m evals.run_eval --mode live --yes-spend                               # paid: estimate printed first
python -m evals.gate                                                           # what CI runs ($0)
```
