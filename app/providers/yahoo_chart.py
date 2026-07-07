"""Yahoo Finance chart API (v8) saglayici.

yfinance'in kullandigi 'download' endpoint'inden FARKLI bir Yahoo endpoint'idir
(query1/v8/finance/chart). Iki amaci vardir:

1) Fallback: yfinance/download bozulursa (Yahoo bunu sik degistirir) bu daha
   stabil endpoint devreye girer -> fiyat akisi kesilmez.
2) Dogrulama: /validate ucunda, birincil veriyle karsilastirilacak bagimsiz
   (farkli kod yolu) referans olarak kullanilir.

Auth/crumb gerektirmez. Tek sembol calisir; bounded concurrency ile toplanir.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from .. import symbols as sym
from ..models import HistoryBar, HistoryResponse, Quote
from .base import Provider

logger = logging.getLogger(__name__)

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; bist-canli-api/1.0)"}


def _r(value, ndigits: int = 4) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


class YahooChartProvider(Provider):
    name = "yahoo_chart"

    def __init__(self, concurrency: int = 8) -> None:
        self._sem = asyncio.Semaphore(concurrency)

    async def _fetch_one(self, client: httpx.AsyncClient, symbol: str) -> Quote | None:
        bist = sym.normalize(symbol)
        yahoo_symbol = sym.to_yahoo(bist)
        async with self._sem:
            resp = await client.get(
                f"{_BASE}{yahoo_symbol}",
                params={"range": "5d", "interval": "1d"},
                headers=_HEADERS,
            )
        resp.raise_for_status()
        result = (resp.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        meta = result.get("meta", {})
        price = _r(meta.get("regularMarketPrice"))
        if price is None:
            return None
        prev = _r(meta.get("chartPreviousClose") or meta.get("previousClose"))

        change = change_percent = None
        if prev is not None and prev != 0:
            change = round(price - prev, 4)
            change_percent = round((price - prev) / prev * 100.0, 2)

        return Quote(
            symbol=bist,
            price=price,
            previous_close=prev,
            change=change,
            change_percent=change_percent,
            open=_r(meta.get("regularMarketOpen")),
            day_high=_r(meta.get("regularMarketDayHigh")),
            day_low=_r(meta.get("regularMarketDayLow")),
            volume=int(meta.get("regularMarketVolume"))
            if meta.get("regularMarketVolume")
            else None,
            currency=meta.get("currency", "TRY"),
            source="yahoo_chart",
            delayed=True,
            updated_at=datetime.now(UTC),
            exchange_time=datetime.fromtimestamp(meta["regularMarketTime"], tz=UTC)
            if meta.get("regularMarketTime")
            else None,
        )

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        clean = [sym.normalize(s) for s in symbols if sym.is_valid_symbol(s)]
        if not clean:
            return {}
        out: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            results = await asyncio.gather(
                *(self._fetch_one(client, s) for s in clean),
                return_exceptions=True,
            )
        for s, res in zip(clean, results, strict=False):
            if isinstance(res, Quote):
                out[s] = res
            elif isinstance(res, Exception):
                logger.debug("yahoo_chart %s hata: %s", s, res)
        return out

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        bist = sym.normalize(symbol)
        yahoo_symbol = sym.to_yahoo(bist)
        bars: list[HistoryBar] = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{_BASE}{yahoo_symbol}",
                    params={"range": period, "interval": interval},
                    headers=_HEADERS,
                )
            resp.raise_for_status()
            result = (resp.json().get("chart", {}).get("result") or [None])[0]
        except Exception as exc:
            logger.warning("yahoo_chart history %s: %s", yahoo_symbol, exc)
            result = None

        if result:
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            opens, highs = quote.get("open") or [], quote.get("high") or []
            lows, closes = quote.get("low") or [], quote.get("close") or []
            volumes = quote.get("volume") or []
            for idx, ts in enumerate(timestamps):
                close = closes[idx] if idx < len(closes) else None
                if close is None:
                    continue
                vol = volumes[idx] if idx < len(volumes) else None
                bars.append(
                    HistoryBar(
                        time=datetime.fromtimestamp(ts, tz=UTC),
                        open=_r(opens[idx] if idx < len(opens) else None),
                        high=_r(highs[idx] if idx < len(highs) else None),
                        low=_r(lows[idx] if idx < len(lows) else None),
                        close=_r(close),
                        volume=int(vol) if vol else None,
                    )
                )

        return HistoryResponse(symbol=bist, period=period, interval=interval, bars=bars)
