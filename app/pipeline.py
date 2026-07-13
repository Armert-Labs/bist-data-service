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
from .market import is_market_open, is_stale_bar, market_state
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


def _pick_reference(
    symbol: str,
    own_source: str,
    provider_quotes: dict[str, dict[str, Quote]],
    now: datetime,
    *,
    record_metrics: bool = True,
) -> tuple[Quote | None, str | None]:
    """Ilk-kazanir sirayla bagimsiz referans secer.

    Quote'un KENDI kaynagi asla referans olarak secilmez (H3: totolojik
    dogrulama -- ayni kaynagin yeni bir cagrisi hemen hemen hep ayni fiyati
    dondurur, bu da sahte "tutarli" gorunumu verir). Bayat bar dondüren
    kaynaklar da elenir (H2 guard'i referans yolunda da gecerli).

    MEDIUM-5: aggregator seans icinde damgasiz (ne exchange_time NE bar_time)
    bir quote'u "guvenilmez" sayip TAMAMEN dusürür (bkz. aggregator.py HIGH-1
    guard'i). Ayni feed'in guvenilmez dedigi veri dogrulamanin REFERANSI
    olamaz -- aksi halde aggregator'in attigi veri, referans yolundan arka
    kapidan geri girip sahte bir "tutarli" sonucu uretebilirdi (asimetri).

    Bulunamazsa `(None, reason)` doner ve (LOW-3: `record_metrics=True` ise)
    `bist_validate_no_reference_total` sayacini reason etiketiyle artirir --
    cross_validate_quotes/run_drift_monitor/`/validate` AYNI cekirdegi
    kullandigi icin "temiz" ile "hic bakilamadi" tum caginda ayni sekilde
    raporlanir (review MEDIUM-1). `/validate` insan-teshis (operator poll'u)
    oldugu icin `record_metrics=False` gecer -- aksi halde bu salt-okunur
    endpoint'e yapilan her cagri, arka plan drift monitörünün gercek
    "referans bulunamadi" oranini sisirirdi (review LOW-3).
    """
    saw_stale = False
    saw_untimed = False
    market_open_now = is_market_open(now)
    for name in settings.validate_providers:
        if name == own_source:
            continue
        candidate = provider_quotes.get(name, {}).get(symbol)
        if candidate is None:
            continue
        if market_open_now and candidate.bar_time is None and candidate.exchange_time is None:
            saw_untimed = True
            continue
        # HIGH-4: bar_time TERCIH EDILIR (aggregator.py guard'iyla ayni
        # sozlesme) -- isyatirim/tradingview artik exchange_time'i hic
        # doldurmuyor, yalnizca exchange_time'a bakmak bu kaynaklar icin
        # bayat referanslarin hic elenmemesine yol acardi.
        if is_stale_bar(candidate.bar_time or candidate.exchange_time, now):
            saw_stale = True
            continue
        return candidate, None
    reason = "no_timestamp" if saw_untimed else ("stale" if saw_stale else "no_data")
    if record_metrics:
        metrics.VALIDATE_NO_REFERENCE.labels(reason=reason).inc()
    return None, reason


async def gather_reference_quotes(
    syms: list[str], fetch_timeout: float = 8.0
) -> tuple[dict[str, dict[str, Quote]], dict[str, str]]:
    """Yapilandirilmis TUM `validate_providers`'i syms icin cekip
    (provider -> {sembol: Quote}) + (provider -> durum metni) dondurur.

    `/validate` (insan-teshis) ve yazma yolu (cross_validate_quotes) / arka
    plan (run_drift_monitor) AYNI cekim mantigini kullanir -- eskiden 3 ayri
    implementasyon farkli sonuc/gauge degeri uretebiliyordu (review MEDIUM-1).
    """
    provider_quotes: dict[str, dict[str, Quote]] = {}
    status: dict[str, str] = {}
    for name in settings.validate_providers:
        provider = aggregator.get_provider(name)
        if provider is None:
            status[name] = "yapilandirilmamis"
            continue
        try:
            fetched = await asyncio.wait_for(provider.fetch_quotes(syms), timeout=fetch_timeout)
            provider_quotes[name] = fetched
            status[name] = "ok" if fetched else "veri_yok"
        except TimeoutError:
            status[name] = "erisilemedi: timeout"
        except Exception as exc:
            status[name] = f"erisilemedi: {type(exc).__name__}"
    return provider_quotes, status


def compare_against_references(
    primary: dict[str, Quote],
    syms: list[str],
    provider_quotes: dict[str, dict[str, Quote]],
    now: datetime | None = None,
    *,
    record_metrics: bool = True,
) -> dict:
    """Verdict cekirdegi: own-source-disli + bayat-filtreli referans
    (`_pick_reference`) ile max sapma/tutarlilik hesaplar.

    GAUGE YAZMAZ -- caller karar verir (run_drift_monitor yazar, `/validate`
    salt-okunur insan-teshis endpoint'i oldugu icin yazmaz; review MEDIUM-1).
    `record_metrics=False`: `bist_validate_no_reference_total` sayacina da
    yazma (LOW-3 -- `/validate` operator poll'u drift-monitörü sayacini
    sismesin).
    """
    moment = now or datetime.now(UTC)
    max_dev = 0.0
    any_compared = False
    for s in syms:
        p = primary.get(s)
        if p is None or p.price is None:
            continue
        ref, _reason = _pick_reference(
            s, p.source, provider_quotes, moment, record_metrics=record_metrics
        )
        if ref is None or ref.price is None or ref.price == 0:
            continue
        dev = abs(p.price - ref.price) / ref.price * 100.0
        max_dev = max(max_dev, dev)
        any_compared = True
    return {
        "checked": len(syms),
        "compared": any_compared,
        "max_deviation_pct": round(max_dev, 3),
        "consistent": any_compared and max_dev < settings.cross_validate_max_pct,
    }


async def cross_validate_quotes(
    quotes: dict[str, Quote], now: datetime | None = None
) -> dict[str, Quote]:
    """Birincil fiyatlari bagimsiz referans kaynaklarla karsilastirir."""
    if not quotes or not settings.write_cross_validate:
        return quotes

    syms = list(quotes.keys())
    moment = now or datetime.now(UTC)
    provider_quotes, _status = await gather_reference_quotes(syms)

    out: dict[str, Quote] = {}
    max_pct = settings.cross_validate_max_pct
    for s, q in quotes.items():
        ref, _reason = _pick_reference(s, q.source, provider_quotes, moment)
        if ref is None or ref.price is None or q.price is None or ref.price == 0:
            # Dogrulanamadi: bagimsiz referans yok (hepsi kendi kaynagi, bayat
            # veya erisilemez) -- SESSIZCE gecerli kabul edilir (fail-quiet;
            # bunu reddetmek fiyatin yanlis oldugu anlamina gelmez).
            out[s] = q
            continue
        dev = abs(q.price - ref.price) / ref.price * 100.0
        if dev <= max_pct:
            out[s] = q
        else:
            metrics.WRITE_VALIDATE_REJECTS.inc()
            logger.warning(
                "Yazma capraz-dogrulama reddi %s: primary=%.4f (%s) ref=%.4f (%s) (%%%.2f)",
                s,
                q.price,
                q.source,
                ref.price,
                ref.source,
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


async def run_drift_monitor(
    store: Store, symbols: list[str] | None = None, now: datetime | None = None
) -> dict:
    """Arka planda kaynaklar arasi sapma kontrolu (updater dongusu icin).

    HIGH-3/MEDIUM-1: own-source disli + bayat-filtreli ayni cekirdegi
    (`compare_against_references`) kullanir -- eskiden own-source disli
    olmadigi icin bir kaynak kendi kendini dogrulayip VALIDATION_CONSISTENT
    gauge'unu kalici 1'de birakabiliyordu. Bagimsiz referans hic yoksa
    gauge'lara YAZILMAZ (eski deger korunur; 0 yazmak "gercekten tutarsiz"
    ile "hic bakilamadi"yi ayirt edilemez hale getirirdi).
    """
    syms = symbols or settings.drift_monitor_symbols or DRIFT_MONITOR_SYMBOLS
    moment = now or datetime.now(UTC)
    primary = await store.get_quotes(syms)
    missing = [s for s in syms if s not in primary]
    if missing:
        fetched = await aggregator.fetch_quotes(missing)
        primary.update(fetched)

    provider_quotes, _status = await gather_reference_quotes(syms)
    result = compare_against_references(primary, syms, provider_quotes, moment)

    if result["compared"]:
        metrics.CROSS_SOURCE_DRIFT.set(result["max_deviation_pct"])
        metrics.VALIDATION_CONSISTENT.set(1 if result["consistent"] else 0)
        if not result["consistent"]:
            logger.warning(
                "Drift monitörü: kaynaklar arasi max sapma %%%.2f (esik %%%.1f)",
                result["max_deviation_pct"],
                settings.cross_validate_max_pct,
            )
            metrics.DRIFT_ALERTS.inc()

    return result
