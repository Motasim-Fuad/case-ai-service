"""Service-to-service authentication (NestJS -> this service) and webhook signing."""
from __future__ import annotations

import hashlib
import hmac

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

api_key_header = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)


def require_api_key(
    key: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    if not key or not hmac.compare_digest(key.encode(), settings.internal_api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Api-Key")


def sign_body(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign_body(secret, body), signature)
