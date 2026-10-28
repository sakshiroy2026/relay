# P6 — The eval: is the record right, and does CI keep it right?

## 1. The picture

Everything Relay has measured so far is about *surviving*: crashes, zombies,
outages. None of it says whether the company record is **correct**. That needs
two things a machine can't invent: ground truth (Sakshi's hand-labelled golden
set, Day 18) and a way to score against it that is strict where facts are
exact and careful where they aren't.

**The exam analogy.** Most questions have one right answer and are marked by
the answer key (deterministic scoring). One question is an essay (the
industry), so a second examiner reads it (the LLM judge). A student who writes
"I'm not sure, please check" isn't marked wrong, but isn't marked right either
(abstained). The school doesn't re-sit the exam on every visit; it keeps the
last result on file and refuses to promote anyone whose result is missing or
below the bar (the CI gate).

The harness was **built for $0**: the scorer, the gate and a demo run use no
paid model, and the judge is tested through the real SDK with a mock transport.

## 2. Each file's job

| File | One line |
|---|---|
| `evals/scoring.py` | per field: correct / wrong / abstained. `name` and `hq_country` normalized (legal suffixes, a small alias table), `founded_year` and the enums exact, `industry` by the judge or (labelled as such) word overlap. A null label = "not knowable": a confident value there is **wrong** |
| `evals/judge.py` | one narrow question to Sonnet 5.5, "same industry?", answered as schema-constrained JSON (`output_config.format`), `effort: low`, priced and counted; unavailable/unsure → `None`, never a crash |
| `evals/run_eval.py` | one run per label (`demo`: fake model, $0 → `results.demo.json`; `live`: real model, refuses without `--yes-spend`, estimate first, cost cap → `results.json`), scores, writes every field outcome, cost, model and commit |
| `evals/gate.py` + `thresholds.json` | the $0 CI check: committed **live** results must meet the floor; demo results always fail; no results yet = skipped, said plainly |
| `.github/workflows/ci.yml` | every push/PR: ruff, mypy, all tests on a real Postgres 16 service, the gate. No secrets, no paid calls |
| `evals/README.md`, `golden.example.jsonl` | the label format; two clearly marked format examples for the demo model (not real data) |

## 3. The two numbers, and why both

| | Counts | Answers |
|---|---|---|
| **accuracy** | correct / all scored fields | "how much of the record is right?" |
| **precision** | correct / (correct + wrong) | "when it answers, is it right?" (abstentions excluded) |

A model that guesses everything can look decent on accuracy and terrible on
precision. A model that flags honestly keeps precision high. Relay's prompt
asks for flagging, so the gate watches both. The floors in `thresholds.json`
(precision 0.85, accuracy 0.6, at least 20 labels) are **placeholders**; Sakshi
sets the real bar after the first live eval.

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Deterministic scoring wherever a fact is exact; the judge for `industry` only | Judge every field | A model judging "2014 vs 2014" adds cost and noise; exact facts get exact checks |
| Small, explicit normalization | Fuzzy matching | "Stripe, Inc." = "Stripe", but "Acme" ≠ "Acme Payments" and "U.K." isn't guessed. The eval must not flatter the agent |
| Flagged = abstained, not wrong | Count it wrong | The system prompt asks the agent to flag instead of guessing; punishing that would train the opposite |
| Null label = "unknowable", a value there = wrong | Skip such fields | Inventing a funding stage nobody published is the failure we most want to see |
| Judge = Sonnet 5.5, schema-constrained, effort low | Haiku judging Haiku | A judge should be at least as strong as what it judges; one comparison is ~$0.002 |
| CI reads committed results, never runs the paid eval | Run the eval on every push | Every push would cost money and need a key in CI. Re-running is a deliberate local act; CI only blocks a regression or a missing re-run |
| Demo results go to a different file and always fail the gate | One results file | A $0 demo run must never be able to pass for a real measurement |

## 5. The proofs ($0)

- **Demo eval** (`--mode demo`, local Postgres): `eval-demo-1` (labels matching the
  fake's Acme record) scored **6/6 correct**; `eval-demo-2` (deliberately different
  labels) scored **0/6**, including `funding_stage: null` vs the fake's `series_b`
  → wrong (`unknowable`). Accuracy 0.5, precision 0.5.
- `--mode live` without `--yes-spend` refuses; the gate with no live results says
  "skipped"; the gate pointed at the demo results **fails** ("not produced by a live run").
- `tests/test_evals.py`, 16 tests: normalization (and what it refuses to equate);
  all-correct, abstained, unknowable-but-invented, failed-run cases; the judge used
  and marked, an unsure judge falling back visibly; accuracy vs precision on a mixed
  set; the judge's request shape (json_schema with the verdict enum), its price, and
  different / unsure / 500 verdicts; the gate passing good live results and failing
  demo results, low precision, low accuracy and too few labels.
- 196 tests pass.

**Not yet possible:** a real number. It needs the Day 18 labels, then
`python -m evals.run_eval --mode live --yes-spend` (estimate for 50 labels: about
$0.40 = 50 × $0.006 runs + 50 × $0.002 judge), after Sakshi's go.

## 6. Questions for Sakshi Roy

1. Why report precision *and* accuracy instead of one score?
2. Why is a flagged field not counted as wrong?
3. A label says `funding_stage: null`. The agent answers `series_b`. Why is that
   wrong rather than skipped?
4. Why doesn't CI run the eval itself?
5. Why does the judge only look at `industry`?

<details>
<summary>Answers</summary>

1. They measure different failures. Accuracy falls when the agent abstains a
   lot; precision falls when it guesses wrong. An agent that's right when it
   answers and honest when it can't is what Relay's prompt asks for.
2. The prompt tells the agent to flag what its sources don't settle. Counting
   that as wrong would reward confident guessing. It's counted as abstained, so
   accuracy still shows the gap.
3. Null means "not knowable from public sources". A confident value there was
   invented, which is the exact failure an enrichment product must not have.
4. Each run costs money and needs an API key in CI, and the score only changes
   when the agent or the model changes. So re-running is a deliberate, paid,
   local act whose results are committed, and CI refuses a commit whose
   committed results are missing or below the bar.
5. Every other field has an exact answer that code can check for free.
   Industry is the only free-text field where "same meaning" needs judgement.
</details>

## 7. Interview lines

- "The eval separates accuracy from precision, and an agent that flags a field
  instead of guessing is scored as abstaining, so the metric rewards the
  behaviour the prompt asks for. A null label marks a fact that isn't public, and
  a confident answer there counts as invented."
- "Only the one free-text field goes to an LLM judge, schema-constrained to a
  three-value verdict; everything exact is checked by code. CI never calls a paid
  model: it gates on committed live results, and demo results can never pass."
