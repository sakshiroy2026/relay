from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

EmployeeRange = Literal["1-10", "11-50", "51-200", "201-1000", "1001-5000", "5000+"]
FundingStage = Literal[
    "bootstrapped", "seed", "series_a", "series_b", "series_c", "series_d+", "public", "unknown"
]


class CompanyRecord(BaseModel):
    """The agent's final answer. Every field the model fills must pass these rules."""

    name: str = Field(min_length=1, max_length=200)
    hq_country: str | None = None
    founded_year: int | None = Field(default=None, ge=1600, le=2026)
    industry: str | None = None
    employee_range: EmployeeRange | None = None
    funding_stage: FundingStage | None = None
    description: str = Field(max_length=600)
    sources: list[HttpUrl] = Field(min_length=1)
    confidence: dict[str, float]
    flagged_fields: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    """One hit from web_search."""

    title: str
    url: HttpUrl
    snippet: str


class PageText(BaseModel):
    """Readable text of one fetched page, after nav/ads are stripped."""

    url: HttpUrl
    title: str | None = None
    text: str
