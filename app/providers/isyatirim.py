"""Is Yatirim veri saglayici (bagimsiz fallback + dogrulama kaynagi).

Endpoint (resmi olmayan, herkese acik):
  https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/
  HisseTekil?hisse=THYAO&startdate=01-01-2026&enddate=05-07-2026.json

Donen `value[]` gunluk cubuklar icerir: HGDG_KAPANIS/MIN/MAX/HACIM/ACILIS.

ONEMLI: Is Yatirim, Turkiye disindaki IP'lerden gelen baglantilari firewall'da
dusurebilir (TCP timeout). Bu durumda:
  - Servis TR'de deploy edilirse dogrudan calisir, VEYA
  - ISYATIRIM_PROXY ile TR cikisli bir proxy verilir (Yahoo dogrudan kalir).
Erisim yoksa circuit breaker bu kaynagi gecici devre disi birakir; sistem
Yahoo ile kesintisiz devam eder.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from .. import symbols as sym
from ..config import settings
from ..market import market_close_time
from ..models import HistoryBar, HistoryResponse, Quote
from .base import Provider

logger = logging.getLogger(__name__)

_BASE = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/HisseTekil"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; bist-canli-api/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.isyatirim.com.tr/",
}


def _f(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y")
    except ValueError:
        return None


def parse_quote(bist_symbol: str, rows: list[dict], now: datetime | None = None) -> Quote | None:
    """value[] satirlarindan Quote uretir. AGSIZ; test edilebilir saf fonksiyon."""
    rows = [r for r in (rows or []) if _f(r.get("HGDG_KAPANIS")) is not None]
    if not rows:
        return None

    # Is Yatirim `value[]` dizisini KRONOLOJIK sirali dondurmez (ayni istekte
    # bile sira degisebilir). "Son fiyat" olarak dizinin son elemanini almak,
    # eski bir gunluk cubugu secip /validate'te sahte sapma dogurur. Tarihe gore
    # sirala; tarihsiz/bozuk satirlar en eskiye itilir (son cubuk olarak secilmez).
    rows.sort(key=lambda r: _parse_date(r.get("HGDG_TARIH")) or datetime.min)

    last = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    price = _f(last.get("HGDG_KAPANIS"))
    if price is None:
        return None
    previous_close = _f(prev.get("HGDG_KAPANIS")) if prev else None

    change = change_percent = None
    if previous_close is not None and previous_close != 0:
        change = round(price - previous_close, 4)
        change_percent = round((price - previous_close) / previous_close * 100.0, 2)

    # H2: bu kaynak gunluk EOD cubuk dondurur; son cubugun tarihi exchange_time'a
    # tasinmazsa seans-ici bayat-bar guard'i (market.is_stale_bar) bu kaynak icin
    # hicbir zaman devreye giremez (exchange_time=None her zaman "taze" sayilir).
    last_date = _parse_date(last.get("HGDG_TARIH"))
    moment = now or datetime.now(UTC)
    exchange_time = min(market_close_time(last_date.date()), moment) if last_date else None

    return Quote(
        symbol=bist_symbol,
        price=round(price, 4),
        previous_close=round(previous_close, 4) if previous_close is not None else None,
        change=change,
        change_percent=change_percent,
        open=_f(last.get("HGDG_ACILIS")),
        day_high=_f(last.get("HGDG_MAX")),
        day_low=_f(last.get("HGDG_MIN")),
        volume=int(_f(last.get("HGDG_HACIM")) or 0) or None,
        currency="TRY",
        source="isyatirim",
        delayed=True,
        updated_at=datetime.now(UTC),
        exchange_time=exchange_time,
    )


class IsYatirimProvider(Provider):
    name = "isyatirim"

    def __init__(self, concurrency: int | None = None) -> None:
        self._sem = asyncio.Semaphore(concurrency or settings.isyatirim_concurrency)
        self._proxy = settings.isyatirim_proxy or None
        self._timeout = settings.isyatirim_timeout
        self._retries = settings.isyatirim_retries

    def _url(self, bist_symbol: str) -> str:
        end = datetime.now()
        start = end - timedelta(days=10)
        # NOT: Eskiden enddate sonuna ".json" eklenirdi; Is Yatirim API'si
        # artik bunu tarihe dahil edip LocalDate parse hatasi veriyor. ".json"
        # olmadan JSON doner ({"ok":true,"value":[...]}).
        return f"{_BASE}?hisse={bist_symbol}&startdate={start:%d-%m-%Y}&enddate={end:%d-%m-%Y}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, proxy=self._proxy, trust_env=True)

    async def _get_rows(self, client: httpx.AsyncClient, bist: str) -> list[dict]:
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._sem:
                    resp = await client.get(self._url(bist), headers=_HEADERS)
                resp.raise_for_status()
                return resp.json().get("value") or []
            except Exception as exc:
                last_exc = exc
                if attempt < self._retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        if last_exc:
            raise last_exc
        return []

    async def _fetch_one(self, client: httpx.AsyncClient, symbol: str) -> Quote | None:
        bist = sym.normalize(symbol)
        rows = await self._get_rows(client, bist)
        return parse_quote(bist, rows)

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        clean = [sym.normalize(s) for s in symbols if sym.is_valid_symbol(s)]
        if not clean:
            return {}
        out: dict[str, Quote] = {}
        async with self._client() as client:
            results = await asyncio.gather(
                *(self._fetch_one(client, s) for s in clean),
                return_exceptions=True,
            )
        for s, res in zip(clean, results, strict=False):
            if isinstance(res, Quote):
                out[s] = res
            elif isinstance(res, Exception):
                logger.debug("isyatirim %s hata: %s", s, res)
        return out

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        bist = sym.normalize(symbol)
        bars: list[HistoryBar] = []
        try:
            async with self._client() as client:
                rows = await self._get_rows(client, bist)
        except Exception as exc:
            logger.warning("isyatirim history %s hata: %s", bist, exc)
            rows = []

        for r in rows:
            close = _f(r.get("HGDG_KAPANIS"))
            if close is None:
                continue
            raw_date = r.get("HGDG_TARIH")
            if not isinstance(raw_date, str):
                continue
            try:
                ts = datetime.strptime(raw_date, "%d-%m-%Y").replace(tzinfo=UTC)
            except ValueError:
                continue
            bars.append(
                HistoryBar(
                    time=ts,
                    open=_f(r.get("HGDG_ACILIS")),
                    high=_f(r.get("HGDG_MAX")),
                    low=_f(r.get("HGDG_MIN")),
                    close=round(close, 4),
                    volume=int(_f(r.get("HGDG_HACIM")) or 0) or None,
                )
            )
        return HistoryResponse(symbol=bist, period=period, interval="1d", bars=bars)
