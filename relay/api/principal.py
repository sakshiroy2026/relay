"""Who is calling: full API keys with tenant identity (Day 10).

WHAT IT IS  The grown-up version of the safety slice's single env key. A key
            looks like `rly_<prefix>.<secret>`: the prefix finds the row in
            api_keys (so the lookup is indexed and the secret never touches
            the database), bcrypt verifies the secret against the stored hash,
            and the caller becomes a Principal — a tenant, not just "allowed".
            The old single env key still works and maps to the dev tenant, so
            the demo page and every existing script keep working.
GOES IN     The request's Authorization header.
COMES OUT   require_principal -> Principal(tenant_id, key_name, limits), or
            401 (missing/unknown/wrong/revoked key), 503 (no keys configured
            at all: the API refuses rather than running open).
TOUCHES     api_keys + tenants (one indexed SELECT per cache miss). A
            successful verification is cached in-process for CACHE_SECONDS,
            because bcrypt is deliberately slow (~200 ms) — far too slow per
            request. Revoking a key therefore takes up to CACHE_SECONDS to
            bite per API process; that trade-off is the cache's whole design.
FAILS WHEN  Database errors propagate (a down database must not turn into
            401s). Nothing about the key — not even its prefix — is logged.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from uuid import UUID

import bcrypt
from fastapi import Header, HTTPException
from psycopg.rows import dict_row

from relay.config import settings
from relay.db import pool

CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Every /v1 route scopes its SQL by tenant_id."""

    tenant_id: UUID
    key_name: str  # for operator logs ("dev key"), never the key itself
    max_concurrent_runs: int
    requests_per_minute: int


# sha256(full key) -> (expires_at, Principal). sha256 so a heap dump of the
# cache never contains a usable key.
_cache: dict[str, tuple[float, Principal]] = {}


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="missing or invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _lookup(key: str) -> Principal | None:
    """The database path: prefix lookup, bcrypt check, tenant limits."""
    prefix, _, secret = key.partition(".")
    if not prefix or not secret:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT k.key_hash, k.name, t.id AS tenant_id,
                       t.max_concurrent_runs, t.requests_per_minute
                FROM api_keys k
                JOIN tenants t ON t.id = k.tenant_id
                WHERE k.key_prefix = %s AND k.revoked_at IS NULL
                """,
                (prefix,),
            )
            row = cur.fetchone()
    if row is None or not bcrypt.checkpw(secret.encode(), row["key_hash"].encode()):
        return None
    return Principal(
        tenant_id=row["tenant_id"],
        key_name=row["name"],
        max_concurrent_runs=row["max_concurrent_runs"],
        requests_per_minute=row["requests_per_minute"],
    )


def _dev_principal() -> Principal:
    """The env key's identity: the dev tenant, with its limits read like any other."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT max_concurrent_runs, requests_per_minute FROM tenants WHERE id = %s",
                (settings.dev_tenant_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=503, detail="dev tenant not found; run scripts.seed")
    return Principal(
        tenant_id=UUID(settings.dev_tenant_id),
        key_name="env key",
        max_concurrent_runs=row["max_concurrent_runs"],
        requests_per_minute=row["requests_per_minute"],
    )


def require_principal(authorization: str | None = Header(default=None)) -> Principal:
    scheme, _, key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise _unauthorized()

    # The safety slice's env key still works (the demo page and scripts use it).
    if settings.relay_api_key and hmac.compare_digest(
        key.encode(), settings.relay_api_key.encode()
    ):
        return _dev_principal()

    digest = hashlib.sha256(key.encode()).hexdigest()
    cached = _cache.get(digest)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    principal = _lookup(key)
    if principal is None:
        # the safety slice's contract is kept: a server with NO keys at all
        # answers 503 (refuses to run open) rather than pretending to authenticate
        if not settings.relay_api_key:
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1"
                ).fetchone()
            if row is None:
                raise HTTPException(status_code=503, detail="no API keys configured")
        raise _unauthorized()
    _cache[digest] = (time.monotonic() + CACHE_SECONDS, principal)
    if len(_cache) > 10_000:  # a flood of distinct keys must not grow the cache forever
        _cache.clear()
    return principal
