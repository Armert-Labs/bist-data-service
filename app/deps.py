"""Ortak bagimliliklar: API key, rate limit, bounded on-demand fetch."""

from __future__ import annotations

import asyncio
import logging

from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .auth import registry
from .config import settings

logger = logging.getLogger("bist-canli-api.auth")


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    """Kimlik dogrulama bagimliligi.

    - Anahtar tanimli degilse ve AUTH_REQUIRED=false -> acik (gelistirme kolayligi).
    - AUTH_REQUIRED=true ama anahtar yoksa -> 503 (fail-safe; kazara acik kalmaz).
    - Aksi halde X-API-Key gecerli olmalidir; gecersizse 401.
    """
    if not registry.enabled:
        if settings.auth_required:
            logger.error("AUTH_REQUIRED=true ancak hic API anahtari tanimli degil.")
            raise HTTPException(
                status_code=503, detail="Sunucu kimlik dogrulama icin yapilandirilmamis."
            )
        return

    label = registry.verify(x_api_key)
    if label is None:
        logger.warning(
            "Yetkisiz istek: %s %s (ip=%s)",
            request.method,
            request.url.path,
            get_remote_address(request),
        )
        raise HTTPException(status_code=401, detail="Gecersiz veya eksik API anahtari.")
    request.state.api_client = label


def _rate_limit_key(request: Request) -> str:
    """API key etiketi varsa anahtar bazli, yoksa IP bazli limit."""
    presented = request.headers.get("X-API-Key")
    if presented:
        label = registry.verify(presented)
        if label:
            return f"key:{label}"
    return get_remote_address(request)


def _build_limiter() -> Limiter:
    storage_uri = settings.redis_url if settings.redis_enabled else "memory://"
    return Limiter(
        key_func=_rate_limit_key,
        storage_uri=storage_uri,
        default_limits=[settings.rate_limit] if settings.rate_limit_enabled else [],
    )


limiter = _build_limiter()

fetch_semaphore = asyncio.Semaphore(settings.max_concurrent_fetch)
