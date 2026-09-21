"""Day 4 smoke test: raw text in -> CompanyRecord out, via the fake model."""

import json

from pydantic import ValidationError

from relay.agent.schemas import CompanyRecord
from relay.llm.client import LLMClient
from relay.llm.fake import FakeLLMClient, ScriptedReply
from relay.llm.pricing import price

PAGE_TEXT = """
Acme Payments is a payments company headquartered in India, founded in 2016.
It has around 800 employees and raised a Series B round last year.
It builds APIs that let online businesses accept card and UPI payments.
"""

SYSTEM = "Extract a CompanyRecord as JSON from the page text. Reply with JSON only."

GOOD = {
    "name": "Acme Payments",
    "hq_country": "India",
    "founded_year": 2016,
    "industry": "Fintech / Payments",
    "employee_range": "201-1000",
    "funding_stage": "series_b",
    "description": "Payment APIs for online businesses (cards, UPI).",
    "sources": ["https://acmepay.example/about"],
    "confidence": {"founded_year": 0.9, "employee_range": 0.6},
}
BAD = {**GOOD, "founded_year": 2030}


def extract(llm: LLMClient) -> None:
    messages = [{"role": "user", "content": f"<page>\n{PAGE_TEXT}\n</page>"}]
    response = llm.call(model="fake-planner", system=SYSTEM, messages=messages, tools=[])
    print(
        f"  tokens in/out: {response.input_tokens}/{response.output_tokens}"
        f"  cost: ${price(response):.6f}"
    )

    try:
        record = CompanyRecord.model_validate(json.loads(response.content))
    except ValidationError as exc:
        print(f"  REJECTED - {exc.error_count()} error(s):")
        for err in exc.errors():
            print(f"    {err['loc']}: {err['msg']} (got {err['input']!r})")
        return

    print("  ACCEPTED:")
    print(json.dumps(record.model_dump(mode="json"), indent=2))


def main() -> None:
    print("1) good reply")
    extract(FakeLLMClient([ScriptedReply(content=json.dumps(GOOD))]))
    print("2) bad reply (founded_year 2030)")
    extract(FakeLLMClient([ScriptedReply(content=json.dumps(BAD))]))


if __name__ == "__main__":
    main()
