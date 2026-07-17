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
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import metrics
from .config import settings
from .models import HistoryResponse, Quote

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _mask_redis_url(url: str) -> str:
    """HIGH-2 (PR#19 review): baglanti loglarinda parola ASLA gorunmesin --
    yalniz `redis://host:port/db` (kimlik bilgisi varsa `***@` ile maskeli)
    kalir. Onceden `self._url` oldugu gibi loglaniyordu, parola api/updater
    stdout/json-file log'una duz metin dusuyordu."""
    try:
        parsed = urlsplit(url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.username or parsed.password:
            netloc = f"***@{netloc}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        # Ayristirilamayan (bozuk userinfo/port) URL -- yine de parola
        # sizdirmadan tamamen maskele (fail-safe).
        return "***"


def _real_data_age_seconds(q: Quote, now: datetime) -> float | None:
    """HIGH-1 (kardes bulgu): `stale=True` isaretli bir quote'ta `updated_at`
    cache YAZIM anini gosterir (GERCEK veri yasini degil) -- fail-open (bkz.
    aggregator.end_cycle) veya baska bir mekanizma boyle bir quote'u bilerek
    stale isaretleyip yine de commit ettiginde, yalniz `updated_at`'e bakan
    bir yas hesabi cache-yazimiyla SIFIRLANIR; bu, `bist_oldest_quote_age_seconds`
    metrigini ve ona bakan `/ready`'nin `oldest_quote_age_seconds` alanini
    yaniltir (fresh_ratio()/is_stale() icin zaten kapatilan ayni korlugun
    farkli bir alandan sizmasi). `stale=True` quote'larda yas GERCEK veri
    zamanindan (`exchange_time or bar_time or updated_at` onceligiyle)
    hesaplanir; digerleri (fresh quote'larda ikisi zaten yakinsar) mevcut
    davranisla `updated_at`'ten hesaplanmaya devam eder. `oldest_update_age()`
    ve `fresh_ratio()` (dolayisiyla /ready + metrik) AYNI sozlesmeyi paylasir."""
    reference = (q.exchange_time or q.bar_time or q.updated_at) if q.stale else q.updated_at
    if reference is None:
        return None
    return (now - reference).total_seconds()


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
    async def get_quote(self, symbol: str) -> Quote | None: ...

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    async def get_all(self) -> dict[str, Quote]: ...

    @abstractmethod
    async def size(self) -> int: ...

    @abstractmethod
    async def last_update(self) -> datetime | None: ...

    @abstractmethod
    async def negative_cache_has(self, symbol: str) -> bool: ...

    @abstractmethod
    async def negative_cache_add(self, symbol: str) -> None: ...

    @abstractmethod
    async def get_history_cached(
        self, symbol: str, period: str, interval: str
    ) -> HistoryResponse | None: ...

    @abstractmethod
    async def set_history_cached(
        self, symbol: str, period: str, interval: str, data: HistoryResponse
    ) -> None: ...

    @abstractmethod
    def subscribe(self, symbols: frozenset[str] | None = None) -> AsyncIterator[list[Quote]]: ...

    @abstractmethod
    async def get_intraday(self, symbol: str) -> list[dict]: ...

    async def oldest_update_age(self) -> float | None:
        """En eski sembol guncelleme yasi (sn). Staleness icin kullanilir.

        bkz. `_real_data_age_seconds`: `stale=True` isaretli quote'larda yas
        cache-yazim ani degil GERCEK veri zamanindan hesaplanir."""
        data = await self.get_all()
        if not data:
            return None
        now = _now()
        ages: list[float] = []
        for q in data.values():
            age = _real_data_age_seconds(q, now)
            if age is not None:
                ages.append(age)
        return max(ages) if ages else None

    async def fresh_ratio(self) -> float | None:
        """Taze (yasi staleness esiginin altinda) sembol orani; veri yoksa None.

        updated_at'i olmayan quote taze SAYILMAZ (muhafazakar). HIGH-1: `stale=True`
        isaretli quote de TAZE SAYILMAZ -- fail-open (bkz. aggregator.end_cycle)
        veya baska bir mekanizma bir quote'u bilerek stale isaretleyip yine de
        commit ettiginde, `updated_at` cache YAZIM anini gosterir (GERCEK veri
        yasini degil); bu alan tek basina kullanilirsa fail-open sirasinda
        `/ready` "saglikli" gorunur, hicbir alarm calmazdi (bkz. review). Ayni
        kordugun `bist_oldest_quote_age_seconds` metrigine sizmamasi icin
        `oldest` de `_real_data_age_seconds` ile (stale=True icin GERCEK veri
        zamanindan) hesaplanir."""
        data = await self.get_all()
        if not data:
            return None
        now = _now()
        fresh = 0
        oldest: float | None = None
        for q in data.values():
            age = _real_data_age_seconds(q, now)
            if age is None:
                continue
            if oldest is None or age > oldest:
                oldest = age
            if q.stale:
                continue
            if age <= settings.staleness_seconds:
                fresh += 1
        if oldest is not None:
            metrics.OLDEST_QUOTE_AGE.set(oldest)
        return fresh / len(data)

    async def is_stale(self) -> bool:
        """Veri bayat mi? Taze sembol KAPSAMASINA bakar (en-eski-sembol degil):
        tek guncellenemeyen sembol (askidaki hisse, watchlist-disi tek sorgu)
        tum servisi kalici bayat gosteremez.

        HIGH-1 (review-4, REGRESYON): acilis toleransi eskiden `staleness_
        seconds` (vars. 300 sn) idi -- ama veri ~15 dk (900 sn) gecikmeli
        oldugu icin acilisin ilk ~15 dk'sinda kaynagin regularMarketTime'i
        hala dunku/Cuma gunune ait olabilir; guard TUM sembolleri duser,
        TUR-duzeyi fail-open devreye girer (bkz. aggregator.end_cycle),
        `fresh_ratio()=0` olur -- ama bu servis 300 sn sonra zaten "bayat"
        sayip HER ISLEM GUNU acilista ~10-16 dk `/ready` 503 + critical alarm
        uretiyordu (rutin, arizasiz bir durum icin). Guard'in KENDI acilis
        toleransi (`GUARD_OPEN_GRACE_SECONDS`, vars. 1200 sn) zaten "acilistan
        sonraki bu kadar sure icinde tam guard-dususu NORMALDIR" karari
        vermisti -- ama bu karar yalniz provider-streak'e uygulaniyordu,
        buradaki (ayri) 300 sn'lik tolerans ona HIC bakmiyordu. Artik ayni
        pencereyi paylasir: acilisin ilk GUARD_OPEN_GRACE_SECONDS'i icinde
        (dolayisiyla fail-open'in fiilen calistigi tum sure boyunca) is_stale
        False doner -- servis hazir, veri henuz gecikmeli (BEKLENEN). Pencere
        disinda hala dusuk fresh_ratio varsa bu GERCEK bir sorundur -> 503."""
        from .market import seconds_since_open

        ratio = await self.fresh_ratio()
        if ratio is None:
            return True

        since_open = seconds_since_open()
        if since_open is None:
            return False
        if since_open <= settings.guard_open_grace_seconds:
            return False

        return ratio < settings.staleness_min_fresh_pct / 100.0

    async def staleness_seconds(self) -> float | None:
        return await self.oldest_update_age()


# --------------------------------------------------------------------------- #
# In-memory (tek process)
# --------------------------------------------------------------------------- #
class _MemorySubscriber:
    __slots__ = ("queue", "symbols")

    def __init__(self, symbols: frozenset[str] | None) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.symbols = symbols


class MemoryStore(Store):
    _NEGATIVE_MAX = 4096
    # /history/{symbol} sembolu yalnizca BICIM olarak dogrular (gercek watchlist
    # uyeligini degil); farkli sembol x period x interval kombinasyonlariyla
    # tek seferlik (typo/bot) sorgular tavan+budama olmadan onbellegi sinirsiz
    # buyutebilir (bkz. _negative ile ayni desen).
    _HISTORY_CACHE_MAX = 4096

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._history: dict[str, deque] = {}
        self._last_update: datetime | None = None
        self._lock = asyncio.Lock()
        self._subscribers: set[_MemorySubscriber] = set()
        self._negative: dict[str, float] = {}
        self._history_cache: dict[tuple[str, str, str], tuple[float, HistoryResponse]] = {}

    async def connect(self) -> None:
        logger.info("MemoryStore kullaniliyor (tek-process, Redis yok).")

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def negative_cache_has(self, symbol: str) -> bool:
        expiry = self._negative.get(symbol.upper())
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            self._negative.pop(symbol.upper(), None)
            return False
        return True

    async def negative_cache_add(self, symbol: str) -> None:
        sym = symbol.upper()
        if len(self._negative) >= self._NEGATIVE_MAX:
            now = time.monotonic()
            expired = [s for s, exp in self._negative.items() if exp <= now]
            for s in expired:
                self._negative.pop(s, None)
            if len(self._negative) >= self._NEGATIVE_MAX:
                self._negative.clear()
        self._negative[sym] = time.monotonic() + settings.negative_cache_ttl

    async def get_history_cached(
        self, symbol: str, period: str, interval: str
    ) -> HistoryResponse | None:
        key = (symbol.upper(), period, interval)
        hit = self._history_cache.get(key)
        if hit is None:
            return None
        if time.monotonic() >= hit[0]:
            self._history_cache.pop(key, None)
            return None
        return hit[1]

    async def set_history_cached(
        self, symbol: str, period: str, interval: str, data: HistoryResponse
    ) -> None:
        key = (symbol.upper(), period, interval)
        if key not in self._history_cache and len(self._history_cache) >= self._HISTORY_CACHE_MAX:
            now = time.monotonic()
            expired = [k for k, v in self._history_cache.items() if v[0] <= now]
            for k in expired:
                self._history_cache.pop(k, None)
            if len(self._history_cache) >= self._HISTORY_CACHE_MAX:
                self._history_cache.clear()
        self._history_cache[key] = (
            time.monotonic() + settings.history_cache_ttl,
            data,
        )

    def _publish(self, quotes: list[Quote]) -> None:
        for sub in list(self._subscribers):
            filtered = quotes
            if sub.symbols is not None:
                filtered = [q for q in quotes if q.symbol in sub.symbols]
            if not filtered:
                continue
            try:
                sub.queue.put_nowait(filtered)
            except asyncio.QueueFull:
                # Yavas abone: olay dusuyor — sessiz kalmasin, metrikle gorunur olsun.
                metrics.SSE_DROPPED_EVENTS.inc()

    def _persist(self, quotes: dict[str, Quote]) -> None:
        if not settings.persistence_enabled:
            return
        cap = settings.persistence_max_points
        for symbol, quote in quotes.items():
            dq = self._history.get(symbol)
            if dq is None:
                dq = deque(maxlen=cap)
                self._history[symbol] = dq
            dq.append(
                {
                    "t": quote.updated_at.isoformat() if quote.updated_at else None,
                    "p": quote.price,
                }
            )

    async def set_quotes(self, quotes: dict[str, Quote]) -> None:
        if not quotes:
            return
        now = _now()
        for q in quotes.values():
            if q.updated_at is None:
                q.updated_at = now
        async with self._lock:
            self._quotes.update(quotes)
            times = [q.updated_at for q in self._quotes.values() if q.updated_at]
            self._last_update = max(times) if times else _now()
            self._persist(quotes)
        self._publish(list(quotes.values()))

    async def set_quote(self, symbol: str, quote: Quote) -> None:
        await self.set_quotes({symbol: quote})

    async def get_quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    async def get_all(self) -> dict[str, Quote]:
        return dict(self._quotes)

    async def size(self) -> int:
        return len(self._quotes)

    async def last_update(self) -> datetime | None:
        return self._last_update

    async def subscribe(self, symbols: frozenset[str] | None = None) -> AsyncIterator[list[Quote]]:
        sub = _MemorySubscriber(symbols)
        self._subscribers.add(sub)
        try:
            while True:
                yield await sub.queue.get()
        finally:
            self._subscribers.discard(sub)

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
        self._redis: Any = None
        self._quotes_key = f"{prefix}:quotes"
        self._last_update_key = f"{prefix}:meta:last_update"
        self._channel = f"{prefix}:updates"

    def _hist_key(self, symbol: str) -> str:
        return f"{self._prefix}:hist:{symbol}"

    def _neg_key(self, symbol: str) -> str:
        return f"{self._prefix}:neg:{symbol.upper()}"

    def _hist_cache_key(self, symbol: str, period: str, interval: str) -> str:
        return f"{self._prefix}:histcache:{symbol.upper()}:{period}:{interval}"

    def _symbol_channel(self, symbol: str) -> str:
        return f"{self._prefix}:updates:{symbol.upper()}"

    def _subscribe_channels(self, symbols: frozenset[str] | None) -> list[str]:
        """Abone olunacak Redis kanallarini secer.

        symbols is None  -> tum-liste (broadcast kanali).
        symbols dolu     -> per-sembol kanallar.
        symbols BOS set  -> istenen tum sembollerin gecersiz/karsilanamaz oldugu
                            durum: asla PUBLISH edilmeyen sentinel kanal. Boylece
                            istemci baglantisi acik kalir (keep-alive) ama
                            tum-piyasa akisini ALMAZ (eski `if symbols:` bunu
                            yanlislikla tum-liste sayiyordu)."""
        if symbols is None:
            return [self._channel]
        return [self._symbol_channel(s) for s in symbols] or [f"{self._prefix}:updates:__none__"]

    async def connect(self) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(
            self._url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=settings.redis_socket_timeout,
            socket_keepalive=True,
            health_check_interval=30,
        )
        await self._redis.ping()
        logger.info("RedisStore baglandi: %s", _mask_redis_url(self._url))

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def negative_cache_has(self, symbol: str) -> bool:
        return bool(await self._redis.exists(self._neg_key(symbol)))

    async def negative_cache_add(self, symbol: str) -> None:
        ttl = max(1, int(settings.negative_cache_ttl))
        await self._redis.setex(self._neg_key(symbol), ttl, "1")

    async def get_history_cached(
        self, symbol: str, period: str, interval: str
    ) -> HistoryResponse | None:
        raw = await self._redis.get(self._hist_cache_key(symbol, period, interval))
        if not raw:
            return None
        try:
            return HistoryResponse.model_validate_json(raw)
        except Exception:
            return None

    async def set_history_cached(
        self, symbol: str, period: str, interval: str, data: HistoryResponse
    ) -> None:
        ttl = max(1, int(settings.history_cache_ttl))
        await self._redis.setex(
            self._hist_cache_key(symbol, period, interval),
            ttl,
            data.model_dump_json(),
        )

    async def _persist(self, quotes: dict[str, Quote]) -> None:
        cap = settings.persistence_max_points
        pipe = self._redis.pipeline(transaction=False)
        for symbol, quote in quotes.items():
            key = self._hist_key(symbol)
            point = json.dumps(
                {
                    "t": quote.updated_at.isoformat() if quote.updated_at else None,
                    "p": quote.price,
                }
            )
            pipe.lpush(key, point)
            pipe.ltrim(key, 0, cap - 1)
        await pipe.execute()

    async def set_quotes(self, quotes: dict[str, Quote]) -> None:
        if not quotes:
            return
        now = _now()
        for q in quotes.values():
            if q.updated_at is None:
                q.updated_at = now
        mapping = {s: q.model_dump_json() for s, q in quotes.items()}
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(self._quotes_key, mapping=mapping)
        pipe.set(self._last_update_key, _now().isoformat())
        await pipe.execute()

        batch_payload = json.dumps(
            [q.model_dump(mode="json") for q in quotes.values()], default=str
        )
        await self._redis.publish(self._channel, batch_payload)
        for symbol, quote in quotes.items():
            single = json.dumps([quote.model_dump(mode="json")], default=str)
            await self._redis.publish(self._symbol_channel(symbol), single)

        if settings.persistence_enabled:
            await self._persist(quotes)

    async def set_quote(self, symbol: str, quote: Quote) -> None:
        await self.set_quotes({symbol: quote})

    async def get_quote(self, symbol: str) -> Quote | None:
        raw = await self._redis.hget(self._quotes_key, symbol)
        return Quote.model_validate_json(raw) if raw else None

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        raw = await self._redis.hmget(self._quotes_key, symbols)
        out: dict[str, Quote] = {}
        for symbol, value in zip(symbols, raw, strict=False):
            if value:
                out[symbol] = Quote.model_validate_json(value)
        return out

    async def get_all(self) -> dict[str, Quote]:
        raw = await self._redis.hgetall(self._quotes_key)
        return {k: Quote.model_validate_json(v) for k, v in raw.items()}

    async def size(self) -> int:
        return int(await self._redis.hlen(self._quotes_key))

    async def last_update(self) -> datetime | None:
        raw = await self._redis.get(self._last_update_key)
        return datetime.fromisoformat(raw) if raw else None

    async def subscribe(self, symbols: frozenset[str] | None = None) -> AsyncIterator[list[Quote]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*self._subscribe_channels(symbols))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    yield [Quote.model_validate(item) for item in data]
                except Exception as exc:
                    logger.warning("pub/sub mesaj ayristirma hatasi: %s", exc)
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    async def get_intraday(self, symbol: str) -> list[dict]:
        raw = await self._redis.lrange(self._hist_key(symbol), 0, -1)
        points = [json.loads(x) for x in raw]
        points.reverse()
        return points


# --------------------------------------------------------------------------- #
# Fabrika (singleton)
# --------------------------------------------------------------------------- #
_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        if settings.redis_enabled:
            _store = RedisStore(settings.redis_url, settings.redis_prefix)
        else:
            _store = MemoryStore()
    return _store
