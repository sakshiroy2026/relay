# P6 live: the first accuracy numbers

Read `learn/P6_EVAL.md` first: it explains the scorer and the gate. This note is what
happened when they met real companies, a real model and the real web.

## 1. The picture

Relay could already prove it survives crashes. It couldn't say whether its answers were right.
Now it can: 20 companies, hand-researched, graded field by field.

```
hand labels (golden.jsonl) ---------------+
                                          v
20 real runs -> records -> scorer + judge -> 99 / 119 correct -> results.json -> CI gate
                                     (90 / 119 under the judge's first rule)
```

**The exam analogy.** A mark depends on three things: the student, the answer key and the
marker. A low mark doesn't say which one is at fault. The first mark was 0.76. Sorting the misses
showed many were the marker's, so the marking rule was made clearer and everything re-marked: 0.83.
Both marks are kept, because a mark that moved after someone saw it needs explaining.

## 2. Each file's job

| File | One line |
|---|---|
| `evals/golden.jsonl` | the answer key: 20 companies, 119 scored fields, sources in `notes` |
| `evals/run_eval.py` | spaces live runs, stops at `--max-requests`, stops on a daily-quota error, counts only scored runs as `labels` |
| `evals/scoring.py` | `hq_country` may be a list (dual HQ); `funding_stage: "unknown"` counts as no answer |
| `evals/judge.py` | the clearer `industry` rule; keeps the judge's stated reason for each verdict |
| `evals/results.json` | every field's outcome, the summary, and the first rule's verdicts and summary; the CI gate reads it |
| `evals/thresholds.json` | floors set a little under this run, to catch regressions |
| `tests/test_evals.py` | the scoring rules and the judge's request and verdict handling |
| `tests/test_idempotency_race.py` | a flaky timing assumption removed (the eval's load exposed it) |

## 3. Runtime story

One company, say `razorpay.com`. `run_eval` creates a `live` run; a worker claims it. The model
checks `lookup_existing`, searches the real web through Tavily a few times, saves the record and
answers. Its first final answer came back empty, so the repair ladder re-asked it once, and the
second one validated. The scorer compares each field with the label, and `industry` goes to the
judge unless the text matches exactly. Then a 60 s wait for the free tier's per-minute limit, and
the next company. A run took 20 s on average, 33 s at most. The whole eval took 26 minutes.

## 4. The numbers

30 Oct 2026. **Local Postgres**, `gemini-3.5-flash-lite` at the model's default thinking level, real
tools, free tier ($0 billed). 20/20 runs succeeded, with no retried model calls. Priced at Google's
paid rate: $0.0079 a run. All run ids: `evals/results.json`.

| Field | Correct | Accuracy |
|---|---|---|
| `hq_country` | 19/20 | 0.95 |
| `name` | 18/20 | 0.90 |
| `founded_year` | 16/19 | 0.84 |
| `funding_stage` | 17/20 | 0.85 |
| `industry`, clearer rule | 15/20 | 0.75 |
| `employee_range` | 14/20 | 0.70 |
| **Overall, clearer rule** | **99/119** | **0.83** |
| **Without `industry`** (no judge involved) | **84/99** | **0.85** |
| Overall, the first rule (`industry` 6/20 = 0.30) | 90/119 | 0.76 |

Precision equals accuracy because the agent never abstained (section 7).

**Added after the run, with no requests:** the three funding labels left open. Stripe (Series I, Apr
2024) and Canva (Series F, May 2024, Tracxn) are `series_d+`, from funding tables. Atlassian is `public`:
the owner's table lists Series D (2014) as its last round, a news search found its Nasdaq listing in Dec
2015, and the owner chose the label, the same way the sheet treats Zomato. That search covered 2015 only.
The owner wasn't shown the agent's answers. All three matched, which moved the factual fields from 81/96
(0.84) to 84/99 (0.85) and left overall at 0.83. Nothing is left out of scoring now.

## 5. The judge: what changed, and how far to trust it

**The first rule** asked for "the same or a reasonably close level of specificity". It marked "Financial
Services / Software" as different from "Financial Services", which is plainly wrong.

**The clearer rule** adds: a more specific part of the hand label's industry counts as the same, even
with a second detail added. Same model, same saved answers, no agent runs: 18 requests. **This rule was
written after seeing the first score**, so `industry` isn't a blind measurement. It changed 9 verdicts,
all from different to same, and left 5 pairs different.

| Flip | Which | My read |
|---|---|---|
| A specific part of the label, or the label plus a detail | Canva, Stripe, Blue Enigma, Chargebee, Darwinbox, Lattice, CRED | what the rule says |
| The label is very broad | Vokal: "Knowledge Sharing / Q&A Platform" against "Technology, Information and Internet" | fits the rule, but inconsistent: Agsure's "AgriTech" failed against the same label |
| **Generous** | OpenAI: "Artificial Intelligence" against "Generative AI" | the answer is *broader* than the label; the judge's own reason has it backwards |

Atlassian's "Software" against "proprietary software company" was marked same under both rules, for the
same backwards reason. **Without those two, `industry` is 13/20 (0.65) and overall 97/119 (0.82).** The
verdicts were left as the judge gave them, not corrected by hand, so call it 0.82-0.83.

No negative controls were run: pairs that should obviously fail, to test the new rule on purpose.

## 6. Reading the 20 remaining misses

This sorting is a judgement call from the outputs alone, not a verified re-check of the facts.

| Group | Count | Which |
|---|---|---|
| **Clearly the agent** | 6 | Myntra funding (said `public`, label Series E); Whole Truth funding (`series_c`, label Series D); Blue Enigma founded year + funding (not found); Agsure founded year (not found); Postman industry (broader than the label) |
| **Label broader than the answer** | 4 | "Software Development" against Financial Services (Razorpay, Juspay) and AI (Hugging Face); "Technology, Information and Internet" against AgriTech (Agsure) |
| **Name format** | 2 | "Vokal" against "Vokal India"; "GalaxEye Space" against "GalaxEye" |
| **Sources may disagree** | 8 | employee counts for Postman, CRED, Hugging Face, Whole Truth, Agsure, Vokal; Canva founded 2012 vs 2013; Darwinbox HQ India vs United States |

## 7. What the real runs showed about the agent

- **The repair ladder earns its place.** 4 of 21 real runs needed a re-ask: one empty final answer
  (Razorpay), three records missing `name` (Chargebee, Whole Truth, Agsure). All 4 were rescued at
  rung 2. Rung 3 never fired by itself.
- **It never flags anything.** `flagged_fields` was empty on all 21 runs, including Blue Enigma and
  Agsure, where it simply left a field blank. The prompt asks it to flag, and the metric rewards it,
  but it doesn't happen. So the abstention side of the scoring has never been exercised.
- **It reads snippets, not pages.** 80 searches, 4 page fetches (3 Wikipedia, 1 company site) across
  21 runs. Possibly part of why employee counts are the weakest factual field; not proven.
- **Implicit caching does work on longer prompts:** 23.5% of input tokens read from cache, in 17 of 21
  runs. The 0% in `learn/P3_GEMINI.md` was short, fake-tool prompts.

## 8. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Change the judge's rule only by the owner's decision, and keep both scores | quietly re-judge, or never touch it | Moving the marking after seeing the mark is how evals start lying. Moving it openly, with the old mark beside it, is a correction |
| Change only the rule, not the model | also move the judge to a stronger model | One change at a time, or the two scores can't be compared |
| Don't correct the judge's verdicts by hand | fix OpenAI's flip myself | Then the number is partly my opinion. Report the verdicts and the doubts, and let the reader see the range |
| Fields in doubt are left out, not filled from memory | use what the assistant remembers | An answer key has to be the labeller's own |
| Re-score saved records | run everything again | Label changes need no model. The ledger keeps every record, so it cost 0 requests |
| `funding_stage: "unknown"` counts as no answer | an exact-match miss | "Unknown" about something unknowable is honest, not invented. Where an answer exists, it's still a miss |
| Floors a little under this run (0.78) | keep the placeholder 0.85 precision | A floor it can't meet would fail every build. These catch regressions; they don't claim a target. **The 0.85 is not met** |
| A fresh database | the test database | `lookup_existing` must find nothing from earlier runs, or the agent could copy its own past answers |

## 9. Crash walk

| Dies... | What's kept | Next step |
|---|---|---|
| a worker, mid-run | the ledger | another worker resumes the run; the eval waits |
| `run_eval` itself | every finished run in the database, but no `results.json` (it's written at the end) | score `runs.result` from the database; only repeat what's missing |
| daily quota runs out | the runs so far | the eval stops rather than recording failures as wrong answers, and `labels` is too low for the gate |

## 10. Not verified

- Whether the "sources may disagree" labels or the agent's answers are right.
- The clearer judge rule on companies it wasn't written after seeing, or on deliberate negative controls.
- Accuracy on Neon or Render. The model and tools are the same, but it wasn't measured there.
- The `low` thinking level with real tools.
- 20 companies is a small set, and most came from one person's search: treat these as a first
  reading, not a precise rate.

## 11. Questions to answer yourself

1. The score went from 0.76 to 0.83 without the agent changing. What changed, and how do you say that
   honestly?
2. Why quote 0.82-0.83 rather than just 0.83?
3. What's the difference between "wrong" and "abstained", and why did nothing abstain?
4. Why did the CRED rerun need its old company row deleted first?
5. Why do the floors sit under 0.85, the placeholder from before there was data?
6. A re-ask fired on 4 of 21 runs. What would have happened without the repair ladder?

<details>
<summary>Answers</summary>

1. Only the marking of one free-text field. The first rule failed answers that plainly fit, so the rule
   was made clearer and the same saved answers were re-judged. Say it was changed after seeing the score,
   give both numbers, and point to 0.85 on the factual fields, which no judge touches.
2. Two "same" verdicts look too generous: the answer was broader than the label, and the judge's reason
   had the rule backwards. They weren't corrected by hand, so the top number stands, but you know about them.
3. Wrong is a claim that doesn't match; abstained is a field the agent explicitly flagged. It never
   flagged, even when it had no answer, so blanks counted as wrong. That's a real prompt weakness.
4. `lookup_existing` could have returned the earlier record if the model looked up `cred.club`, and
   then it would be copying itself rather than doing the research being graded.
5. The 0.85 was a guess made before any data. The floor's job is catching regressions, so it sits
   under the first real measurement. Say plainly that 0.85 isn't met.
6. Those 4 runs would have ended `invalid_final_answer`: about one real run in five lost, though the
   research was done and already paid for.
</details>

## 12. Interview lines

- "I hand-labelled 20 companies and scored the agent field by field: 0.85 on the factual fields, and 0.83
  overall once the free-text industry field is included."
- "The first industry judge scored 0.30 because it rejected answers that plainly fit. I fixed the rule,
  but that's a change made after seeing a score, so I kept both numbers and checked the new verdicts.
  Two of them looked too generous, so I quote 0.82 to 0.83."
- "The eval found things the tests couldn't: 1 run in 5 needed the repair ladder, and the agent never
  used its flagging path, so I know what to improve next."
