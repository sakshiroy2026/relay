# P5 — A stronger model as a last resort: repair rung 3 and model fallback

## 1. The picture

Relay develops on Claude Haiku 5.5, the cheapest current model (about $0.006
a run). Two situations call for something stronger, and both had a "deferred"
note since Day 8 and Day 9:

1. **Rung 3 of the repair ladder.** Haiku's final answer fails validation, and
   so does its corrected answer after the re-ask. Day 9 gave up there.
2. **The model is down.** Haiku is still overloaded after three retries. Day 8
   ended the run with `model_unavailable`; its Q7 said the real answer is a
   fallback provider.

Claude Sonnet 5.5 is about 20× Haiku's price, so the rule is strict: **Sonnet
only after Haiku's own chances are spent, only on a real provider, and only if
the run's remaining budget covers a pessimistic estimate of that one call,
checked *before* it is made.**

**The hospital analogy.** The ward doctor handles almost everything. When a
case defeats her twice, or she's unreachable, the senior consultant is called,
but only if the patient's care budget can pay the consultant's fee. And the
chart notes that the case was escalated, so the night shift knows who is in
charge.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/agent/loop.py` | `_judge` gains rung 3 (a `repair_requested` row with `escalate_to`, the next call uses that model); the model call falls back once per turn when the primary is still down after its retries; `can_afford()` guards both |
| `relay/core/replay.py` | `escalated_to` (from a `repair_requested` row): a resumed run stays on the stronger model; `last_prompt_tokens` for the estimate |
| `relay/llm/pricing.py` | `estimate(model, prompt_tokens)`: every prompt token priced as uncached, 2,000 output tokens: the pessimistic price of a call not made yet |
| `relay/config.py` | `escalation_model`, `fallback_model` (both `claude-sonnet-5-5`; `""` switches either off) |
| `scripts/check_run.py` | the "paid twice" check now allows the same reply again after a re-ask (a re-asked model may legitimately repeat itself), and includes a real model's raw blocks in the comparison |
| `tests/test_escalation.py` | 6 tests, plus 1 in `test_check_run.py` |

## 3. Runtime story: rung 3

| Call | Model | What happens | Ledger |
|---|---|---|---|
| 1 | Haiku | final record with `founded_year: 1599` | `model_call`, then `repair_requested` (rung 2) |
| 2 | Haiku | the corrected answer is still 1599 | `model_call`; `can_afford(Sonnet)`: spent $0.0012 + estimate $0.032 ≤ budget → `repair_requested {escalate_to: claude-sonnet-5-5}` |
| 3 | **Sonnet** | a valid record | `model_call` (priced at Sonnet rates), `final` tagged `repaired_by: ["model_reask", "escalated:claude-sonnet-5-5"]` |

If worker A dies waiting for call 3, worker B reads `escalate_to` from the
ledger and asks Sonnet, not Haiku. With a budget that can't cover the
estimate, call 3 never happens and the run ends `invalid_final_answer`.

**Fallback:** Haiku answers 529 three times → `can_afford(Sonnet)` → the same
turn is asked of Sonnet; the `model_call` row records `model:
claude-sonnet-5-5` and `fallback_from: claude-haiku-5-5`. The next turn tries
Haiku again: an outage is usually short, and staying on Sonnet would quietly
multiply the bill.

## 4. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| Check affordability **before** the call, with a pessimistic estimate | Rely on the per-turn budget check | That check runs only before a turn and can be overshot by one call. One Sonnet call on a 40k-token conversation (~$0.08) is bigger than a live run's whole $0.05 budget |
| Rung 3 only after rung 2 failed | Escalate on the first invalid answer | Most broken answers are fixed free (rung 1) or by one cheap re-ask (rung 2) |
| Escalation is sticky for the run (in the ledger) | Per call | Once Haiku has failed twice on this run, going back to it buys nothing; the ledger row also makes the choice survive a crash |
| Fallback is per turn, not sticky | Stay on Sonnet once switched | An outage is usually short; per turn keeps the expensive model the exception |
| Only on a real provider | Also for the fake | The fake is scripted; "a stronger fake model" would prove nothing |
| No new step kind: rung 3 is a `repair_requested` row with `escalate_to` | A new `escalated` kind (migration) | It *is* a re-ask, just answered by a different model; replay and check_run already understand it |
| Haiku's thinking blocks are sent to Sonnet unchanged | Strip them first | The API drops blocks the current model can't read, unbilled. Sending the history unchanged keeps one code path |

## 5. Crash walk

| Worker dies… | Ledger | Next worker |
|---|---|---|
| after the `escalate_to` row, before Sonnet answers | `repair_requested {escalate_to}` | replays it → `escalated_to` → asks Sonnet |
| after Sonnet's `model_call`, before `final` | the billed Sonnet reply | judges it (unjudged answer), no new call |
| mid-fallback call | nothing for that call | next turn starts with the primary again |

## 6. The proofs ($0)

`tests/test_escalation.py` (scripted provider, real loop, real ledger):
- answer → re-ask → escalation: models asked `[Haiku, Haiku, Sonnet]`, the second
  `repair_requested` names Sonnet, `final` tagged `["model_reask",
  "escalated:claude-sonnet-5-5"]`, the Sonnet call priced at Sonnet rates, ledger clean
- budget $0.03: Sonnet never called, `invalid_final_answer`
- worker killed while waiting for Sonnet → the resumed worker asks **only** Sonnet
- Haiku down: `[Haiku ×3, Sonnet]`, the row says `fallback_from: claude-haiku-5-5`
- budget $0.01: no fallback, `model_unavailable`; `FALLBACK_MODEL=""`: no fallback

180 tests pass. Not yet proven against the real API: P5's paid check
(~5 runs, ~$0.20) can force rung 3 with a domain the model can't settle, after
Sakshi's go.

## 7. Questions for Sakshi Roy

1. Why is affordability checked with a *pessimistic* estimate, and before the call?
2. Why is escalation sticky for a run, but the fallback isn't?
3. A worker dies right after writing the `escalate_to` row. Which model does the
   next worker ask, and how does it know?
4. Why does check_run now allow the same reply twice when a `repair_requested`
   sits between them?
5. Why does neither rule apply to the fake model?

<details>
<summary>Answers</summary>

1. The real cost is only known after the call returns, and one Sonnet call can
   exceed a live run's whole budget. Pricing every prompt token as uncached
   errs on the safe side: if even that fits, the real call (with cache hits) fits.
2. Two failures on this run say Haiku can't produce this record, so going back
   to it is wasted money. An outage says nothing about the model's ability and
   usually passes in seconds, so the next turn should try the cheap model again.
3. Sonnet. Replay sees the `repair_requested` row's `escalate_to` and sets
   `escalated_to`; the loop uses it for every remaining call.
4. A re-asked model can legitimately give the same answer again; that's a
   second paid call that really happened, not a replay bug. Without a re-ask
   between them, an identical reply still means "paid twice".
5. The fake is a script: escalating to "a stronger fake" or falling back to
   another fake would prove nothing and could mask real behaviour in the
   durability harnesses.
</details>

## 8. Interview lines

- "The cheap model does the work; a stronger model is a last resort for two cases,
  a final answer that survives a re-ask still invalid, or the primary staying down
  after retries, and it's only called if a pessimistic estimate of that one call
  fits the run's remaining budget."
- "Escalation is written to the ledger, so a worker that resumes a crashed run
  keeps using the stronger model; the fallback is per turn, because outages are
  short and staying on the expensive model would quietly multiply the bill."
