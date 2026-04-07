"""
API Key authentication.
Set INFRA_API_KEY in your environment (or .env file).
Discord bot passes it as:  X-API-Key: <key>
"""

from typing import Optional
from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from app.config import INFRA_API_KEY

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Security(API_KEY_HEADER)) -> str:
    if api_key is None or api_key != INFRA_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
    return api_key
