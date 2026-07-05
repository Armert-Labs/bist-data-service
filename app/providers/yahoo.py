"""Yahoo Finance (yfinance) tabanli veri saglayici.

BIST hisseleri icin ~15 dakika gecikmeli, halka acik veriyi ceker.
- fetch_quotes(): coklu sembolu tek batch istekte anlik goruntu olarak ceker.
- fetch_history(): tek sembol icin gecmis OHLCV cubuklarini ceker.

Tum yfinance cagrilari senkrondur; cagiran taraf (updater / API) bunlari
`asyncio.to_thread` ile sararak event loop'u bloke etmemelidir.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pandas as pd
import yfinance as yf

from .. import symbols as sym
from ..models import HistoryBar, HistoryResponse, Quote
from .base import Provider

logger = logging.getLogger(__name__)


def _safe_float(value, ndigits: int | None = None) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        if pd.isna(f):
            return None
        return round(f, ndigits) if ndigits is not None else f
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    f = _safe_float(value)
    return int(f) if f is not None else None


def _quote_from_frame(bist_symbol: str, df: pd.DataFrame | None) -> Quote | None:
    if df is None or getattr(df, "empty", True):
        return None
    df = df.dropna(how="all")
    if df.empty:
        return None

    last = df.iloc[-1]
    price = _safe_float(last.get("Close"), 4)
    if price is None:
        return None

    previous_close = _safe_float(df.iloc[-2].get("Close"), 4) if len(df) >= 2 else None

    change = None
    change_percent = None
    if previous_close is not None and previous_close != 0:
        change = round(price - previous_close, 4)
        change_percent = round((price - previous_close) / previous_close * 100.0, 2)

    return Quote(
        symbol=bist_symbol,
        price=price,
        previous_close=previous_close,
        change=change,
        change_percent=change_percent,
        open=_safe_float(last.get("Open"), 4),
        day_high=_safe_float(last.get("High"), 4),
        day_low=_safe_float(last.get("Low"), 4),
        volume=_safe_int(last.get("Volume")),
        currency="TRY",
        source="yahoo",
        delayed=True,
        updated_at=datetime.now(UTC),
    )


def fetch_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Verilen sembol listesi icin anlik (gecikmeli) fiyat goruntusu doner.

    Donen sozluk {BIST_SEMBOL: Quote} bicimindedir. Veri bulunamayan
    semboller sonuca dahil edilmez.
    """
    clean = [sym.normalize(s) for s in symbols if sym.is_valid_symbol(s)]
    clean = list(dict.fromkeys(clean))  # tekrar edenleri sirayi bozmadan at
    if not clean:
        return {}

    yahoo_syms = [sym.to_yahoo(s) for s in clean]
    result: dict[str, Quote] = {}

    try:
        data = yf.download(
            tickers=" ".join(yahoo_syms),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        logger.warning("yf.download hatasi (%d sembol): %s", len(yahoo_syms), exc)
        return {}

    if data is None or len(data) == 0:
        return {}

    # Tek sembolde yfinance tek seviyeli kolon dondurur.
    if len(yahoo_syms) == 1:
        bist = sym.from_yahoo(yahoo_syms[0])
        quote = _quote_from_frame(bist, data)
        if quote is not None:
            result[bist] = quote
        return result

    # Coklu sembol: kolonlar (ticker, alan) MultiIndex'idir.
    try:
        available = set(data.columns.get_level_values(0))
    except (AttributeError, IndexError):
        available = set()

    for ysym in yahoo_syms:
        if ysym not in available:
            continue
        bist = sym.from_yahoo(ysym)
        try:
            quote = _quote_from_frame(bist, data[ysym])
        except Exception as exc:
            logger.debug("%s ayristirma hatasi: %s", ysym, exc)
            continue
        if quote is not None:
            result[bist] = quote

    return result


def fetch_history(symbol: str, period: str = "1mo", interval: str = "1d") -> HistoryResponse:
    """Tek bir sembol icin gecmis OHLCV verisini doner."""
    bist = sym.normalize(symbol)
    yahoo_symbol = sym.to_yahoo(bist)
    bars: list[HistoryBar] = []

    try:
        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=False)
    except Exception as exc:
        logger.warning("history hatasi %s: %s", yahoo_symbol, exc)
        df = None

    if df is not None and not df.empty:
        for idx, row in df.iterrows():
            ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            bars.append(
                HistoryBar(
                    time=ts,
                    open=_safe_float(row.get("Open"), 4),
                    high=_safe_float(row.get("High"), 4),
                    low=_safe_float(row.get("Low"), 4),
                    close=_safe_float(row.get("Close"), 4),
                    volume=_safe_int(row.get("Volume")),
                )
            )

    return HistoryResponse(symbol=bist, period=period, interval=interval, bars=bars)


class YahooProvider(Provider):
    """yfinance senkron cagrilarini thread-pool'da calistiran async saglayici."""

    name = "yahoo"

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return await asyncio.to_thread(fetch_quotes, symbols)

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        return await asyncio.to_thread(fetch_history, symbol, period, interval)
