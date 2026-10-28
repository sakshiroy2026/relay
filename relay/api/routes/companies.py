from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from relay.api import repo
from relay.api.principal import Principal
from relay.api.routes.runs import guarded

router = APIRouter(prefix="/v1", tags=["companies"])


@router.get("/companies/{domain}")
def get_company(domain: str, principal: Annotated[Principal, Depends(guarded)]) -> dict[str, Any]:
    """The saved company row for a domain (what save_company wrote), for this tenant."""
    row = repo.get_company(principal.tenant_id, domain)
    if row is None:
        raise HTTPException(status_code=404, detail="company not found")
    return row
