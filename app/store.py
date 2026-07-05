"""Veri deposu soyutlamasi: onbellek + pub/sub + bayatlik + persistence.

Iki uygulama:
- MemoryStore : tek-process, harici bagimlilik yok (REDIS_URL bos ise).
- RedisStore  : paylasimli durum + gercek pub/sub (cok worker / ayri updater).

Ikisi de ayni async arayuzu uygular; kod store tipinden habersizdir.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from .config import settings
from .models import Quote

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def set_quotes(self, quotes: dict[str, Quote]) -> None: ...

    @abstractmethod
    async def set_quote(self, symbol: str, quote: Quote) -> None: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Optional[Quote]: ...

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    async def get_all(self) -> dict[str, Quote]: ...

    @abstractmethod
    async def size(self) -> int: ...

    @abstractmethod
    async def last_update(self) -> Optional[datetime]: ...

    @abstractmethod
    def subscribe(self) -> AsyncIterator[list[Quote]]: ...

    @abstractmethod
    async def get_intraday(self, symbol: str) -> list[dict]: ...

    async def is_stale(self) -> bool:
        lu = await self.last_update()
        if lu is None:
            return True
        age = (_now() - lu).total_seconds()
        return age > settings.staleness_seconds

    async def staleness_seconds(self) -> Optional[float]:
        lu = await self.last_update()
        if lu is None:
            return None
        return (_now() - lu).total_seconds()


# --------------------------------------------------------------------------- #
# In-memory (tek process)
# --------------------------------------------------------------------------- #
class MemoryStore(Store):
    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._history: dict[str, deque] = {}
        self._last_update: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()

    async def connect(self) -> None:
        logger.info("MemoryStore kullaniliyor (tek-process, Redis yok).")

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    def _publish(self, quotes: list[Quote]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(quotes)
            except asyncio.QueueFull:
                # Yavas tuketici: guncellemeyi atla (backpressure).
                pass

    def _persist(self, quotes: dict[str, Quote]) -> None:
        if not settings.persistence_enabled:
            return
        cap = settings.persistence_max_points
        for symbol, quote in quotes.items():
            dq = self._history.get(symbol)
            if dq is None:
                dq = deque(maxlen=cap)
                self._history[symbol] = dq
            dq.append({
                "t": quote.updated_at.isoformat() if quote.updated_at else None,
                "p": quote.price,
            })

    async def set_quotes(self, quotes: dict[str, Quote]) -> None:
        if not quotes:
            return
        async with self._lock:
            self._quotes.update(quotes)
            self._last_update = _now()
            self._persist(quotes)
        self._publish(list(quotes.values()))

    async def set_quote(self, symbol: str, quote: Quote) -> None:
        await self.set_quotes({symbol: quote})

    async def get_quote(self, symbol: str) -> Optional[Quote]:
        return self._quotes.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    async def get_all(self) -> dict[str, Quote]:
        return dict(self._quotes)

    async def size(self) -> int:
        return len(self._quotes)

    async def last_update(self) -> Optional[datetime]:
        return self._last_update

    async def subscribe(self) -> AsyncIterator[list[Quote]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    async def get_intraday(self, symbol: str) -> list[dict]:
        dq = self._history.get(symbol)
        return list(dq) if dq else []


# --------------------------------------------------------------------------- #
# Redis (paylasimli, cok worker)
# --------------------------------------------------------------------------- #
class RedisStore(Store):
    def __init__(self, url: str, prefix: str) -> None:
        self._url = url
        self._prefix = prefix
        self._redis = None
        self._quotes_key = f"{prefix}:quotes"
        self._last_update_key = f"{prefix}:meta:last_update"
        self._channel = f"{prefix}:updates"

    def _hist_key(self, symbol: str) -> str:
        return f"{self._prefix}:hist:{symbol}"

    async def connect(self) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(
            self._url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
        )
        await self._redis.ping()
        logger.info("RedisStore baglandi: %s", self._url)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def _persist(self, quotes: dict[str, Quote]) -> None:
        cap = settings.persistence_max_points
        pipe = self._redis.pipeline(transaction=False)
        for symbol, quote in quotes.items():
            key = self._hist_key(symbol)
            point = json.dumps({
                "t": quote.updated_at.isoformat() if quote.updated_at else None,
                "p": quote.price,
            })
            pipe.lpush(key, point)
            pipe.ltrim(key, 0, cap - 1)
        await pipe.execute()

    async def set_quotes(self, quotes: dict[str, Quote]) -> None:
        if not quotes:
            return
        mapping = {s: q.model_dump_json() for s, q in quotes.items()}
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(self._quotes_key, mapping=mapping)
        pipe.set(self._last_update_key, _now().isoformat())
        await pipe.execute()

        payload = json.dumps([q.model_dump(mode="json") for q in quotes.values()], default=str)
        await self._redis.publish(self._channel, payload)

        if settings.persistence_enabled:
            await self._persist(quotes)

    async def set_quote(self, symbol: str, quote: Quote) -> None:
        await self.set_quotes({symbol: quote})

    async def get_quote(self, symbol: str) -> Optional[Quote]:
        raw = await self._redis.hget(self._quotes_key, symbol)
        return Quote.model_validate_json(raw) if raw else None

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        raw = await self._redis.hmget(self._quotes_key, symbols)
        out: dict[str, Quote] = {}
        for symbol, value in zip(symbols, raw):
            if value:
                out[symbol] = Quote.model_validate_json(value)
        return out

    async def get_all(self) -> dict[str, Quote]:
        raw = await self._redis.hgetall(self._quotes_key)
        return {k: Quote.model_validate_json(v) for k, v in raw.items()}

    async def size(self) -> int:
        return int(await self._redis.hlen(self._quotes_key))

    async def last_update(self) -> Optional[datetime]:
        raw = await self._redis.get(self._last_update_key)
        return datetime.fromisoformat(raw) if raw else None

    async def subscribe(self) -> AsyncIterator[list[Quote]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    yield [Quote.model_validate(item) for item in data]
                except Exception as exc:  # noqa: BLE001
                    logger.warning("pub/sub mesaj ayristirma hatasi: %s", exc)
        finally:
            await pubsub.unsubscribe(self._channel)
            await pubsub.aclose()

    async def get_intraday(self, symbol: str) -> list[dict]:
        raw = await self._redis.lrange(self._hist_key(symbol), 0, -1)
        points = [json.loads(x) for x in raw]
        points.reverse()  # eskiden yeniye
        return points


# --------------------------------------------------------------------------- #
# Fabrika (singleton)
# --------------------------------------------------------------------------- #
_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        if settings.redis_enabled:
            _store = RedisStore(settings.redis_url, settings.redis_prefix)
        else:
            _store = MemoryStore()
    return _store
