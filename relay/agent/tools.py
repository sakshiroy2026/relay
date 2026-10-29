"""The agent's toolbox: five tools, a registry, and one dispatcher.

WHAT IT IS  Everything the model is allowed to do. External tools (web_search,
            fetch_page) are fake by default; TOOLS_PROVIDER=real swaps in Tavily
            search and the pinned-IP fetcher (relay/agent/real_tools.py) under
            the same names and schemas. Internal tools (lookup_existing,
            save_company, flag_for_review) touch Relay's own database.
GOES IN     dispatch_tool(name, args, ctx): the tool the model asked for, its raw
            args, and a ToolContext (which run, its domain, this call's
            tool_call index and idempotency key). Read tools ignore ctx.
COMES OUT   ToolResult(ok, content): content is exactly what the model reads.
TOUCHES     save_company: companies + tool_invocations (via run_once).
            lookup_existing: reads companies. The rest: nothing.
FAILS WHEN  Model mistakes (unknown tool, bad args, 404) -> ok=False result, never
            raised. An internal tool called without ctx, or a database error,
            is a bug or an outage -> raised, so it crashes loudly.
"""

import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, HttpUrl, ValidationError

from relay.agent.errors import ToolError  # re-exported: callers import it from here
from relay.agent.schemas import CompanyRecord, PageText, SearchResult
from relay.agent.ssrf import UnsafeURL, check_url
from relay.config import settings
from relay.core import breaker
from relay.core.idempotency import run_once
from relay.core.retry import RetryableError, call_with_retries
from relay.db import pool

TOOL_RETRY_ATTEMPTS = 3  # tries per tool call; sleeps at most ~3 s in total (see retry.py)


@dataclass(frozen=True)
class ToolContext:
    """Which run a tool call belongs to. Built by the loop, never by the model."""

    run_id: UUID
    domain: str  # the run's own domain (runs.input), not one the model names
    step_index: int  # index of this call's tool_call row
    idem_key: str  # '<run_id>:<tool_call_id>': the same on every re-run of this call
    tools_provider: str = ""  # "fake" / "real" for this run (its mode); "" = the process default


# ── What the model must send for each tool ─────────────────────────────
class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class FetchPageInput(BaseModel):
    url: HttpUrl


class LookupExistingInput(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


class FlagForReviewInput(BaseModel):
    field: str = Field(min_length=1, max_length=50)
    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=10)


# ── Fault injection (Day 8 proofs only; both knobs default off) ─────────
def _maybe_fail(tool_name: str) -> None:
    """Make a fake external tool flaky or down, like the real internet is."""
    if settings.fake_tool_down == tool_name:
        raise RetryableError(f"{tool_name}: injected outage (503 service unavailable)")
    if settings.fake_tool_failure_rate > 0 and random.random() < settings.fake_tool_failure_rate:
        raise RetryableError(f"{tool_name}: injected transient failure (503)")


# ── The pretend internet (tools_provider = "fake") ──────────────────────
_FAKE_SEARCH: dict[str, list[SearchResult]] = {
    "acme payments": [
        SearchResult(
            title="Acme Payments | About us",
            url=HttpUrl("https://acmepay.example/about"),
            snippet="Acme Payments builds payment infrastructure for small online businesses.",
        ),
        SearchResult(
            title="Acme Payments raises Series B",
            url=HttpUrl("https://news.example/acme-series-b"),
            snippet="The Bengaluru fintech has closed a Series B round.",
        ),
    ],
}

_FAKE_PAGES: dict[str, PageText] = {
    "https://acmepay.example/about": PageText(
        url=HttpUrl("https://acmepay.example/about"),
        title="About Acme Payments",
        text=(
            "Acme Payments was founded in 2016 and is headquartered in Bengaluru, India. "
            "It builds payment infrastructure for small online businesses. "
            "The company employs around 350 people."
        ),
    ),
    "https://news.example/acme-series-b": PageText(
        url=HttpUrl("https://news.example/acme-series-b"),
        title="Acme Payments raises Series B",
        text=(
            "Acme Payments, a Bengaluru-based fintech founded in 2016, "
            "has closed a Series B funding round."
        ),
    ),
}


def _fake_web_search(args: WebSearchInput, _ctx: ToolContext | None) -> str:
    _maybe_fail("web_search")
    results = _FAKE_SEARCH.get(args.query.strip().lower())
    if results is None:
        # An empty list reads like "the web has nothing", so a real model
        # rephrases and searches again, and again — five wasted turns of a
        # free-tier quota (measured, P7). Say what is actually true instead:
        # this index is canned. The model then moves on in one turn.
        raise ToolError(
            f"no canned result for {args.query.strip()!r}: this demo's search index holds "
            "a few fixed queries, not the live web. Rephrasing will not help — continue "
            "from what you already know and use flag_for_review for anything you "
            "cannot source."
        )
    return json.dumps([r.model_dump(mode="json") for r in results])


def _fake_fetch_page(args: FetchPageInput, _ctx: ToolContext | None) -> str:
    _maybe_fail("fetch_page")
    try:
        # fake mode: .example hosts don't resolve, so only the checks that need no DNS;
        # the real fetcher calls check_url with DNS, and next_hop on every redirect
        check_url(str(args.url), resolve=None)
    except UnsafeURL as exc:
        raise ToolError(str(exc)) from exc
    page = _FAKE_PAGES.get(str(args.url))
    if page is None:
        raise ToolError(f"could not fetch {args.url}: 404 not found")
    return page.model_dump_json()


# ── Internal tools (Relay's own database) ───────────────────────────────
def _need(ctx: ToolContext | None) -> ToolContext:
    if ctx is None:
        raise RuntimeError("internal tools need a ToolContext (called outside the loop?)")
    return ctx


def _lookup_existing(args: LookupExistingInput, ctx: ToolContext | None) -> str:
    run_id = _need(ctx).run_id
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, hq_country, founded_year, industry, employee_range,
                       funding_stage, description, sources
                FROM companies
                WHERE domain = %s
                  AND tenant_id = (SELECT tenant_id FROM runs WHERE id = %s)
                """,
                (args.domain, run_id),
            )
            row = cur.fetchone()
    if row is None:
        return json.dumps({"found": False, "domain": args.domain})
    keys = ["name", "hq_country", "founded_year", "industry", "employee_range"]
    keys += ["funding_stage", "description", "sources"]
    return json.dumps({"found": True, "domain": args.domain, **dict(zip(keys, row, strict=True))})


_SAVE_SQL = """
INSERT INTO companies (tenant_id, domain, name, hq_country, founded_year, industry,
                       employee_range, funding_stage, description, sources, confidence,
                       created_by_run)
SELECT tenant_id, %(domain)s, %(name)s, %(hq_country)s, %(founded_year)s, %(industry)s,
       %(employee_range)s, %(funding_stage)s, %(description)s, %(sources)s, %(confidence)s, id
FROM runs WHERE id = %(run_id)s
ON CONFLICT (tenant_id, domain) DO UPDATE SET
    name           = EXCLUDED.name,
    hq_country     = EXCLUDED.hq_country,
    founded_year   = EXCLUDED.founded_year,
    industry       = EXCLUDED.industry,
    employee_range = EXCLUDED.employee_range,
    funding_stage  = EXCLUDED.funding_stage,
    description    = EXCLUDED.description,
    sources        = EXCLUDED.sources,
    confidence     = EXCLUDED.confidence,
    updated_at     = now()
RETURNING id, (xmax = 0) AS created
"""


def _save_company(args: CompanyRecord, ctx: ToolContext | None) -> str:
    c = _need(ctx)
    record = args.model_dump(mode="json")

    def execute(cur: psycopg.Cursor[Any]) -> dict[str, Any]:
        cur.execute(
            _SAVE_SQL,
            {
                **record,
                "domain": c.domain,  # the run's domain, whatever the model called it
                "sources": Jsonb(record["sources"]),
                "confidence": Jsonb(record["confidence"]),
                "run_id": c.run_id,
            },
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"run {c.run_id} not found while saving its company")
        return {"company_id": str(row[0]), "domain": c.domain, "created": bool(row[1])}

    return json.dumps(run_once(c.idem_key, c.run_id, c.step_index, "save_company", execute))


def _flag_for_review(args: FlagForReviewInput, _ctx: ToolContext | None) -> str:
    # Simplest version: the note lives in the tool_result row. No approvals table,
    # and the run is not parked (awaiting_approval is out of scope for now).
    return json.dumps(
        {
            "flagged_field": args.field,
            "question": args.question,
            "options": args.options,
            "status": "noted for human review; keep the field in flagged_fields",
        }
    )


# ── The toolbox ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[Any, ToolContext | None], str]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str  # exactly what the model will read
    attempts: int = 1  # how many tries this call took (Day 8)
    breaker_open: bool = False  # True = never ran: the tool's circuit was open


_FAKE_TOOLS = (
    Tool(
        name="web_search",
        description="Search the web. Returns a JSON list of {title, url, snippet}.",
        input_model=WebSearchInput,
        run=_fake_web_search,
    ),
    Tool(
        name="fetch_page",
        description="Fetch one web page by URL. Returns JSON {url, title, text}.",
        input_model=FetchPageInput,
        run=_fake_fetch_page,
    ),
)

_INTERNAL_TOOLS = (
    Tool(
        name="lookup_existing",
        description="Check whether a company record already exists for a domain.",
        input_model=LookupExistingInput,
        run=_lookup_existing,
    ),
    Tool(
        name="save_company",
        description=(
            "Save the finished company record for this run's domain. "
            "Safe to retry: the same call never saves twice."
        ),
        input_model=CompanyRecord,
        run=_save_company,
    ),
    Tool(
        name="flag_for_review",
        description="Ask a human to review one field you could not settle from sources.",
        input_model=FlagForReviewInput,
        run=_flag_for_review,
    ),
)


# ── The real internet (tools_provider = "real", P2) ─────────────────────
def _real_web_search(args: WebSearchInput, _ctx: ToolContext | None) -> str:
    from relay.agent import real_tools

    if not settings.tavily_api_key:  # a live run on a worker without the key
        raise ToolError("web_search is not configured on this worker; continue without it")
    results = real_tools.tavily_search(args.query.strip(), settings.tavily_api_key)
    return json.dumps([r.model_dump(mode="json") for r in results])


def _real_fetch_page(args: FetchPageInput, _ctx: ToolContext | None) -> str:
    from relay.agent import real_tools

    try:
        page = real_tools.fetch_page(str(args.url), settings.page_text_max_chars)
    except UnsafeURL as exc:
        raise ToolError(str(exc)) from exc
    return page.model_dump_json()


# Same names, descriptions and input models as the fake pair: the model sees an
# identical menu whichever provider is configured.
_REAL_RUNS: dict[str, Callable[[Any, ToolContext | None], str]] = {
    "web_search": _real_web_search,
    "fetch_page": _real_fetch_page,
}
_REAL_TOOLS = tuple(
    Tool(name=t.name, description=t.description, input_model=t.input_model, run=_REAL_RUNS[t.name])
    for t in _FAKE_TOOLS
)


def _build_registry(provider: str | None = None, strict: bool = True) -> dict[str, Tool]:
    provider = provider or settings.tools_provider
    if provider == "fake":
        return {tool.name: tool for tool in (*_FAKE_TOOLS, *_INTERNAL_TOOLS)}
    if provider == "real":
        if strict and not settings.tavily_api_key:  # the process default: fail at boot
            raise RuntimeError("TOOLS_PROVIDER=real needs TAVILY_API_KEY in the environment")
        return {tool.name: tool for tool in (*_REAL_TOOLS, *_INTERNAL_TOOLS)}
    raise RuntimeError(f"no tools for provider {provider!r}")


# The process default registry; a run whose mode picks the other provider gets
# its registry from registry_for (built once, on first use).
REGISTRY: dict[str, Tool] = _build_registry()
_OTHER_REGISTRIES: dict[str, dict[str, Tool]] = {}


def registry_for(provider: str) -> dict[str, Tool]:
    if not provider or provider == settings.tools_provider:
        return REGISTRY
    if provider not in _OTHER_REGISTRIES:
        # not strict: a missing Tavily key makes web_search answer ok=False, not crash
        _OTHER_REGISTRIES[provider] = _build_registry(provider, strict=False)
    return _OTHER_REGISTRIES[provider]


def breaker_key(name: str, provider: str) -> str:
    """Fake and real tools never share a breaker row: a fake success must not close a
    real outage. The fake keeps the bare name (Day 8's rows and proofs)."""
    return name if provider in ("", "fake") else f"{name}@{provider}"


# The menu shown to the model: generated from the same Pydantic models that validate the args.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_model.model_json_schema(),
    }
    for tool in REGISTRY.values()
]


def dispatch_tool(name: str, args: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
    provider = (ctx.tools_provider if ctx is not None else "") or settings.tools_provider
    registry = registry_for(provider)
    tool = registry.get(name)
    if tool is None:
        return ToolResult(
            ok=False, content=f"unknown tool {name!r}; choose from {sorted(registry)}"
        )

    try:
        validated = tool.input_model.model_validate(args)
    except ValidationError as exc:
        return ToolResult(ok=False, content=f"invalid arguments for {name}: {exc}")

    # ── Day 8: circuit gate, then run with retries ────────────────────────
    # The breaker is checked once per dispatch, not per retry: if the tool is
    # known-down we don't even make the first attempt. The model is told and
    # asked to adapt instead of the run crashing (blueprint §9).
    bkey = breaker_key(name, provider)
    if not breaker.allow(bkey):
        return ToolResult(
            ok=False,
            content=(
                f"{name} is temporarily unavailable (circuit open); "
                "continue with the evidence you already have, or use flag_for_review"
            ),
            breaker_open=True,
        )

    try:
        content, attempts = call_with_retries(
            lambda: tool.run(validated, ctx),
            attempts=TOOL_RETRY_ATTEMPTS,
            on_failure=lambda _exc: breaker.record_failure(bkey),
        )
    except ToolError as exc:
        # A model mistake (404, bad URL): the tool itself is healthy, so the
        # breaker is not touched and nothing is retried.
        return ToolResult(ok=False, content=str(exc))
    except RetryableError as exc:
        # Every attempt failed; each one was already counted by the breaker.
        return ToolResult(
            ok=False,
            content=f"{name} failed after {TOOL_RETRY_ATTEMPTS} attempts: {exc}",
            attempts=TOOL_RETRY_ATTEMPTS,
        )

    breaker.record_success(bkey)
    return ToolResult(ok=True, content=content, attempts=attempts)
