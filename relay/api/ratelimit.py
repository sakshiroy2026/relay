"""Per-tenant request rate limiting: a fixed window of one minute, in-process.

WHAT IT IS  A counter per (tenant, current minute). The tenant's
            requests_per_minute (a tenants column, Day 10) is the ceiling;
            request N+1 inside the window gets 429 with Retry-After.
GOES IN     check(principal) from the routes' dependency chain; a time source
            is injectable for the tests.
COMES OUT   Nothing, or HTTPException 429.
TOUCHES     Process memory only — deliberately. One API process is the
            deployment today, so shared state would be Redis for nobody.
            With N API processes the effective limit becomes up to N× the
            configured one; the honest fix at that point is a shared counter,
            and the blueprint's answer (Redis) is noted in learn/DAY10.md.
            The per-tenant CONCURRENCY cap (repo.create_run) is the one that
            guards money, and that one lives in Postgres, shared by everyone.
FAILS WHEN  Never, except the 429.
"""

import time
from collections.abc import Callable

from fastapi import HTTPException

from relay.api.principal import Principal

# (tenant_id hex, minute number) -> requests seen in that minute
_windows: dict[tuple[str, int], int] = {}


def check(principal: Principal, now: Callable[[], float] = time.time) -> None:
    minute = int(now() // 60)
    key = (principal.tenant_id.hex, minute)
    used = _windows.get(key, 0)
    if used >= principal.requests_per_minute:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit: {principal.requests_per_minute} requests per minute",
            headers={"Retry-After": str(int(60 - now() % 60) or 1)},
        )
    _windows[key] = used + 1

    if len(_windows) > 4096:  # drop finished windows; bounded however many tenants exist
        _windows_copy = {k: v for k, v in _windows.items() if k[1] >= minute}
        _windows.clear()
        _windows.update(_windows_copy)
