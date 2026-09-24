import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, ValidationError

from relay.agent.schemas import PageText, SearchResult
from relay.config import settings


class ToolError(Exception):
    """An expected tool failure (page not found, site down). Shown to the model, not a crash."""


# ── What the model must send for each tool ─────────────────────────────
class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class FetchPageInput(BaseModel):
    url: HttpUrl


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


def _fake_web_search(args: WebSearchInput) -> str:
    results = _FAKE_SEARCH.get(args.query.strip().lower(), [])
    return json.dumps([r.model_dump(mode="json") for r in results])


def _fake_fetch_page(args: FetchPageInput) -> str:
    page = _FAKE_PAGES.get(str(args.url))
    if page is None:
        raise ToolError(f"could not fetch {args.url}: 404 not found")
    return page.model_dump_json()


# ── The toolbox ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[Any], str]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str  # exactly what the model will read


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


def _build_registry() -> dict[str, Tool]:
    if settings.tools_provider == "fake":
        return {tool.name: tool for tool in _FAKE_TOOLS}
    raise RuntimeError(f"no tools for provider {settings.tools_provider!r}")


REGISTRY: dict[str, Tool] = _build_registry()

# The menu shown to the model: generated from the same Pydantic models that validate the args.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_model.model_json_schema(),
    }
    for tool in REGISTRY.values()
]


def dispatch_tool(name: str, args: dict[str, Any]) -> ToolResult:
    tool = REGISTRY.get(name)
    if tool is None:
        return ToolResult(
            ok=False, content=f"unknown tool {name!r}; choose from {sorted(REGISTRY)}"
        )

    try:
        validated = tool.input_model.model_validate(args)
    except ValidationError as exc:
        return ToolResult(ok=False, content=f"invalid arguments for {name}: {exc}")

    try:
        return ToolResult(ok=True, content=tool.run(validated))
    except ToolError as exc:
        return ToolResult(ok=False, content=str(exc))
