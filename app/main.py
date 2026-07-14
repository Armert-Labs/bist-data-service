"""bist-canli-api — FastAPI uygulamasi (mikroservis).

Uc noktalar:
  GET /                 -> servis bilgisi
  GET /health           -> liveness (surec ayakta mi)
  GET /ready            -> readiness (store + taze veri var mi)
  GET /metrics          -> Prometheus metrikleri
  GET /symbols          -> takip edilen semboller
  GET /all              -> TEK cagriyla tum BIST anlik fiyatlari
  GET /quote/{symbol}   -> tek hisse
  GET /quotes?symbols=  -> coklu / tumu
  GET /history/{symbol} -> gecmis OHLCV
  GET /stream?symbols=  -> SSE canli akis (store pub/sub fan-out)
  GET /demo             -> canli test sayfasi
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sse_starlette.sse import EventSourceResponse

from . import __version__, metrics
from . import symbols as sym
from .aggregator import aggregator
from .auth import registry
from .config import settings, validate_production
from .deps import fetch_semaphore, limiter, require_api_key
from .logging_config import set_request_id, setup_logging
from .market import is_market_open, is_stale_bar, market_state
from .models import HistoryResponse, Quote, UnavailableSymbol
from .pipeline import (
    compare_against_references,
    fetch_and_commit,
    fetch_quotes,
    gather_reference_quotes,
)
from .store import get_store
from .updater import updater
from .webhooks import webhook_manager

setup_logging()
logger = logging.getLogger("bist-canli-api")

store = get_store()

VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1h",
    "1d",
    "5d",
    "1wk",
    "1mo",
    "3mo",
}
VALID_SORTS = {"symbol", "price", "change", "change_percent", "volume"}

_sse_clients = 0
# /all micro-cache: (sort, order) -> (monotonic_ts, serialize edilmis JSON bytes).
# Bytes saklanir ki cache isabetinde 507 quote yeniden serialize edilmesin.
_all_cache: dict[tuple[str, str, str], tuple[float, bytes, str]] = {}


def _all_response(request: Request, body: bytes, etag: str) -> Response:
    """ETag'li /all yaniti: govde degismediyse 304 ile bant genisligi kazan."""
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=body, media_type="application/json", headers={"ETag": etag})


def _with_live_state(q: Quote, state: str) -> Quote:
    """market_state'i ANLIK duruma cevirir + okuma aninda data_age_seconds/stale
    hesaplar (exchange_time varsa oradan, yoksa updated_at'ten). Store objesi
    mutasyona ugratilmaz (MemoryStore ayni nesneyi paylasir); bu alanlar her
    okumada degistigi icin artik HER zaman bir kopya doner."""
    now_moment = datetime.now(UTC)
    reference = q.exchange_time or q.updated_at
    # MEDIUM-2: saat kaymasi / klemp bosluğu gibi uc durumlarda referans
    # "simdi"den ileride kalabilir (orn. isyatirim'in kapanis-zamani tahmini);
    # data_age_seconds negatif GORUNMEMELI.
    age = max((now_moment - reference).total_seconds(), 0.0) if reference else None
    age_based_stale = age is not None and state == "OPEN" and age > settings.staleness_seconds
    # HIGH-2: yas-tabanli hesap (yukarida) exchange_time/updated_at'e bakar --
    # isyatirim/tradingview gibi exchange_time HIC doldurmayan kaynaklarda
    # reference=updated_at (cache YAZIM ani) olur, bu GERCEK veri (bar_time)
    # yasini yansitmaz. Guard yalniz FETCH aninda (aggregator.fetch_quotes)
    # calisir; okuma aninda bir daha bakilmazsa acilisin ilk dakikalarinda
    # (warm-up piyasa kapaliyken doğru gecen, ama okuma piyasa acilinca
    # yapilan) bir Cuma kapanisi "taze" servis edilebilirdi (bkz. review).
    # Okuma da bar_time'i (varsa, yoksa exchange_time'i) yeniden kontrol eder.
    bar_stale = is_stale_bar(q.bar_time or q.exchange_time, now_moment)
    # MEDIUM-1 (review-2): aggregator'in fail-open yolunda quote'a koydugu
    # `stale=True` (bkz. aggregator.end_cycle) buraya kadar ULAŞMIYORDU --
    # asagidaki model_copy KOŞULSUZ ezip yeniden hesapliyordu. Fail-open
    # ozellikle "veri stale isaretiyle geciyor" diye belgelenmisti
    # (README/CHANGELOG) -- bu vaat tutulmuyordu. Artik provider/aggregator'in
    # koydugu bayrak KORUNUR (OR'lanir), diger iki hesap EK birer sebep olarak calisir.
    stale = q.stale or age_based_stale or bar_stale
    return q.model_copy(
        update={
            "market_state": state,
            "data_age_seconds": round(age, 2) if age is not None else None,
            "stale": stale,
        }
    )


def _sse_quotes_payload(
    quotes: list[Quote], symbol_filter: frozenset[str] | None, state: str
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for q in quotes:
        if symbol_filter is None or q.symbol in symbol_filter:
            out[q.symbol] = _with_live_state(q, state).model_dump(mode="json")
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Servis baslatiliyor (v%s), redis=%s", __version__, settings.redis_enabled)

    # Kimlik dogrulama guard'lari: uretimde fail-fast, aksi halde uyari logla.
    validate_production()

    if settings.production_mode and not registry.enabled:
        raise RuntimeError(
            "PRODUCTION_MODE=true ancak API anahtari tanimli degil. "
            "API_KEYS veya API_KEYS_SHA256 ayarlayin."
        )

    if settings.auth_required and not registry.enabled:
        logger.error("AUTH_REQUIRED=true ancak hic API anahtari yok! Veri uclari 503 donecek.")
    elif not registry.enabled:
        logger.warning(
            "Kimlik dogrulama KAPALI (API anahtari tanimli degil). Uretim icin API_KEYS ayarlayin."
        )

    await store.connect()

    background_tasks: list[asyncio.Task] = []
    # Redis YOK ise updater ve webhook izleyici bu surecte calisir (tek instance).
    # Redis VAR ise bunlar ayri `updater_main` servisindedir (cift cekim onlenir).
    if not settings.redis_enabled:
        updater.start()
        background_tasks.append(asyncio.create_task(webhook_manager.watch(store)))

    try:
        yield
    finally:
        logger.info("Servis kapatiliyor...")
        for task in background_tasks:
            task.cancel()
        # Cancel'i bekle: webhook drain'i tamamlansin, task'in onceki bir
        # exception'i varsa burada gorunur olsun.
        for task in background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Arka plan gorevi hatayla sonlanmisti")
        if not settings.redis_enabled:
            await updater.stop()
        await store.close()


_TAGS_METADATA = [
    {"name": "Fiyat", "description": "Anlik ve gecmis fiyat uc noktalari."},
    {"name": "Dogrulama", "description": "Kaynaklar arasi fiyat dogrulama."},
    {"name": "Akis", "description": "SSE ile canli fiyat akisi."},
    {"name": "Sistem", "description": "Saglik, hazirlik ve metrikler."},
]

app = FastAPI(
    title="BIST Data Service",
    description=(
        "BIST hisseleri icin ~15 dk gecikmeli fiyatlari toplayan, Redis'te "
        "onbellekleyen ve REST + SSE ile sunan mikroservis.\n\n"
        "**Kimlik dogrulama:** `API_KEYS` tanimliysa veri uc noktalari `X-API-Key` "
        "basligi ister. `/health` ve `/ready` acik kalir."
    ),
    version=__version__,
    lifespan=lifespan,
    openapi_tags=_TAGS_METADATA,
    contact={"name": "Armert Labs", "url": "https://github.com/Armert-Labs/bist-data-service"},
    license_info={
        "name": "MIT",
        "url": "https://github.com/Armert-Labs/bist-data-service/blob/main/LICENSE",
    },
)

# --- Middleware / eklentiler ---
# ~500 sembollu /all govdesi JSON'da ~10x sikisir; SSE zaten stream oldugu icin
# etkilenmez (yalnizca yeterince buyuk duz yanitlar sikistirilir).
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda r, e: _rate_limit_response())
if settings.rate_limit_enabled:
    app.add_middleware(SlowAPIMiddleware)

# Prometheus: HTTP metriklerini topla. /metrics'i yalnizca METRICS_PUBLIC=true ise
# herkese ac; aksi halde asagida API key ile korunur.
_instrumentator = Instrumentator().instrument(app)
if settings.metrics_public:
    _instrumentator.expose(app, endpoint="/metrics", include_in_schema=False)
else:

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_api_key)])
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _rate_limit_window_seconds() -> int:
    unit = settings.rate_limit.rsplit("/", 1)[-1].strip().lower()
    return {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(unit, 60)


def _rate_limit_response():
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=429,
        content={"detail": "Istek limiti asildi."},
        headers={
            "Retry-After": str(_rate_limit_window_seconds()),
            "X-RateLimit-Limit": settings.rate_limit,
        },
    )


# Request-ID: kullanicidan gelen deger loga ve response'a yazildigi icin
# temizlenir (log/header injection kalkani); yalnizca guvenli karakterler.
_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    raw = request.headers.get("X-Request-ID", "")
    rid = _REQUEST_ID_RE.sub("", raw)[:64] or uuid4().hex[:12]
    set_request_id(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# --- Yardimcilar ---
def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    parsed: list[str] = []
    for item in raw.split(","):
        s = sym.normalize(item)
        if not s:
            continue
        if not sym.is_valid_symbol(s):
            raise HTTPException(status_code=400, detail=f"Gecersiz sembol: {item!r}")
        parsed.append(s)
    return list(dict.fromkeys(parsed))


# Gecersiz sembol raporlanirken (unavailable[].symbol) tek bir asiri-uzun
# token'in payload'i sismesini engeller (gecerli sembol zaten <=6 karakter).
_MAX_REPORTED_SYMBOL_LEN = 16


def _parse_symbols_lenient(raw: str | None) -> tuple[list[str], list[str]]:
    """`_parse_symbols`'un HATA FIRLATMAYAN varyanti: gecerli ve gecersiz
    sembolleri AYRI listelerde doner (normalize + dedup, sira korunur).

    /stream bunu kullanir: bicim-gecersiz bir sembol tum baglantiyi 400 ile
    reddetmek yerine `unavailable[{reason: "invalid_format"}]` olarak
    raporlanir. /quote ve /quotes eskisi gibi `_parse_symbols` (400 firlatan)
    kullanmaya devam eder -- bu asimetri bilinclidir."""
    if not raw:
        return [], []
    valid: list[str] = []
    invalid: list[str] = []
    for item in raw.split(","):
        s = sym.normalize(item)
        if not s:
            continue
        if sym.is_valid_symbol(s):
            valid.append(s)
        else:
            invalid.append(s[:_MAX_REPORTED_SYMBOL_LEN])
    return list(dict.fromkeys(valid)), list(dict.fromkeys(invalid))


async def _resolve_quotes(requested: list[str]) -> dict[str, Quote]:
    """Istenen (bicim-gecerli) sembolleri store'dan getirir; cache'te olmayan
    ve negative-cache'te OLMAYANLARI on-demand fetch eder (fetch_semaphore +
    cross_validate=True). Fetch sonrasi hala gelmeyen sembol negative-cache'e
    eklenir. /quotes ve /stream snapshot'i AYNI yolu paylasir (kopya-mantik yok)."""
    result = await store.get_quotes(requested)
    missing = [s for s in requested if s not in result]
    missing = [s for s in missing if not await store.negative_cache_has(s)]
    if missing:
        async with fetch_semaphore:
            fetched = await fetch_and_commit(store, missing, cross_validate=True)
            result.update(fetched)
        for s in missing:
            if s not in result:
                await store.negative_cache_add(s)
    return result


def _check_symbol_limit(requested: list[str]) -> None:
    """/quotes ve /validate icin sembol sayisi ust sinirini uygular.

    Asiri sayida sembol (her biri cache'te yoksa upstream provider'lara tek
    tek dusebilir) kaynaklari zorlayan bir DoS yuzeyi olusturmasin.
    """
    limit = settings.max_symbols_per_request
    if limit > 0 and len(requested) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"Cok fazla sembol: {len(requested)} istendi, ust sinir {limit}.",
        )


async def _get_or_fetch(symbol: str) -> Quote | None:
    cached = await store.get_quote(symbol)
    if cached is not None:
        return cached
    if await store.negative_cache_has(symbol):
        return None
    async with fetch_semaphore:
        cached = await store.get_quote(symbol)
        if cached is not None:
            return cached
        quotes = await fetch_and_commit(store, [symbol], cross_validate=True)
    quote = quotes.get(symbol)
    if quote is None:
        await store.negative_cache_add(symbol)
        return None
    return quote


# --- Uc noktalar ---
@app.get("/", tags=["Sistem"], summary="Servis bilgisi")
async def root() -> dict:
    return {
        "name": "BIST Canli API",
        "version": __version__,
        "kaynak": "Yahoo Finance + Is Yatirim (fallback)",
        "gecikme": "~15 dakika",
        "ana_uc_nokta": "/all",
        "uc_noktalar": [
            "/all",
            "/health",
            "/ready",
            "/validate",
            "/metrics",
            "/symbols",
            "/quote/{symbol}",
            "/quotes",
            "/history/{symbol}",
            "/intraday/{symbol}",
            "/stream",
            "/demo",
            "/docs",
        ],
    }


@app.get("/health", tags=["Sistem"], summary="Liveness — surec ayakta mi")
@limiter.exempt
async def health() -> dict:
    """Liveness: surec ayakta mi. (Container restart karari icin.)

    Rate limit'ten muaftir: ayni IP'den gelen yogun istemci trafigi limiti
    doldurdugunda saglik problari 429 ile 'coktu' sanilmamali.
    """
    return {"status": "ok", "version": __version__}


@app.get("/ready", tags=["Sistem"], summary="Readiness — trafik almaya hazir mi")
@limiter.exempt
async def ready() -> dict:
    """Readiness: store erisilebilir ve veri taze mi. (Trafik almaya hazir mi.)"""
    # Store (Redis) tamamen koptugunde bile sozlesmedeki yapisal 503 govdesi
    # donmeli; ping ile cagri arasindaki yariska karsi tum erisim korunur.
    size, stale, oldest_age, fresh, last_age = 0, True, None, None, None
    try:
        store_ok = await store.ping()
        if store_ok:
            size = await store.size()
            stale = await store.is_stale()
            oldest_age = await store.staleness_seconds()
            fresh = await store.fresh_ratio()
            last_update = await store.last_update()
            last_age = (datetime.now(UTC) - last_update).total_seconds() if last_update else None
    except Exception:
        logger.exception("/ready store erisim hatasi")
        store_ok = False
        size, stale, oldest_age, fresh, last_age = 0, True, None, None, None
    # MEDIUM (review-5): acilis-toleransi penceresinde (bkz. store.is_stale)
    # `stale` KOSULSUZ False doner -- "veri gecikmeli ama boru hatti saglikli"
    # ile "boru hatti OLU (wedge/donmus updater)" ayirt edilemez, ikisi de
    # `fresh_ratio=0` uretir. Servisin bilinen ANA arizasi (wedge/donmus
    # updater -- py-spy watcher tam bunun icin var) bu pencerede 20 dk'ya
    # kadar tespit edilemezdi. Ayirt edici: SAGLIKLI bir updater store'a HER
    # `UPDATE_INTERVAL`de (vars. 60 sn) yazar; `last_update_age_seconds` bu
    # sureyi (comert bir tampon = 2x) asarsa boru hatti GERCEKTEN olmustur --
    # `stale`/grace penceresi ne derse desin. Yalniz market ACIKKEN uygulanir
    # (kapaliyken updater bilerek calismaz -- UPDATE_WHEN_CLOSED=false varsayilan
    # -- last_age dogal olarak buyur, bu bir ariza degildir).
    market_open_now = is_market_open()
    wedge_detected = (
        market_open_now and last_age is not None and last_age > 2 * settings.update_interval
    )
    ready_flag = store_ok and size > 0 and not stale and not wedge_detected

    body = {
        "ready": ready_flag,
        "store_ok": store_ok,
        "quotes_cached": size,
        "is_stale": stale,
        "fresh_pct": round(fresh * 100.0, 1) if fresh is not None else None,
        "last_update_age_seconds": round(last_age, 1) if last_age is not None else None,
        "oldest_quote_age_seconds": round(oldest_age, 1) if oldest_age is not None else None,
        "market_open": is_market_open(),
        "providers": aggregator.provider_states,
    }
    if not ready_flag:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.get(
    "/symbols",
    tags=["Fiyat"],
    summary="Takip edilen semboller",
    dependencies=[Depends(require_api_key)],
)
async def list_symbols() -> dict:
    return {"count": len(updater.symbols), "symbols": updater.symbols}


@app.get(
    "/all",
    tags=["Fiyat"],
    summary="Tum BIST anlik fiyatlari",
    dependencies=[Depends(require_api_key)],
)
async def all_quotes(
    request: Request,
    sort: str = Query(default="symbol", description="symbol|price|change|change_percent|volume"),
    order: str = Query(default="asc", description="asc|desc"),
) -> Response:
    """TEK cagriyla tum takip edilen BIST hisselerinin anlik (gecikmeli) fiyati."""
    if sort not in VALID_SORTS:
        raise HTTPException(
            status_code=400, detail=f"Gecersiz sort. Gecerli: {sorted(VALID_SORTS)}"
        )
    if order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="order 'asc' veya 'desc' olmali.")

    # Kisa-TTL bytes-cache: isabette hicbir serializasyon yapilmaz (Response
    # dogrudan hazir JSON bytes doner). 507 quote'un her istekte yeniden
    # dump edilmesi yuksek trafikte ana CPU maliyetiydi.
    # Anahtar market state icerir: kapanis aninda cache isabeti eski OPEN
    # damgasini TTL boyunca servis etmesin.
    state = market_state()
    cache_key = (sort, order, state)
    now = time.monotonic()
    hit = _all_cache.get(cache_key)
    if hit is not None and (now - hit[0]) < settings.all_cache_ttl:
        return _all_response(request, hit[1], hit[2])

    data = await store.get_all()
    quotes = list(data.values())
    reverse = order == "desc"

    def key_fn(q: Quote):
        if sort == "symbol":
            return q.symbol
        value = getattr(q, sort)
        return value if value is not None else float("-inf")

    quotes.sort(key=key_fn, reverse=reverse)

    last_update = await store.last_update()
    quotes_json: list[dict[str, Any]] = [
        _with_live_state(q, state).model_dump(mode="json") for q in quotes
    ]
    payload = {
        "market": state,
        "count": len(quotes),
        "last_update": last_update.isoformat() if last_update else None,
        "is_stale": await store.is_stale(),
        "delayed": True,
        "quotes": quotes_json,
    }
    body = json.dumps(payload, default=str).encode("utf-8")
    # LOW-a: ETag data_age_seconds/stale gibi OKUMA ANINDA surekli degisen
    # alanlardan degil, "gercek" veriden turetilmeli -- aksi halde fiyat hic
    # degismese bile her cache-yenilemesinde ETag degisir ve 304 yolu hicbir
    # zaman tetiklenmez.
    etag_basis = {
        **payload,
        "quotes": [
            {k: v for k, v in q.items() if k not in ("data_age_seconds", "stale")}
            for q in quotes_json
        ],
    }
    etag_source = json.dumps(etag_basis, default=str, sort_keys=True).encode("utf-8")
    etag = f'W/"{hashlib.sha256(etag_source).hexdigest()[:16]}"'
    _all_cache[cache_key] = (now, body, etag)
    return _all_response(request, body, etag)


@app.get(
    "/quote/{symbol}",
    response_model=Quote,
    tags=["Fiyat"],
    summary="Tek hisse anlik fiyat",
    dependencies=[Depends(require_api_key)],
)
async def get_quote(symbol: str) -> Quote:
    if not sym.is_valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"Gecersiz sembol: {symbol!r}")
    quote = await _get_or_fetch(sym.normalize(symbol))
    if quote is None:
        raise HTTPException(status_code=404, detail=f"Veri bulunamadi: {sym.normalize(symbol)}")
    return _with_live_state(quote, market_state())


@app.get(
    "/quotes",
    tags=["Fiyat"],
    summary="Coklu hisse (veya tumu)",
    dependencies=[Depends(require_api_key)],
)
async def get_quotes(
    symbols: str | None = Query(default=None, description="Virgulle ayrilmis, orn. THYAO,GARAN"),
) -> dict:
    requested = _parse_symbols(symbols)
    _check_symbol_limit(requested)

    if not requested:
        data = await store.get_all()
        state = market_state()
        return {
            "count": len(data),
            "market": state,
            "missing": [],
            "quotes": {
                s: _with_live_state(q, state).model_dump(mode="json") for s, q in data.items()
            },
        }

    result = await _resolve_quotes(requested)

    state = market_state()
    return {
        "count": len(result),
        "market": state,
        # Istemci 'sembol bulunamadi/veri gelmedi' durumunu sessiz atlama yerine
        # acikca gorebilsin (negatif cache'tekiler de burada listelenir).
        "missing": [s for s in requested if s not in result],
        "quotes": {
            s: _with_live_state(q, state).model_dump(mode="json") for s, q in result.items()
        },
    }


@app.get(
    "/history/{symbol}",
    response_model=HistoryResponse,
    tags=["Fiyat"],
    summary="Gecmis OHLCV",
    dependencies=[Depends(require_api_key)],
)
async def get_history(
    symbol: str,
    period: str = Query(default="1mo"),
    interval: str = Query(default="1d"),
) -> HistoryResponse:
    if not sym.is_valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"Gecersiz sembol: {symbol!r}")
    if period not in VALID_PERIODS:
        raise HTTPException(
            status_code=400, detail=f"Gecersiz period. Gecerli: {sorted(VALID_PERIODS)}"
        )
    if interval not in VALID_INTERVALS:
        raise HTTPException(
            status_code=400, detail=f"Gecersiz interval. Gecerli: {sorted(VALID_INTERVALS)}"
        )

    norm = sym.normalize(symbol)
    cached = await store.get_history_cached(norm, period, interval)
    if cached is not None and cached.bars:
        return cached

    result = await aggregator.fetch_history(norm, period, interval)
    if not result.bars:
        raise HTTPException(status_code=404, detail=f"Gecmis veri bulunamadi: {norm}")
    await store.set_history_cached(norm, period, interval, result)
    return result


@app.get(
    "/intraday/{symbol}",
    tags=["Fiyat"],
    summary="Gun-ici snapshot gecmisi",
    dependencies=[Depends(require_api_key)],
)
async def get_intraday(symbol: str) -> dict:
    """Servis calistigi surece biriken gun-ici fiyat noktalari (persistence).

    Uzun gecmis icin /history kullanin; bu uc, son PERSISTENCE_MAX_POINTS
    kadar anlik snapshot'i (eskiden yeniye) doner.
    """
    if not sym.is_valid_symbol(symbol):
        raise HTTPException(status_code=400, detail=f"Gecersiz sembol: {symbol!r}")
    points = await store.get_intraday(sym.normalize(symbol))
    return {"symbol": sym.normalize(symbol), "count": len(points), "points": points}


REFERENCE_SYMBOLS = [
    "THYAO",
    "GARAN",
    "AKBNK",
    "ASELS",
    "SISE",
    "KCHOL",
    "TUPRS",
    "BIMAS",
    "EREGL",
    "FROTO",
]


@app.get(
    "/validate",
    tags=["Dogrulama"],
    summary="Capraz-kaynak fiyat dogrulama",
    dependencies=[Depends(require_api_key)],
)
async def validate(
    symbols: str | None = Query(default=None, description="Bos ise referans likit hisseler"),
) -> dict:
    """Fiyat dogruluk kontrolu: birincil veriyi (cache/yfinance) BAGIMSIZ bir
    referans kaynakla (varsayilan: yahoo_chart, farkli endpoint) karsilastirir.
    Iki kaynagin uyumu, verinin bozulmadiginin gostergesidir.
    """
    syms = _parse_symbols(symbols) or REFERENCE_SYMBOLS
    _check_symbol_limit(syms)

    primary = await store.get_quotes(syms)
    missing = [s for s in syms if s not in primary]
    if missing:
        async with fetch_semaphore:
            primary.update(await fetch_quotes(store, missing, cross_validate=False))

    # /validate, run_drift_monitor ve cross_validate_quotes AYNI cekim/verdict
    # cekirdegini kullanir (review MEDIUM-1) -- eskiden /validate kendi ayri
    # implementasyonuyla farkli bir sonuc/gauge degeri uretebiliyordu.
    references, reference_status = await gather_reference_quotes(syms)

    comparisons = []
    for s in syms:
        p = primary.get(s)
        pp = p.price if p else None
        row: dict[str, Any] = {
            "symbol": s,
            "primary": pp,
            "primary_source": p.source if p else None,
            "references": {},
        }
        for name in settings.validate_providers:
            r = references.get(name, {}).get(s)
            rp = r.price if r else None
            # Insan-teshis tablosu SEFFAFLIK icin TUM referanslari (kendi
            # kaynagi ve bayat olanlar dahil) gosterir; "self" bayragi
            # totolojik karsilastirmayi gorunur kilar. Asagidaki resmi
            # verdict (max_deviation_pct/consistent) bunlari HARIC tutar.
            is_self = p is not None and name == p.source
            if pp is not None and rp is not None and rp != 0:
                dev = abs(pp - rp) / rp * 100.0
                row["references"][name] = {
                    "price": rp,
                    "deviation_pct": round(dev, 3),
                    "ok": dev < settings.cross_validate_max_pct,
                    "self": is_self,
                }
            else:
                row["references"][name] = {
                    "price": rp,
                    "deviation_pct": None,
                    "ok": False,
                    "self": is_self,
                }
        comparisons.append(row)

    # Esik, yazma dogrulamasiyla ayni ayardan gelir; operator degistirdiginde
    # /validate raporu ile pipeline ayni dilde konusur. Bu salt-okunur
    # insan-teshis endpoint'i GAUGE YAZMAZ (bkz. run_drift_monitor -- tek yazar).
    # record_metrics=False (LOW-3): operator poll'u bist_validate_no_reference_total
    # sayacini sismesin -- o sayac arka plan drift-monitörünün gercek oranini
    # yansitmali.
    verdict = compare_against_references(primary, syms, references, record_metrics=False)

    return {
        "checked": len(syms),
        # compared=false: 'tutarsiz' degil 'hicbir referansla KARSILASTIRILAMADI'.
        "compared": verdict["compared"],
        "threshold_pct": settings.cross_validate_max_pct,
        "reference_status": reference_status,
        "max_deviation_pct": verdict["max_deviation_pct"],
        "consistent": verdict["consistent"],
        "note": "erisilemeyen referanslar (orn. isyatirim yurtdisi IP'den) reference_status'ta gorulur.",
        "comparisons": comparisons,
    }


async def _stream_generator(
    request: Request,
    symbol_filter: frozenset[str] | None,
    invalid_symbols: tuple[str, ...] = (),
):
    """SSE olay ureticisi: once tam snapshot (istege gore unavailable[]),
    sonra pub/sub akisi.

    Modul seviyesinde: TestClient uzerinden test edilemiyor (subscribe'da
    bloklanan generator kapanista iptal edilemiyor); dogrudan test edilir.

    `symbol_filter is None` -> tum-liste modu (symbols parametresi yok); eski
    davranis birebir korunur, unavailable[] URETILMEZ. Aksi halde on-demand
    mod: istenen semboller cozulur, karsilanamayanlar `unavailable[]` ile
    raporlanir. `invalid_symbols` yalnizca on-demand modda anlamlidir."""
    global _sse_clients
    _sse_clients += 1
    metrics.SSE_CLIENTS.set(_sse_clients)
    try:
        state = market_state()
        unavailable: list[dict] = []

        if symbol_filter is None:
            # Tum-liste modu: degismedi.
            initial = await store.get_all()
            snapshot = _sse_quotes_payload(list(initial.values()), symbol_filter, state)
        else:
            # On-demand modu: istenen (bicim-gecerli) sembolleri coz.
            valid = list(symbol_filter)
            # negative-cache uyeligini FETCH'ten ONCE yakala (reason ayrimi icin).
            neg_before = {s for s in valid if await store.negative_cache_has(s)}
            result = await _resolve_quotes(valid)
            snapshot = _sse_quotes_payload(list(result.values()), symbol_filter, state)
            # Oncelik sirasiyla: invalid_format > negative_cache > fetch_failed.
            for s in invalid_symbols:
                unavailable.append(
                    UnavailableSymbol(symbol=s, reason="invalid_format").model_dump()
                )
            for s in valid:
                if s in result:
                    continue
                reason = "negative_cache" if s in neg_before else "fetch_failed"
                unavailable.append(UnavailableSymbol(symbol=s, reason=reason).model_dump())

        # Guard fix: quotes bos OLSA bile unavailable varsa ILK event yayinlanir
        # (istenen sembollerin HEPSI karsilanamazsa consumer haber alsin).
        if snapshot or unavailable:
            payload: dict[str, Any] = {"market": state, "quotes": snapshot}
            # unavailable ANAHTARI yalniz non-empty ise eklenir -> unavailable
            # bos oldugunda payload eski sema ile BIREBIR (geriye uyum).
            if unavailable:
                payload["unavailable"] = unavailable
            yield {
                "event": "quotes",
                "data": json.dumps(payload, default=str),
            }

        async for quotes in store.subscribe(symbol_filter):
            if await request.is_disconnected():
                break
            state = market_state()
            payload = _sse_quotes_payload(quotes, symbol_filter, state)
            if payload:
                yield {
                    "event": "quotes",
                    "data": json.dumps({"market": state, "quotes": payload}, default=str),
                }
    finally:
        _sse_clients -= 1
        metrics.SSE_CLIENTS.set(_sse_clients)


@app.get(
    "/stream",
    tags=["Akis"],
    summary="SSE canli fiyat akisi",
    description=(
        "SSE (text/event-stream) canli fiyat akisi. Baglaninca ILK `event: quotes` "
        "payload'i tam snapshot'tir. `symbols=` ile filtrelenmis baglantida, "
        "karsilanamayan semboller ilk snapshot'a `unavailable: [{symbol, reason}]` "
        "olarak eklenir (reason: invalid_format | negative_cache | fetch_failed). "
        "Sonraki update event'leri bu alani TASIMAZ. `symbols` verilmezse (tum-liste) "
        "veya hepsi karsilanirsa `unavailable` alani hic bulunmaz (geriye uyumlu)."
    ),
    dependencies=[Depends(require_api_key)],
)
async def stream(
    request: Request,
    symbols: str | None = Query(default=None, description="Virgulle ayrilmis; bos ise tum liste"),
) -> EventSourceResponse:
    if _sse_clients >= settings.max_sse_clients:
        raise HTTPException(status_code=503, detail="SSE baglanti limiti dolu.")

    valid, invalid = _parse_symbols_lenient(symbols)
    # on-demand fetch DoS yuzeyini + unavailable[] boyutunu sinirla: gecerli +
    # gecersiz toplami /quotes ile ayni tavana tabi.
    _check_symbol_limit(valid + invalid)

    # symbols parametresi VARSA filtre daima set olur (bos olabilir: hepsi
    # gecersiz); None yalniz parametre HIC verilmediginde (tum-liste modu).
    symbol_filter = frozenset(valid) if symbols else None

    # ping birimi SANIYEDIR (sse-starlette): 15 sn'de bir keep-alive yorumu
    # gonderilir; market kapaliyken (veri akmazken) proxy idle-timeout'larinin
    # baglantiyi koparmasini onler. (Onceki deger 15000 idi = ~4 saat.)
    return EventSourceResponse(_stream_generator(request, symbol_filter, tuple(invalid)), ping=15)


@app.get("/demo", response_class=HTMLResponse, tags=["Sistem"], summary="Canli test sayfasi")
async def demo() -> str:
    if not settings.demo_enabled:
        raise HTTPException(status_code=404, detail="Demo devre disi.")
    return _DEMO_HTML


_DEMO_HTML = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>BIST Canli API - Demo</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0b0e11; color: #e6e8ea; }
  header { padding: 16px 20px; border-bottom: 1px solid #1c2127; }
  h1 { font-size: 18px; margin: 0; }
  .sub { color: #8a929b; font-size: 13px; margin-top: 4px; }
  .bar { padding: 12px 20px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  input { background: #12161b; border: 1px solid #2a313a; color: #e6e8ea; padding: 8px 10px; border-radius: 8px; width: 340px; }
  button { background: #2f81f7; border: 0; color: white; padding: 8px 14px; border-radius: 8px; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: right; padding: 8px 20px; border-bottom: 1px solid #161b22; font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  .up { color: #3fb950; } .down { color: #f85149; } .flat { color: #8a929b; }
  .flash { animation: fl .8s ease; }
  @keyframes fl { from { background: #1b2a17; } to { background: transparent; } }
  #status { font-size: 13px; color: #8a929b; margin-left: auto; }
</style>
</head>
<body>
<header>
  <h1>BIST Canli API - Demo</h1>
  <div class="sub">Yahoo + Is Yatirim, ~15 dk gecikmeli. SSE (pub/sub) ile canli.</div>
</header>
<div class="bar">
  <input id="symbols" placeholder="THYAO,GARAN (bos = tum liste)" value="THYAO,GARAN,ASELS,AKBNK,SISE,KCHOL,TUPRS,BIMAS" />
  <button onclick="connect()">Baglan</button>
  <span id="status">baglanmadi</span>
</div>
<table>
  <thead><tr><th>Sembol</th><th>Fiyat</th><th>Degisim</th><th>%</th><th>Hacim</th><th>Guncelleme</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<script>
let es = null; const rows = {};
function fmt(n, d=2){ return (n===null||n===undefined)?'-':Number(n).toLocaleString('tr-TR',{minimumFractionDigits:d,maximumFractionDigits:d}); }
function connect(){
  if (es) es.close();
  const syms = document.getElementById('symbols').value.trim();
  const url = '/stream' + (syms ? ('?symbols='+encodeURIComponent(syms)) : '');
  es = new EventSource(url);
  document.getElementById('status').textContent = 'baglaniyor...';
  es.onopen = () => document.getElementById('status').textContent = 'canli';
  es.onerror = () => document.getElementById('status').textContent = 'baglanti hatasi (yeniden deneniyor)';
  es.addEventListener('quotes', (e) => { const d = JSON.parse(e.data); for (const [s,q] of Object.entries(d.quotes)) render(s,q); });
}
function render(s, q){
  let tr = rows[s];
  if (!tr){ tr = document.createElement('tr');
    tr.innerHTML = '<td>'+s+'</td><td class="px"></td><td class="ch"></td><td class="pc"></td><td class="vol"></td><td class="ts"></td>';
    document.getElementById('rows').appendChild(tr); rows[s]=tr; }
  const cls = q.change>0?'up':(q.change<0?'down':'flat'); const sign = q.change>0?'+':'';
  tr.querySelector('.px').textContent = fmt(q.price);
  tr.querySelector('.ch').textContent = sign+fmt(q.change); tr.querySelector('.ch').className='ch '+cls;
  tr.querySelector('.pc').textContent = sign+fmt(q.change_percent)+'%'; tr.querySelector('.pc').className='pc '+cls;
  tr.querySelector('.vol').textContent = fmt(q.volume,0);
  tr.querySelector('.ts').textContent = q.updated_at ? new Date(q.updated_at).toLocaleTimeString('tr-TR') : '-';
  tr.classList.remove('flash'); void tr.offsetWidth; tr.classList.add('flash');
}
connect();
</script>
</body>
</html>"""
