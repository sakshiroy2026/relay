# P5 on Gemini: rung 3, the fallback and the judge, on the free tier

Read `learn/P5_ESCALATION.md` first. The ladder itself didn't change. This stage
gives Gemini runs their own stronger model, and moves the eval judge off Anthropic.

## 1. The picture

P5 was built for Claude: Haiku does the work, Sonnet is the last resort. P7 moved
real runs to Gemini and switched P5 off for them, because Sonnet is a model a Gemini
client can't call. So a Gemini run had no rung 3 and no fallback.

```
Flash-Lite answers -> invalid -> re-ask Flash-Lite -> invalid -> 3.8 Flash (rung 3)
Flash-Lite down after 3 tries -> this one turn on 3.8 Flash -> next turn Flash-Lite again
```

**The hospital analogy, part two.** P5's senior consultant worked at a different
hospital, so when this hospital opened its own ward, nobody could page her. Now the
ward has a consultant of its own, and the pager refuses numbers from the other
hospital. The rules for calling one, and who pays, are the same.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/config.py` | `gemini_escalation_model`, `gemini_fallback_model` (both `gemini-3.8-flash`; `""` switches either off) |
| `relay/llm/factory.py` | `escalation_model(provider)` / `fallback_model(provider)` return that provider's model, or `""` if the configured id belongs to another family; `preflight()` prices them and warns about a cross-family id |
| `relay/agent/loop.py` | asks the factory instead of reading settings; heartbeats before a fallback |
| `relay/llm/gemini_client.py` | `generate()` is now public, so the judge shares its key handling and error mapping |
| `evals/judge.py` | Gemini instead of the Anthropic SDK; retries transient errors; `errors` says why a call gave no verdict |
| `evals/run_eval.py` | checks `GEMINI_API_KEY` and `TAVILY_API_KEY`; Flash-Lite judge by default; `judge_errors` in the results |
| `scripts/escalation_smoke.py` | the real proof: forces each failure, everything after it is real |
| `tests/test_escalation.py` | every P5 test now runs for Claude and for Gemini |

## 3. Runtime story

Run `6b9f9aee` (razorpay.com, fake tools, local Postgres):

| Calls | Model | What happens |
|---|---|---|
| 1-4 | Flash-Lite | the real work: search, fetch, save; the 4th reply is a final answer, which the harness swaps for one with `founded_year: 1599` |
| 5-6 | Flash-Lite | the re-ask (rung 2): it saves again, then answers; that answer is swapped the same way |
| 7-8 | 3.8 Flash | `repair_requested {escalate_to: gemini-3.8-flash}`; the real model continues from Flash-Lite's real signed history, saves, and returns a valid record. Call 7 took 3 tries |

The `final` row says `repaired_by: ["model_reask", "escalated:gemini-3.8-flash"]`.
Priced at paid rates, the run comes to $0.011489; the bill is $0.

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| One model pair per provider, cross-family ids dropped | one `ESCALATION_MODEL` that you must remember to change | Claude and Gemini paths can't reach each other's models, whatever a config says |
| Keep Flash-Lite's signed turns unchanged for 3.8 Flash | a fresh repair prompt with no history | The probe showed they're accepted, so there's one code path, and the stronger model sees the evidence |
| Flash-Lite judges by default | 3.8 Flash, as the handoff suggested | Same request: 1.2 s and a verdict vs a 503. Comparing two labels needs no stronger model |
| The judge goes through `generate()` | its own HTTP call, as first written | The first version silently dropped the old SDK's retries, and the probe showed why they matter |
| Heartbeat before a fallback | none | Timeouts count: 3 tries at 60 s each can outlast a 90 s lease. Renew it, or stop if it's already gone |
| Force the failures in the proof | wait for a domain the model can't settle | A valid answer is the normal result, so waiting proves nothing and spends quota. The results file names what was forced |

## 5. Crash walk

| Worker dies... | Ledger | Next worker |
|---|---|---|
| after the `escalate_to` row, before 3.8 Flash answers | `repair_requested {escalate_to}` | asks 3.8 Flash, not Flash-Lite (`a2d1fb52`) |
| after 3.8 Flash's `model_call`, before `final` | the priced reply | judges it, no new call |
| during a fallback call | nothing for that call | the next turn starts on Flash-Lite |

**Known limit.** The heartbeat runs before a turn and before a fallback, not between
retries. Three 60 s timeouts on one model can still outlast a 90 s lease. Another
worker then takes the run and fencing rejects the old worker's write: the ledger stays
correct, but those requests are wasted.

## 6. The proofs

**$0 and offline:** 269 tests pass. New or changed here: 7 P5 properties for both
providers, a full rung 3 through the real adapter with the request history checked
append-only, a Claude id configured for Gemini never being called, a lost lease
stopping the fallback, and the judge's requests, retries and errors.

**Real, free tier, 30 Oct 2026, local Postgres, 35 requests in total.**

Two probes (9 requests, no run ids, a few scratch requests against the real adapter):

| Question | Answer |
|---|---|
| Is 3.8 Flash callable on the free tier? | Yes |
| Does it accept Flash-Lite's signatures after a correction (rung 3)? | Yes, nothing stripped |
| ...and mid-turn (fallback)? | Yes, nothing stripped: 46.8 s. An earlier attempt hit the 60 s timeout |
| Does the judge's request format work? | Yes on Flash-Lite (1.2 s, right verdict). 3.8 Flash answered 503 "high demand" |

Then `python -m scripts.escalation_smoke --yes-spend` (26 requests, results in
`chaos/escalation_smoke_results.json`):

| Run | Forced | Result | Billed calls | Priced |
|---|---|---|---|---|
| `6b9f9aee` | Flash-Lite's final answers made invalid | rescued at rung 3, no ledger problems | 6 Lite + 2 Flash | $0.011489 |
| `a2d1fb52` | the same, plus the worker dies before the escalated call | second worker stayed on 3.8 Flash, no ledger problems | 6 Lite + 3 Flash | $0.014504 |
| `ae2dfc47` | Flash-Lite raises a retryable error for one turn | 3.8 Flash answered that turn, then back to Flash-Lite, no ledger problems | 3 Lite + 1 Flash | $0.004292 |

**3.8 Flash was unreliable today.** 5 of its 11 real requests in these runs failed and
were retried. The harness didn't record why (503s and timeouts are the two kinds seen
in the probes). The retries are what got these runs through.

**Not verified:**
- Rung 3 on a failure the model made by itself. These failures were forced.
- The fallback during a real Google outage.
- The "kill" here is an exception in-process, not `kill -9` of a worker process.
- Any of this on Render or Neon, or with real tools.
- Gemini's per-day limit for 3.8 Flash.

**After the merge:** a public real-AI run that fails validation twice can now call 3.8
Flash. It's free tier and budget-capped, but it uses the same Google quota.

## 7. Questions to answer yourself

1. Why does `escalation_model()` return `""` for a Claude id on a Gemini run, instead
   of raising an error?
2. Flash-Lite's reply has a signature. Why is it fine to send it to a different model,
   and how do we know?
3. Why does the harness keep the real `raw` parts when it swaps the answer text?
4. The judge returned no verdict and `calls` stayed at 0. What does that mean, and why
   is it not added to `cost_usd`?
5. Why is the judge Flash-Lite when the agent being judged is also Flash-Lite?
6. What can still go wrong with a 90 s lease and 60 s timeouts, and why doesn't it
   corrupt the run?

<details>
<summary>Answers</summary>

1. A bad setting shouldn't kill a run that was otherwise fine. The run keeps rungs 0-2
   and ends honestly if those fail, and `preflight()` warns at worker start.
2. It isn't guaranteed by anything we built. We asked: two real probes and three real
   runs were accepted with nothing stripped. If Google tightens this, the adapter's
   strip-and-retry-once is the recovery for a refusal it recognises.
3. Because the signature and the history have to be real for the test to mean anything.
   Only what the loop judges is swapped, so 3.8 Flash really receives Flash-Lite's turns.
4. No reply came back: the request failed, so nothing was metered. `errors` holds the
   reason, and the scorer falls back to its word-overlap heuristic and says so.
5. It never sees the web or the agent's reasoning, only two short labels, so it isn't
   marking its own homework. And it was the one that answered.
6. Retried timeouts can outlast the lease between heartbeats, so a second worker takes
   the run. Fencing rejects the old worker's writes, so the only cost is wasted requests.
</details>

## 8. Interview lines

- "Each provider has its own last-resort model, and the factory refuses ids from the
  other family, so a config mistake can't put a model on a path that can't call it."
- "I tested the risky assumption first: nine requests showed Gemini accepts one model's
  signed history on another, before I built on it. The same probes showed the stronger
  model was slower and less available, so the eval judge went to the cheap one."
- "The proof forces each failure and leaves everything after it real: three runs,
  including one where a second worker resumed on the stronger model from the ledger."
