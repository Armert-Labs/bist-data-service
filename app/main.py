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
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
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
from .config import settings
from .deps import fetch_semaphore, limiter, require_api_key
from .logging_config import set_request_id, setup_logging
from .market import is_market_open, market_state
from .models import HistoryResponse, Quote
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
_all_cache: dict[tuple[str, str], tuple[float, bytes]] = {}

# Negatif onbellek: kaynaklarda BULUNAMAYAN semboller icin symbol -> monotonic
# son kullanma. Ayni gecersiz sembole tekrarli istekler upstream'e gitmez
# (Yahoo rate-limit'ine karsi kalkan; updater'in IP'sini korur).
_negative_cache: dict[str, float] = {}
_NEGATIVE_CACHE_MAX = 4096


def _negative_cache_has(symbol: str) -> bool:
    expiry = _negative_cache.get(symbol)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _negative_cache.pop(symbol, None)
        return False
    return True


def _negative_cache_add(symbol: str) -> None:
    if len(_negative_cache) >= _NEGATIVE_CACHE_MAX:
        now = time.monotonic()
        expired = [s for s, exp in _negative_cache.items() if exp <= now]
        for s in expired:
            _negative_cache.pop(s, None)
        if len(_negative_cache) >= _NEGATIVE_CACHE_MAX:
            _negative_cache.clear()  # kotu niyetli benzersiz-sembol seli: sifirla
    _negative_cache[symbol] = time.monotonic() + settings.negative_cache_ttl


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Servis baslatiliyor (v%s), redis=%s", __version__, settings.redis_enabled)

    # Kimlik dogrulama durumu uyarilari (fail-safe farkindaligi)
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


def _rate_limit_response():
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=429, content={"detail": "Istek limiti asildi."})


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


async def _get_or_fetch(symbol: str) -> Quote | None:
    cached = await store.get_quote(symbol)
    if cached is not None:
        return cached
    if _negative_cache_has(symbol):  # yakin zamanda bulunamadi; upstream'i dovme
        return None
    async with fetch_semaphore:  # bounded: thread-pool / DoS korumasi
        # semaphore beklerken baskasi doldurmus olabilir:
        cached = await store.get_quote(symbol)
        if cached is not None:
            return cached
        quotes = await aggregator.fetch_quotes([symbol])
    quote = quotes.get(symbol)
    if quote is None:
        _negative_cache_add(symbol)
        return None
    quote.market_state = market_state()
    await store.set_quote(symbol, quote)
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
    store_ok = await store.ping()
    size = await store.size() if store_ok else 0
    stale = await store.is_stale()
    age = await store.staleness_seconds()
    ready_flag = store_ok and size > 0 and not stale

    body = {
        "ready": ready_flag,
        "store_ok": store_ok,
        "quotes_cached": size,
        "is_stale": stale,
        "last_update_age_seconds": round(age, 1) if age is not None else None,
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
    cache_key = (sort, order)
    now = time.monotonic()
    hit = _all_cache.get(cache_key)
    if hit is not None and (now - hit[0]) < settings.all_cache_ttl:
        return Response(content=hit[1], media_type="application/json")

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
    payload = {
        "market": market_state(),
        "count": len(quotes),
        "last_update": last_update.isoformat() if last_update else None,
        "is_stale": await store.is_stale(),
        "delayed": True,
        "quotes": [q.model_dump(mode="json") for q in quotes],
    }
    body = json.dumps(payload, default=str).encode("utf-8")
    _all_cache[cache_key] = (now, body)
    return Response(content=body, media_type="application/json")


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
    return quote


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

    if not requested:
        data = await store.get_all()
        return {
            "count": len(data),
            "market": market_state(),
            "quotes": {s: q.model_dump(mode="json") for s, q in data.items()},
        }

    result = await store.get_quotes(requested)
    missing = [s for s in requested if s not in result and not _negative_cache_has(s)]
    if missing:
        async with fetch_semaphore:
            fetched = await aggregator.fetch_quotes(missing)
        state = market_state()
        for s, q in fetched.items():
            q.market_state = state
            await store.set_quote(s, q)
            result[s] = q
        for s in missing:
            if s not in fetched:
                _negative_cache_add(s)

    return {
        "count": len(result),
        "market": market_state(),
        "quotes": {s: q.model_dump(mode="json") for s, q in result.items()},
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

    result = await aggregator.fetch_history(sym.normalize(symbol), period, interval)
    if not result.bars:
        raise HTTPException(
            status_code=404, detail=f"Gecmis veri bulunamadi: {sym.normalize(symbol)}"
        )
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

    primary = await store.get_quotes(syms)
    missing = [s for s in syms if s not in primary]
    if missing:
        async with fetch_semaphore:
            primary.update(await aggregator.fetch_quotes(missing))

    # Her bagimsiz referans kaynagi ayri ayri cek (biri erisilemezse digeri kalir).
    references: dict[str, dict] = {}
    reference_status: dict[str, str] = {}
    for name in settings.validate_providers:
        provider = aggregator.get_provider(name)
        if provider is None:
            reference_status[name] = "yapilandirilmamis"
            references[name] = {}
            continue
        try:
            # Erisilemeyen kaynak (orn. isyatirim yurtdisi IP) /validate'i kilitlemesin.
            fetched = await asyncio.wait_for(provider.fetch_quotes(syms), timeout=8.0)
            references[name] = fetched
            reference_status[name] = "ok" if fetched else "veri_yok"
        except TimeoutError:
            references[name] = {}
            reference_status[name] = "erisilemedi: timeout"
        except Exception as exc:
            references[name] = {}
            reference_status[name] = f"erisilemedi: {type(exc).__name__}"

    comparisons = []
    max_dev = 0.0
    any_compared = False
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
            if pp is not None and rp is not None and rp != 0:
                dev = abs(pp - rp) / rp * 100.0
                max_dev = max(max_dev, dev)
                any_compared = True
                row["references"][name] = {
                    "price": rp,
                    "deviation_pct": round(dev, 3),
                    "ok": dev < 1.0,
                }
            else:
                row["references"][name] = {"price": rp, "deviation_pct": None, "ok": False}
        comparisons.append(row)

    consistent = any_compared and max_dev < 1.0
    metrics.CROSS_SOURCE_DRIFT.set(round(max_dev, 3))
    metrics.VALIDATION_CONSISTENT.set(1 if consistent else 0)

    return {
        "checked": len(syms),
        "reference_status": reference_status,
        "max_deviation_pct": round(max_dev, 3),
        "consistent": consistent,
        "note": "erisilemeyen referanslar (orn. isyatirim yurtdisi IP'den) reference_status'ta gorulur.",
        "comparisons": comparisons,
    }


@app.get(
    "/stream",
    tags=["Akis"],
    summary="SSE canli fiyat akisi",
    dependencies=[Depends(require_api_key)],
)
async def stream(
    request: Request,
    symbols: str | None = Query(default=None, description="Virgulle ayrilmis; bos ise tum liste"),
) -> EventSourceResponse:
    global _sse_clients
    if _sse_clients >= settings.max_sse_clients:
        raise HTTPException(status_code=503, detail="SSE baglanti limiti dolu.")

    requested = set(_parse_symbols(symbols))

    def _filter(quotes: list[Quote]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for q in quotes:
            if not requested or q.symbol in requested:
                out[q.symbol] = q.model_dump(mode="json")
        return out

    async def event_generator():
        global _sse_clients
        _sse_clients += 1
        metrics.SSE_CLIENTS.set(_sse_clients)
        try:
            # 1) Ilk snapshot
            initial = await store.get_all()
            snapshot = _filter(list(initial.values()))
            if snapshot:
                yield {
                    "event": "quotes",
                    "data": json.dumps({"market": market_state(), "quotes": snapshot}, default=str),
                }

            # 2) Canli akis (store pub/sub)
            async for quotes in store.subscribe():
                if await request.is_disconnected():
                    break
                payload = _filter(quotes)
                if payload:
                    yield {
                        "event": "quotes",
                        "data": json.dumps(
                            {"market": market_state(), "quotes": payload}, default=str
                        ),
                    }
        finally:
            _sse_clients -= 1
            metrics.SSE_CLIENTS.set(_sse_clients)

    # ping birimi SANIYEDIR (sse-starlette): 15 sn'de bir keep-alive yorumu
    # gonderilir; market kapaliyken (veri akmazken) proxy idle-timeout'larinin
    # baglantiyi koparmasini onler. (Onceki deger 15000 idi = ~4 saat.)
    return EventSourceResponse(event_generator(), ping=15)


@app.get("/demo", response_class=HTMLResponse, tags=["Sistem"], summary="Canli test sayfasi")
async def demo() -> str:
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
