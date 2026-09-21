from relay.llm.client import Response

# USD per 1,000,000 tokens: (input, output).
# Fake prices are invented, in a realistic big-model : small-model ratio.
# Real model prices are added — and checked against the provider's page — at demo time.
PRICES: dict[str, tuple[float, float]] = {
    "fake-planner": (3.00, 15.00),
    "fake-cheap": (1.00, 5.00),
}


class UnknownModelPrice(Exception):
    """A model id with no entry in PRICES — its cost can't be computed."""


def price(response: Response) -> float:
    try:
        in_rate, out_rate = PRICES[response.model]
    except KeyError as exc:
        raise UnknownModelPrice(f"no price for model {response.model!r}") from exc

    cost = (
        response.input_tokens * in_rate / 1_000_000 + response.output_tokens * out_rate / 1_000_000
    )
    return round(cost, 6)
