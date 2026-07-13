"""TradingView scanner saglayici (resmi olmayan, herkese acik endpoint).

Yahoo ve Is Yatirim gibi bu proje zaten resmi-olmayan kaynaklar kullanir;
TradingView scanner de ayni cizgide bagimsiz bir kaynak/dogrulama saglar.

Endpoint (quotes):
  POST https://scanner.tradingview.com/turkey/scan
  Govde: {"symbols":{"tickers":["BIST:THYAO","BIST:GARAN"],"query":{"types":[]}},
          "columns":["lp","change_abs","change","volume","open","high","low","close"]}
  Yanit: {"data":[{"s":"BIST:THYAO","d":[lp,change_abs,change,volume,open,high,low,close]}, ...]}

Sembol esleme: bizim "THYAO" <-> TradingView "BIST:THYAO".
Coklu sembol TEK POST'ta gonderilir (batch; Yahoo download gibi verimli).
Scanner /scan zaman serisi sunmaz; fetch_history bos doner (durust davranis).

NOT (M1 denetimi): "prev_close_price" kolonu bu /scan uc noktasinda desteklenmiyor
(her zaman null donuyordu, ayni sekilde eski "ch"/"chp" isimleri de calismiyordu).
Dogru/calisan kolonlar "change_abs" (mutlak degisim) ve "change" (yuzde degisim);
previous_close bunlardan AYNI yanit icinde turetilir (price - change_abs) --
baska bir kaynaga/uca gidilmez. change_abs de null gelirse previous_close
ACIKCA None birakilir + loglanir (bu, canliya cikmadan dogrulanamamis bir
varsayimdir; sonraki canli denetimde teyit edilmeli).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from .. import symbols as sym
from ..models import HistoryResponse, Quote
from .base import Provider

logger = logging.getLogger(__name__)

_SCANNER_URL = "https://scanner.tradingview.com/turkey/scan"
# Kolon sirasi yanit `d[]` dizisiyle birebir eslesir (index -> alan).
# lp=last price (canli, piyasa kapaliyken null); close=son kapanis (fallback).
# change_abs/change: mutlak/yuzde degisim (eski "ch"/"chp" isimleri calismiyordu).
_COLUMNS = ["lp", "change_abs", "change", "volume", "open", "high", "low", "close"]
_EXCHANGE = "BIST"
_TIMEOUT = 10.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; bist-canli-api/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}


def _f(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _bist_from_ticker(ticker: str) -> str:
    """'BIST:THYAO' -> 'THYAO'. Borsa oneki (: sonrasi) atilir."""
    return sym.normalize(ticker.split(":")[-1])


def parse_quote(row: dict, columns: list[str] | None = None) -> Quote | None:
    """Scanner satirindan ({s, d}) Quote uretir. AGSIZ; saf/test edilebilir.

    Gecersiz sembol, eksik alan veya fiyat yoksa None doner (sonuca girmez).
    """
    if not isinstance(row, dict):
        return None
    ticker = row.get("s")
    data = row.get("d")
    if not ticker or not isinstance(data, list):
        return None

    bist = _bist_from_ticker(ticker)
    if not sym.is_valid_symbol(bist):
        return None

    values = dict(zip(columns or _COLUMNS, data, strict=False))
    # Piyasa kapaliyken lp (canli son fiyat) null gelir; son kapanisa (close) dus.
    price = _f(values.get("lp"))
    if price is None:
        price = _f(values.get("close"))
    if price is None:
        return None

    change = _f(values.get("change_abs"))
    change_percent = _f(values.get("change"))
    volume = _f(values.get("volume"))

    previous_close: float | None = None
    if change is not None:
        # prev_close_price kolonu /scan'de calismiyor (hep null); ayni yanittaki
        # change_abs'den turetiyoruz -- baska bir uca/kaynaga GITMIYORUZ.
        previous_close = round(price - change, 4)
    else:
        logger.debug("tradingview %s: change_abs bos, previous_close hesaplanamiyor", bist)

    return Quote(
        symbol=bist,
        price=round(price, 4),
        previous_close=previous_close,
        change=round(change, 4) if change is not None else None,
        change_percent=round(change_percent, 2) if change_percent is not None else None,
        open=_f(values.get("open")),
        day_high=_f(values.get("high")),
        day_low=_f(values.get("low")),
        volume=int(volume) if volume is not None else None,
        currency="TRY",
        source="tradingview",
        delayed=True,
        updated_at=datetime.now(UTC),
    )


class TradingViewProvider(Provider):
    name = "tradingview"
    supports_history = False  # scanner /scan zaman serisi vermez

    def __init__(self, timeout: float | None = None) -> None:
        self._timeout = timeout or _TIMEOUT

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        clean = [sym.normalize(s) for s in symbols if sym.is_valid_symbol(s)]
        clean = list(dict.fromkeys(clean))  # tekrarlari sirayi bozmadan at
        if not clean:
            return {}

        body = {
            "symbols": {
                "tickers": [f"{_EXCHANGE}:{s}" for s in clean],
                "query": {"types": []},
            },
            "columns": _COLUMNS,
        }
        # Tek POST: coklu sembol batch olarak sorgulanir. Hata FIRLAR (aggregator
        # yakalar, circuit breaker'i tetikler) — surekli sessiz {} donmez.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(_SCANNER_URL, json=body, headers=_HEADERS)
        resp.raise_for_status()
        rows = resp.json().get("data") or []

        out: dict[str, Quote] = {}
        for row in rows:
            quote = parse_quote(row)
            if quote is not None:
                out[quote.symbol] = quote
        return out

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        # Scanner /scan yalnizca anlik goruntu verir; zaman serisi yoktur.
        # Bos yanit dondururuz (aggregator bir sonraki kaynaga gecer).
        return HistoryResponse(
            symbol=sym.normalize(symbol), period=period, interval=interval, bars=[]
        )
