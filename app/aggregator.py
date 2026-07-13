"""Coklu kaynak orkestrasyonu: fallback + circuit breaker + sanity-check.

Strateji: Saglayicilar oncelik sirasiyla denenir. gapfill modunda her kaynak
eksik sembolleri tamamlar. Bir kaynak ard arda hata verirse circuit breaker onu
gecici devre disi birakir.

Sanity-check: onceki bilinen fiyata gore absurt sicramalar elenir.
Kapsam kontrolu: kismi/bos yanit basarisiz sayilir (circuit breaker tetiklenir).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from . import metrics
from .config import settings
from .market import is_market_open, is_stale_bar
from .models import HistoryResponse, Quote
from .providers.base import CircuitBreaker, Provider
from .providers.isyatirim import IsYatirimProvider
from .providers.tradingview import TradingViewProvider
from .providers.yahoo import YahooProvider
from .providers.yahoo_chart import YahooChartProvider
from .symbol_circuit import symbol_circuit

logger = logging.getLogger(__name__)

_FACTORY = {
    "yahoo": YahooProvider,
    "yahoo_chart": YahooChartProvider,
    "tradingview": TradingViewProvider,
    "isyatirim": IsYatirimProvider,
}


class Aggregator:
    def __init__(self) -> None:
        self._providers: list[tuple[Provider, CircuitBreaker]] = []
        # Sembol -> (ilk red, son red) monotonic. Kesintisiz red penceresini izler;
        # herhangi bir kabulde temizlenir, escape'ten uzun bosluk pencereyi sifirlar.
        self._sanity_reject_since: dict[str, tuple[float, float]] = {}
        for name in settings.providers:
            factory = _FACTORY.get(name)
            if factory is None:
                logger.warning("Bilinmeyen provider atlandi: %s", name)
                continue
            self._providers.append((factory(), CircuitBreaker(name)))
            metrics.PROVIDER_UP.labels(provider=name).set(1)
        if not self._providers:
            raise RuntimeError("Hic gecerli provider yapilandirilmadi (PROVIDERS).")

    @property
    def provider_states(self) -> dict[str, str]:
        return {p.name: cb.state for p, cb in self._providers}

    def _coverage_ok(self, fetched: dict[str, Quote], ask: list[str]) -> bool:
        if not ask:
            return True
        if not fetched:
            return False
        hit = sum(1 for s in ask if s in fetched)
        ratio = hit / len(ask)
        return ratio >= (settings.provider_min_coverage_pct / 100.0)

    def _is_sane(self, symbol: str, quote: Quote, previous: dict[str, float] | None) -> bool:
        prev = (previous or {}).get(symbol)
        if prev is None or prev == 0 or quote.price is None:
            self._sanity_reject_since.pop(symbol, None)
            return True
        change_pct = abs((quote.price - prev) / prev * 100.0)
        if change_pct <= settings.sanity_max_change_percent:
            self._sanity_reject_since.pop(symbol, None)
            return True

        escape = settings.sanity_reject_escape_seconds
        if escape > 0:
            now = time.monotonic()
            entry = self._sanity_reject_since.get(symbol)
            # Escape'ten uzun fetch boslugu "kesintisiz red" sayilmaz: gece/kesinti
            # sonrasi ilk bozuk tick'in aninda kacis almasini onler.
            if entry is None or now - entry[1] > escape:
                self._sanity_reject_since[symbol] = (now, now)
            else:
                self._sanity_reject_since[symbol] = (entry[0], now)

        metrics.SANITY_REJECTS.inc()
        logger.warning(
            "Sanity reddi %s: %%%.1f degisim (%.4f -> %.4f)",
            symbol,
            change_pct,
            prev,
            quote.price,
        )
        return False

    def _try_escape(self, symbol: str, candidates: list[Quote]) -> Quote | None:
        """Kesintisiz red penceresi dolduysa VE en az iki kaynak yeni fiyatta
        uzlasiyorsa ilk adayi kabul eder.

        Coklu-kaynak uzlasisi sarti: bedelsiz/split'te tum kaynaklar ayni yeni
        fiyati verir; tek bozuk kaynagin fiyati ise teyit alamaz (previous'i
        zehirleyip dogru fiyatlari reddettirme dongusunu engeller). Damga burada
        SILINMEZ: kabul commit'e donusurse bir sonraki turda dogal kabul temizler,
        donusmezse (orn. cross-validate dusurdu) pencere sifirlanmadan kalir.
        """
        escape = settings.sanity_reject_escape_seconds
        if escape <= 0 or len(candidates) < 2:
            return None
        entry = self._sanity_reject_since.get(symbol)
        now = time.monotonic()
        if entry is None or now - entry[0] < escape:
            return None
        base = candidates[0]
        if base.price is None:
            return None
        max_pct = settings.cross_validate_max_pct
        agree = sum(
            1
            for other in candidates[1:]
            if other.price is not None
            and abs(other.price - base.price) / base.price * 100.0 <= max_pct
        )
        if agree < 1:
            return None
        metrics.SANITY_ESCAPES.inc()
        logger.warning(
            "Sanity kacisi %s: %d kaynak yeni fiyatta uzlasti, %.0f sn kesintisiz "
            "red sonrasi kabul ediliyor (fiyat=%.4f)",
            symbol,
            agree + 1,
            now - entry[0],
            base.price,
        )
        return base

    def get_provider(self, name: str) -> Provider | None:
        for provider, _ in self._providers:
            if provider.name == name:
                return provider
        return None

    async def fetch_quotes(
        self,
        symbols: list[str],
        previous: dict[str, float] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Quote]:
        mode = settings.provider_mode.lower()
        gapfill = mode in ("gapfill", "hybrid")
        result: dict[str, Quote] = {}
        rejected: dict[str, list[Quote]] = {}
        remaining = list(symbols)
        min_cov = settings.provider_min_coverage_pct / 100.0
        moment = now or datetime.now(UTC)

        for provider, breaker in self._providers:
            if not remaining:
                break
            if not breaker.allow():
                logger.debug("%s devre disi (circuit=%s), atlaniyor", provider.name, breaker.state)
                continue

            ask = remaining if gapfill else symbols
            ask = [s for s in ask if symbol_circuit.allow(provider.name, s)]
            if not ask:
                continue

            metrics.FETCH_REQUESTS.labels(provider=provider.name).inc()
            try:
                fetched = await asyncio.wait_for(
                    provider.fetch_quotes(ask), settings.provider_fetch_timeout
                )
            except TimeoutError:
                # NOT: disaridan (guncelleme turu butcesi) gelen bir iptal burada
                # YAKALANMAZ — o CancelledError'dur (BaseException), Exception'dan
                # TUREMEZ, bu try/except'i atlayip yukari yayilir. Bu blok yalnizca
                # BU cagrinin KENDI ic zaman asimini (provider_fetch_timeout) yakalar.
                breaker.record_failure()
                metrics.FETCH_ERRORS.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                for s in ask:
                    symbol_circuit.record_failure(provider.name, s)
                logger.warning(
                    "%s fetch zaman asimi (%.0f sn), sonraki kaynaga dusuluyor",
                    provider.name,
                    settings.provider_fetch_timeout,
                )
                continue
            except Exception as exc:
                breaker.record_failure()
                metrics.FETCH_ERRORS.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                for s in ask:
                    symbol_circuit.record_failure(provider.name, s)
                logger.warning("%s fetch hatasi, sonraki kaynaga dusuluyor: %s", provider.name, exc)
                continue

            # H2: seans acikken bayat bar (dunku/eski veri noktasi) dondüren
            # sembolleri "hic gelmemis" say -- provider Quote ÜRETMEMIS gibi
            # davran, sonraki kaynaga (gapfill) dusulsun. Kapsama/circuit
            # muhasebesi (asagida) bu filtrelenmis fetched'e gore hesaplanir.
            guard_dropped: set[str] = set()
            market_open_now = is_market_open(moment)
            for s in [s for s, q in fetched.items() if is_stale_bar(q.exchange_time, moment)]:
                guard_dropped.add(s)
                metrics.STALE_BAR_SKIPPED.labels(provider=provider.name).inc()
                logger.warning(
                    "%s bayat bar: sembol=%s exchange_time=%s (simdi=%s, seans acik) — "
                    "quote atlandi",
                    provider.name,
                    s,
                    fetched[s].exchange_time,
                    moment.isoformat(),
                )
                del fetched[s]

            # HIGH-1: seans acikken exchange_time hic URETEMEYEN kaynak da ayni
            # kuraldan muaf olmamali -- canli olay: TradingView damgasiz oldugu
            # icin guard'dan tamamen kaciyor, 2 yillik bayat bir fiyati "taze"
            # diye servis ediyordu. Damgasiz veri seans icinde guvenilmez.
            for s in [s for s, q in fetched.items() if market_open_now and q.exchange_time is None]:
                guard_dropped.add(s)
                metrics.MISSING_EXCHANGE_TIME.labels(provider=provider.name).inc()
                logger.warning(
                    "%s exchange_time yok: sembol=%s (seans acik) — quote atlandi",
                    provider.name,
                    s,
                )
                del fetched[s]

            if self._coverage_ok(fetched, ask):
                breaker.record_success()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1)
            elif gapfill:
                # Gapfill'de fallback yalnizca onceki kaynaklarin bulamadigi (zor /
                # illikit) sembolleri sorar; dusuk kapsama provider sagligini DEGIL
                # sembol yoklugunu gosterir. Yanit geldi (exception yok) -> saglikli
                # say, circuit'i actirma. Gercek coku exception yolunda yakalanir.
                # Aksi halde illikit semboller fallback'leri bosuna devre disi birakip
                # legitim eksik sembollerin kapsamasini dusururdu.
                breaker.record_success()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1)
            else:
                hit = sum(1 for s in ask if s in fetched)
                ratio = hit / len(ask) if ask else 0.0
                breaker.record_failure()
                metrics.FETCH_PARTIAL.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                logger.warning(
                    "%s kismi yanit: %d/%d sembol (%%%.0f, esik %%%.0f)",
                    provider.name,
                    hit,
                    len(ask),
                    ratio * 100,
                    min_cov * 100,
                )

            for s in ask:
                if s in fetched:
                    symbol_circuit.record_success(provider.name, s)
                elif s in guard_dropped:
                    # MEDIUM-3: guard'in dusurdugu (bayat bar / damgasiz) sembol
                    # provider'in veri VEREMEDIGI anlamina gelmez -- "bugun
                    # islem gormedi/damga yok" bir politika karari. HATA
                    # yazilirsa 3 turda sembol_circuit acilir, gec islem goren
                    # bir hisse damgasi duzelince bile 300sn atlanmis olur.
                    continue
                else:
                    symbol_circuit.record_failure(provider.name, s)

            for s, q in fetched.items():
                if s in result:
                    continue
                if self._is_sane(s, q, previous):
                    result[s] = q
                else:
                    rejected.setdefault(s, []).append(q)
            remaining = [s for s in symbols if s not in result]

            if not gapfill and result:
                break

        for s, candidates in rejected.items():
            if s in result:
                continue
            escaped = self._try_escape(s, candidates)
            if escaped is not None:
                result[s] = escaped

        return result

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        for provider, breaker in self._providers:
            if not provider.supports_history:
                continue
            if not breaker.allow():
                continue
            try:
                result = await asyncio.wait_for(
                    provider.fetch_history(symbol, period, interval),
                    settings.provider_fetch_timeout,
                )
                if result.bars:
                    breaker.record_success()
                    return result
                breaker.record_failure()
            except TimeoutError:
                # NOT: dis iptal (CancelledError) burada YAKALANMAZ — quote
                # yolundaki asyncio.wait_for ile ayni koruma (bkz. fetch_quotes).
                breaker.record_failure()
                logger.warning(
                    "%s history zaman asimi (%.0f sn), sonraki kaynaga dusuluyor",
                    provider.name,
                    settings.provider_fetch_timeout,
                )
                continue
            except Exception as exc:
                breaker.record_failure()
                logger.warning("%s history hatasi: %s", provider.name, exc)
                continue
        return HistoryResponse(symbol=symbol, period=period, interval=interval, bars=[])


aggregator = Aggregator()
