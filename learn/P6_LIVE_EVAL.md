# P6 live: the first accuracy numbers

Read `learn/P6_EVAL.md` first: it explains the scorer and the gate. This note is what
happened when they met real companies, a real model and the real web.

## 1. The picture

Relay could already prove it survives crashes. It couldn't say whether its answers were right.
Now it can: 20 companies, hand-researched, graded field by field.

```
hand labels (golden.jsonl) ---------------+
                                          v
20 real runs -> records -> scorer + judge -> 87 / 116 correct -> results.json -> CI gate
```

**The exam analogy.** A mark depends on three things: the student, the answer key and the
marker. A low mark doesn't say which one is at fault. Most of this note is about separating
them: 0.75 is the mark, but only a few of the 29 misses are clearly the student's.

## 2. Each file's job

| File | One line |
|---|---|
| `evals/golden.jsonl` | the answer key: 20 companies, 116 scored fields, sources in `notes` |
| `evals/run_eval.py` | spaces live runs, stops at `--max-requests`, stops on a daily-quota error, counts only scored runs as `labels` |
| `evals/scoring.py` | `hq_country` may be a list (dual HQ); `funding_stage: "unknown"` counts as no answer |
| `evals/results.json` | every field's outcome and the summary; the CI gate reads it |
| `evals/thresholds.json` | floors set just under this run, to catch regressions |
| `tests/test_evals.py` | the two scoring rules above |
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
| `funding_stage` | 14/17 | 0.82 |
| `employee_range` | 14/20 | 0.70 |
| `industry` | 6/20 | 0.30 |
| **Overall** | **87/116** | **0.75** |
| **Without `industry`** | **81/96** | **0.84** |

Precision equals accuracy because the agent never abstained (section 6).

**Left out of scoring**, waiting on the owner: the funding stage of Atlassian, Stripe and Canva.

## 5. Reading the 29 misses

This sorting is a judgement call from the outputs alone, not a verified re-check of the facts.

| Group | Count | Which |
|---|---|---|
| **Clearly the agent** | 6 | Myntra funding (said `public`, label Series E); Whole Truth funding (`series_c`, label Series D); Blue Enigma founded year + funding (not found); Agsure founded year (not found); Postman industry (broader than the label) |
| **Judge looks too strict** | 4 | "Financial Services / Software" against "Financial Services" (Chargebee); "Artificial Intelligence / Hospitality Software" against "Artificial Intelligence" (Blue Enigma); Canva; Stripe |
| **Debatable wording** | 2 | "Artificial Intelligence" against "Generative AI" (OpenAI); "Financial Technology" against "Financial Services" (CRED) |
| **Label broader than the answer** | 7 | "Software Development" against Financial Services (Razorpay, Juspay), HR Software (Darwinbox, Lattice), AI (Hugging Face); "Technology, Information and Internet" against AgriTech (Agsure), Q&A Platform (Vokal) |
| **Name format** | 2 | "Vokal" against "Vokal India"; "GalaxEye Space" against "GalaxEye" |
| **Sources may disagree** | 8 | employee counts for Postman, CRED, Hugging Face, Whole Truth, Agsure, Vokal; Canva founded 2012 vs 2013; Darwinbox HQ India vs United States |

So `industry`'s 0.30 mostly measures the marking. **It's still the number in the results**: the
judge wasn't tuned after seeing this. Changing its rule is the owner's decision, and the results
must say so if it changes.

## 6. What the real runs showed about the agent

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

## 7. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Keep the judge's verdicts after seeing the score | re-judge with a kinder rule straight away | Tuning the marker after seeing the mark is how evals start lying. Needs the owner's decision, and disclosure |
| Fields in doubt are left out, not filled from memory | use what the assistant remembers | An answer key has to be the labeller's own |
| Re-score saved records | run everything again | Label changes need no model. The ledger keeps every record, so it cost 0 requests |
| `funding_stage: "unknown"` counts as no answer | an exact-match miss | "Unknown" about something unknowable is honest, not invented. Where an answer exists, it's still a miss |
| Floors just under this run | keep the placeholder 0.85 precision | A floor it can't meet would fail every build. These floors catch regressions; they don't claim a target. **The 0.85 is not met** |
| A fresh database | the test database | `lookup_existing` must find nothing from earlier runs, or the agent could copy its own past answers |

## 8. Crash walk

| Dies... | What's kept | Next step |
|---|---|---|
| a worker, mid-run | the ledger | another worker resumes the run; the eval waits |
| `run_eval` itself | every finished run in the database, but no `results.json` (it's written at the end) | score `runs.result` from the database; only repeat what's missing |
| daily quota runs out | the runs so far | the eval stops rather than recording failures as wrong answers, and `labels` is too low for the gate |

## 9. Not verified

- Whether the "sources may disagree" labels or the agent's answers are right.
- Accuracy on Neon or Render. The model and tools are the same, but it wasn't measured there.
- The `low` thinking level with real tools.
- 20 companies is a small set, and most came from one person's search: treat 0.75 as a first
  reading, not a precise rate.

## 10. Questions for Sakshi Roy

1. Why is `industry`'s 0.30 still in the results, when most of those misses look unfair?
2. What's the difference between "wrong" and "abstained", and why did nothing abstain?
3. Why did the CRED rerun need its old company row deleted first?
4. Why did the floors go down from the placeholder 0.85, and how do you say that honestly?
5. A re-ask fired on 4 of 21 runs. What would have happened without the repair ladder?

<details>
<summary>Answers</summary>

1. Because changing the marking scheme after seeing the marks is exactly what a skeptical reader
   worries about. You report the strict number, explain it, and change the rule only as a stated
   decision with both numbers shown.
2. Wrong is a claim that doesn't match; abstained is a field the agent explicitly flagged. It never
   flagged, even when it had no answer, so blanks counted as wrong. That's a real prompt weakness.
3. `lookup_existing` could have returned the earlier record if the model looked up `cred.club`, and
   then it would be copying itself rather than doing the research being graded.
4. The 0.85 was a guess made before any data. The floor's job is catching regressions, so it sits
   under the first real measurement. Say plainly that 0.85 isn't met.
5. Those 4 runs would have ended `invalid_final_answer`: about one real run in five lost, though the
   research was done and already paid for.
</details>

## 11. Interview lines

- "I hand-labelled 20 companies and scored the agent field by field: 0.75 overall, 0.84 without the
  free-text industry field, which mostly exposed a strict judge and my own broad labels."
- "I didn't tune the judge after seeing the score. I separated the misses into the agent's, the
  labels' and the marker's first, because a number you've tuned is a number nobody should trust."
- "The eval found things the tests couldn't: 1 run in 5 needed the repair ladder, and the agent
  never used its flagging path, so I know what to improve next."
