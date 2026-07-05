"""Unified quote fetch, cross-validate, and commit pipeline.

Tum yazma yollari (updater + on-demand API) bu modul uzerinden gecmelidir;
boylece sanity-check ve capraz-kaynak dogrulama tutarli uygulanir.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from . import metrics
from .aggregator import aggregator
from .config import settings
from .market import market_state
from .models import Quote
from .store import Store

logger = logging.getLogger(__name__)

# Updater drift monitörü icin varsayilan likit semboller.
DRIFT_MONITOR_SYMBOLS = [
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


async def cross_validate_quotes(quotes: dict[str, Quote]) -> dict[str, Quote]:
    """Birincil fiyatlari bagimsiz referans kaynaklarla karsilastirir."""
    if not quotes or not settings.write_cross_validate:
        return quotes

    syms = list(quotes.keys())
    reference: dict[str, Quote] = {}
    for name in settings.validate_providers:
        provider = aggregator.get_provider(name)
        if provider is None:
            continue
        try:
            fetched = await asyncio.wait_for(provider.fetch_quotes(syms), timeout=8.0)
            for s, q in fetched.items():
                if s not in reference:
                    reference[s] = q
        except Exception as exc:
            logger.debug("Capraz-dogrulama kaynagi %s erisilemedi: %s", name, exc)

    if not reference:
        return quotes

    out: dict[str, Quote] = {}
    max_pct = settings.cross_validate_max_pct
    for s, q in quotes.items():
        ref = reference.get(s)
        if ref is None or ref.price is None or q.price is None or ref.price == 0:
            out[s] = q
            continue
        dev = abs(q.price - ref.price) / ref.price * 100.0
        if dev <= max_pct:
            out[s] = q
        else:
            metrics.WRITE_VALIDATE_REJECTS.inc()
            logger.warning(
                "Yazma capraz-dogrulama reddi %s: primary=%.4f ref=%.4f (%%%.2f)",
                s,
                q.price,
                ref.price,
                dev,
            )
    return out


async def fetch_quotes(
    store: Store,
    symbols: list[str],
    *,
    cross_validate: bool | None = None,
) -> dict[str, Quote]:
    """Store'daki onceki fiyatlarla sanity-check'li cekim."""
    if not symbols:
        return {}

    cached = await store.get_quotes(symbols)
    previous = {s: q.price for s, q in cached.items() if q.price is not None}
    quotes = await aggregator.fetch_quotes(symbols, previous=previous)

    do_validate = (
        settings.write_cross_validate_on_demand if cross_validate is None else cross_validate
    )
    if do_validate and quotes:
        quotes = await cross_validate_quotes(quotes)
    return quotes


async def commit_quotes(
    store: Store,
    quotes: dict[str, Quote],
    *,
    market: str | None = None,
) -> None:
    """Dogrulanmis fiyatlari store'a yazar (updated_at + market_state)."""
    if not quotes:
        return
    now = datetime.now(UTC)
    state = market or market_state()
    for q in quotes.values():
        q.updated_at = now
        q.market_state = state
    await store.set_quotes(quotes)


async def fetch_and_commit(
    store: Store,
    symbols: list[str],
    *,
    cross_validate: bool | None = None,
    market: str | None = None,
) -> dict[str, Quote]:
    """Tek adimda cek + (opsiyonel) capraz-dogrula + store'a yaz."""
    quotes = await fetch_quotes(store, symbols, cross_validate=cross_validate)
    if quotes:
        await commit_quotes(store, quotes, market=market)
    return quotes


async def run_drift_monitor(store: Store, symbols: list[str] | None = None) -> dict:
    """Arka planda kaynaklar arasi sapma kontrolu (updater dongusu icin)."""
    syms = symbols or settings.drift_monitor_symbols or DRIFT_MONITOR_SYMBOLS
    primary = await store.get_quotes(syms)
    missing = [s for s in syms if s not in primary]
    if missing:
        fetched = await aggregator.fetch_quotes(missing)
        primary.update(fetched)

    max_dev = 0.0
    any_compared = False
    for s in syms:
        p = primary.get(s)
        pp = p.price if p else None
        if pp is None:
            continue
        for name in settings.validate_providers:
            provider = aggregator.get_provider(name)
            if provider is None:
                continue
            try:
                ref = await asyncio.wait_for(provider.fetch_quotes([s]), timeout=8.0)
            except Exception:
                continue
            r = ref.get(s)
            rp = r.price if r else None
            if rp is not None and rp != 0:
                dev = abs(pp - rp) / rp * 100.0
                max_dev = max(max_dev, dev)
                any_compared = True

    consistent = any_compared and max_dev < settings.cross_validate_max_pct
    metrics.CROSS_SOURCE_DRIFT.set(round(max_dev, 3))
    metrics.VALIDATION_CONSISTENT.set(1 if consistent else 0)

    if any_compared and not consistent:
        logger.warning(
            "Drift monitörü: kaynaklar arasi max sapma %%%.2f (esik %%%.1f)",
            max_dev,
            settings.cross_validate_max_pct,
        )
        metrics.DRIFT_ALERTS.inc()

    return {
        "checked": len(syms),
        "max_deviation_pct": round(max_dev, 3),
        "consistent": consistent,
    }
