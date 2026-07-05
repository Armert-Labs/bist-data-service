"""Coklu kaynak orkestrasyonu: fallback + circuit breaker + sanity-check.

Strateji: Saglayicilar oncelik sirasiyla denenir. gapfill modunda her kaynak
eksik sembolleri tamamlar. Bir kaynak ard arda hata verirse circuit breaker onu
gecici devre disi birakir.

Sanity-check: onceki bilinen fiyata gore absurt sicramalar elenir.
Kapsam kontrolu: kismi/bos yanit basarisiz sayilir (circuit breaker tetiklenir).
"""

from __future__ import annotations

import logging

from . import metrics
from .config import settings
from .models import HistoryResponse, Quote
from .providers.base import CircuitBreaker, Provider
from .providers.isyatirim import IsYatirimProvider
from .providers.yahoo import YahooProvider
from .providers.yahoo_chart import YahooChartProvider
from .symbol_circuit import symbol_circuit

logger = logging.getLogger(__name__)

_FACTORY = {
    "yahoo": YahooProvider,
    "yahoo_chart": YahooChartProvider,
    "isyatirim": IsYatirimProvider,
}


class Aggregator:
    def __init__(self) -> None:
        self._providers: list[tuple[Provider, CircuitBreaker]] = []
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
        if not previous:
            return True
        prev = previous.get(symbol)
        if prev is None or prev == 0 or quote.price is None:
            return True
        change_pct = abs((quote.price - prev) / prev * 100.0)
        if change_pct > settings.sanity_max_change_percent:
            metrics.SANITY_REJECTS.inc()
            logger.warning(
                "Sanity reddi %s: %%%.1f degisim (%.4f -> %.4f)",
                symbol,
                change_pct,
                prev,
                quote.price,
            )
            return False
        return True

    def get_provider(self, name: str) -> Provider | None:
        for provider, _ in self._providers:
            if provider.name == name:
                return provider
        return None

    async def fetch_quotes(
        self,
        symbols: list[str],
        previous: dict[str, float] | None = None,
    ) -> dict[str, Quote]:
        mode = settings.provider_mode.lower()
        gapfill = mode in ("gapfill", "hybrid")
        result: dict[str, Quote] = {}
        remaining = list(symbols)
        min_cov = settings.provider_min_coverage_pct / 100.0

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
                fetched = await provider.fetch_quotes(ask)
            except Exception as exc:
                breaker.record_failure()
                metrics.FETCH_ERRORS.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                for s in ask:
                    symbol_circuit.record_failure(provider.name, s)
                logger.warning("%s fetch hatasi, sonraki kaynaga dusuluyor: %s", provider.name, exc)
                continue

            if self._coverage_ok(fetched, ask):
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
                else:
                    symbol_circuit.record_failure(provider.name, s)

            for s, q in fetched.items():
                if s not in result and self._is_sane(s, q, previous):
                    result[s] = q
            remaining = [s for s in symbols if s not in result]

            if not gapfill and result:
                break

        return result

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        for provider, breaker in self._providers:
            if not breaker.allow():
                continue
            try:
                result = await provider.fetch_history(symbol, period, interval)
                if result.bars:
                    breaker.record_success()
                    return result
                breaker.record_failure()
            except Exception as exc:
                breaker.record_failure()
                logger.warning("%s history hatasi: %s", provider.name, exc)
                continue
        return HistoryResponse(symbol=symbol, period=period, interval=interval, bars=[])


aggregator = Aggregator()
