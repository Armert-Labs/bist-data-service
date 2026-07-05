"""Ortak bagimliliklar: API key, rate limit, bounded on-demand fetch."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .auth import registry
from .config import settings

logger = logging.getLogger("bist-canli-api.auth")


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
) -> None:
    """Kimlik dogrulama bagimliligi.

    - Anahtar tanimli degilse ve AUTH_REQUIRED=false -> acik (gelistirme kolayligi).
    - AUTH_REQUIRED=true ama anahtar yoksa -> 503 (fail-safe; kazara acik kalmaz).
    - Aksi halde X-API-Key gecerli olmalidir; gecersizse 401.
    """
    if not registry.enabled:
        if settings.auth_required:
            logger.error("AUTH_REQUIRED=true ancak hic API anahtari tanimli degil.")
            raise HTTPException(status_code=503, detail="Sunucu kimlik dogrulama icin yapilandirilmamis.")
        return  # auth kapali (yalnizca gelistirme icin)

    label = registry.verify(x_api_key)
    if label is None:
        # Hangi anahtarin denendigini loglama; yalnizca kaynak IP.
        logger.warning("Yetkisiz istek: %s %s (ip=%s)",
                       request.method, request.url.path, get_remote_address(request))
        raise HTTPException(status_code=401, detail="Gecersiz veya eksik API anahtari.")
    request.state.api_client = label


def _build_limiter() -> Limiter:
    storage_uri = settings.redis_url if settings.redis_enabled else "memory://"
    return Limiter(
        key_func=get_remote_address,
        storage_uri=storage_uri,
        default_limits=[settings.rate_limit] if settings.rate_limit_enabled else [],
    )


limiter = _build_limiter()

# On-demand (cache-miss) cekimleri sinirlar: thread-pool doygunlugu / DoS korumasi.
fetch_semaphore = asyncio.Semaphore(settings.max_concurrent_fetch)
