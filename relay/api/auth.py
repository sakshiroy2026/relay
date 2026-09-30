"""API-key check for every /v1 route (the safety slice's simple version).

WHAT IT IS  One FastAPI dependency. A request to /v1/... must carry
            `Authorization: Bearer <key>` matching RELAY_API_KEY from .env.
GOES IN     The request's Authorization header.
COMES OUT   Nothing (the request continues), or an HTTPException:
            401 if the header is missing or the key is wrong,
            503 if the server has no key configured (it refuses rather than
            running open).
TOUCHES     Nothing: no database, no logging of the key.
FAILS WHEN  Never raises anything but the HTTPExceptions above.

Deliberately NOT the blueprint's full design (bcrypt hash + prefix lookup in
api_keys, one key per tenant): one key in .env is enough before a public URL.
"""

import hmac

from fastapi import Header, HTTPException

from relay.config import settings


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = settings.relay_api_key
    if not expected:
        raise HTTPException(status_code=503, detail="API key not configured on the server")

    scheme, _, key = (authorization or "").partition(" ")
    # compare_digest takes the same time wherever the strings differ: no timing hints
    if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
