from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row

from relay.api.auth import require_api_key
from relay.config import settings
from relay.db import pool

router = APIRouter(prefix="/v1", tags=["companies"], dependencies=[Depends(require_api_key)])


@router.get("/companies/{domain}")
def get_company(domain: str) -> dict[str, Any]:
    """The saved company row for a domain (what save_company wrote), for this tenant."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, domain, name, hq_country, founded_year, industry,
                       employee_range, funding_stage, description, sources, confidence,
                       created_by_run, created_at, updated_at
                FROM companies
                WHERE domain = %s AND tenant_id = %s
                """,
                (domain, settings.dev_tenant_id),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="company not found")
    return row
