"""What one model call cost, in USD, computed from its token counts.

WHAT IT IS  The price list and the arithmetic. The ledger stores the result in
            every model_call row (cost_usd), and the per-run budget sums those
            rows, so this function is what turns tokens into the budget's truth.
GOES IN     A Response: its model and its token counts (uncached input, cache
            reads, cache writes, output — thinking is billed as output).
COMES OUT   The cost in USD, rounded to 6 decimals. estimate(): a pessimistic
            price for a call not made yet (P5: can this run afford Sonnet?).
TOUCHES     Nothing.
FAILS WHEN  The model has no price -> UnknownModelPrice. The factory checks the
            configured model at boot (assert_priced), so this can't first
            surface after a call was already paid for.

Real prices are Anthropic first-party API rates as published at build time
(per million tokens): Claude Haiku 5.5 $0.10 in / $0.50 out for prompts up to
100K tokens and $0.50 / $2.50 above that; Claude Sonnet 5.5 $2 / $10. Cache
reads cost 0.1x the input rate, 5-minute cache writes 1.25x. Re-check them on
the provider's pricing page before trusting a bill to the cent.
"""

from dataclasses import dataclass

from relay.llm.client import Response

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25  # the default 5-minute cache


@dataclass(frozen=True)
class Price:
    input: float  # USD per 1M input tokens
    output: float  # USD per 1M output tokens
    # (threshold in prompt tokens, input, output) for a long-prompt rate card
    long_prompt: tuple[int, float, float] | None = None


PRICES: dict[str, Price] = {
    # Fake prices are invented, in a realistic big-model : small-model ratio.
    "fake-planner": Price(3.00, 15.00),
    "fake-cheap": Price(1.00, 5.00),
    # Real models (P1). Haiku 5.5 is the development default.
    "claude-haiku-5-5": Price(0.10, 0.50, long_prompt=(100_000, 0.50, 2.50)),
    "claude-sonnet-5-5": Price(2.00, 10.00),
}


class UnknownModelPrice(Exception):
    """A model id with no entry in PRICES — its cost can't be computed."""


def assert_priced(model: str) -> None:
    """Fail at boot, before any money is spent, if a model has no price."""
    if model not in PRICES:
        raise UnknownModelPrice(f"no price for model {model!r}; add it to relay/llm/pricing.py")


def price(response: Response) -> float:
    try:
        p = PRICES[response.model]
    except KeyError as exc:
        raise UnknownModelPrice(f"no price for model {response.model!r}") from exc

    in_rate, out_rate = p.input, p.output
    prompt_tokens = response.input_tokens + response.cache_read_tokens + response.cache_write_tokens
    if p.long_prompt is not None and prompt_tokens > p.long_prompt[0]:
        _, in_rate, out_rate = p.long_prompt  # the whole request moves to the long card

    cost = (
        response.input_tokens * in_rate
        + response.cache_read_tokens * in_rate * CACHE_READ_MULTIPLIER
        + response.cache_write_tokens * in_rate * CACHE_WRITE_MULTIPLIER
        + response.output_tokens * out_rate
    ) / 1_000_000
    return round(cost, 6)


def estimate(model: str, prompt_tokens: int, output_tokens: int = 2000) -> float:
    """A pessimistic cost for a call not made yet: every prompt token uncached.

    Used to decide whether a run can afford an escalation or a fallback to a
    pricier model BEFORE making it (P5) — the budget check alone runs only
    before each turn and can be overshot by one call.
    """
    assert_priced(model)
    fake = Response(
        model=model,
        content="",
        stop_reason="end_turn",
        input_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )
    return price(fake)
