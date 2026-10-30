# P3 on Gemini: what a run costs, and whether thinking less is worth it

## 1. The picture

A run's bill has two knobs: how much of the prompt is served from cache, and how much
the model thinks before it answers (thinking is billed as output, the expensive side).
P3 was built for Claude as settings. This stage measures both on Gemini.

**The analogy.** Asking a contractor for a quote: you can have them reread the whole
file every visit or keep notes (the cache), and you can pay for an hour of pondering or
ten minutes (the thinking level). Ten minutes is cheaper, but the bill alone can't tell
you whether the work got worse.

## 2. Each file's job

| File | One line |
|---|---|
| `relay/config.py` | `gemini_thinking_level`: `""` (default, sends nothing), `low`, `medium`, `high` |
| `relay/llm/gemini_client.py` | `generation_config(model)`: sends `thinkingLevel` to 3.x models only, and only when set |
| `scripts/live_smoke.py` | `--mode public_real` (real model, fake tools, no Tavily credits), `--gap-seconds` (per-minute limit) |
| `chaos/p3_gemini_results.json` | the numbers below |

## 3. Runtime story

`GEMINI_THINKING_LEVEL=low` in a worker's environment adds
`thinkingConfig: {thinkingLevel: "low"}` to each request. With it unset, the request
bytes are what P7 sent. It must stay constant for a run's life, like the system prompt.

## 4. The numbers

30 Oct 2026, `gemini-3.5-flash-lite`, free tier ($0 billed; costs at the paid rate), fake
tools, local Postgres, the same 5 domains in the same order, 35 requests in total.

| Level | Succeeded | Model calls | Input tokens | Output tokens | Priced, 5 runs | Per run |
|---|---|---|---|---|---|---|
| `low` | 5/5 | 17 | 30,681 | 1,965 | $0.014117 | $0.0028 |
| `medium` | 5/5 | 18 | 37,952 | 5,688 | $0.025608 | $0.0051 |

Runs: low `06a82930 79ceaa40 456eec26 73820586 2c8df406`; medium `194afcf2 34dcae53
bd506829 76ac7f7e 5cc00f1e`. All ledgers clean.

**Low costs about 45% less**, nearly all of it output tokens (about a third of medium's).

**Caching: zero.** Gemini reported `cachedContentTokenCount` 0 on all 35 calls, so
`cache_read_share` is 0%. These prompts are short (about 2k tokens) and the runs were
spaced 45 s apart. We don't know which of those is the reason, if either. Don't claim
any saving from implicit caching on Relay.

## 5. Design decisions

| Decision | Alternative | Why this one |
|---|---|---|
| The default stays `""` | switch to `low` now | Fake tools measure cost, not accuracy. Five successes only say the record passed validation |
| A separate setting | reuse `LLM_EFFORT` | That defaults to `medium`, which would silently change every Gemini request |
| Measure with fake tools | `live` mode | Real tools would spend Tavily credits and add noise from the web |
| 3.x models only | send it to every Gemini model | Other generations spell it differently |

## 6. The crash walk

Nothing new: the setting adds no state, and a resumed run rebuilds the same conversation.
Don't change the level on workers while runs are in flight: nobody has tested a run whose
turns used different levels.

## 7. Not verified

- Whether `low` hurts accuracy. **Decide this from the eval, with both levels on the same
  labels, once the labels exist.**
- Whether `""` behaves like `medium`; it wasn't measured here.
- Costs with real tools, where fetched pages make prompts much longer. Caching may behave
  differently there too.
- Five runs a level is a small sample: enough to show the size of the gap, not a precise rate.

## 8. Questions for Sakshi Roy

1. Why didn't the cheaper setting become the default?
2. Why is the saving mostly in output tokens?
3. What does `cache_read_share` 0% let you claim in an interview?
4. Why is `GEMINI_THINKING_LEVEL` separate from `LLM_EFFORT`?

<details>
<summary>Answers</summary>

1. The measurement says what it costs, not whether the answers got worse. With fake tools
   the record comes from canned pages, so these runs can't show that. The eval decides.
2. Thinking is billed as output, and low cut it to about a third. Output is also the
   pricier side, at $2.50 against $0.30 per million tokens.
3. Only that it was measured and was zero on these short prompts. No caching savings.
4. `LLM_EFFORT` defaults to `medium` for Claude. Reusing it would change every Gemini
   request on deploy, when the point was for this to be opt-in.
</details>

## 9. Interview lines

- "I measured thinking levels side by side on the same domains: low cost about 45% less.
  I didn't ship it, because those runs couldn't measure accuracy, and the eval can."
- "I expected implicit caching to help and measured zero on 35 calls, so the project
  doesn't claim it."
