"""Sembol bazli devre kesici: provider+symbol cifti icin gecici devre disi birakma."""

from __future__ import annotations

import logging
import time

from .config import settings

logger = logging.getLogger(__name__)


class SymbolCircuitRegistry:
    """Provider basina sembol bazli hata sayaci."""

    # /quote/{symbol} ve /quotes?symbols= sembolu yalnizca BICIM olarak dogrular
    # (gercek watchlist uyeligini degil); tek seferlik (typo/bot) sorgular
    # buraya hicbir zaman esige ulasmayan "kapali" kayitlar birakabilir. Tavan +
    # budama olmadan bu, sinirsiz bellek buyumesine yol acar.
    _MAX_ENTRIES = 4096

    def __init__(
        self,
        fail_threshold: int | None = None,
        reset_timeout: float | None = None,
    ) -> None:
        self._fail_threshold = fail_threshold or settings.symbol_circuit_fail_threshold
        self._reset_timeout = reset_timeout or settings.symbol_circuit_reset_seconds
        self._failures: dict[tuple[str, str], int] = {}
        self._opened_at: dict[tuple[str, str], float] = {}

    def _key(self, provider: str, symbol: str) -> tuple[str, str]:
        return (provider, symbol.upper())

    def allow(self, provider: str, symbol: str) -> bool:
        key = self._key(provider, symbol)
        opened = self._opened_at.get(key)
        if opened is None:
            return True
        if time.monotonic() - opened >= self._reset_timeout:
            self._failures.pop(key, None)
            self._opened_at.pop(key, None)
            return True
        return False

    def record_success(self, provider: str, symbol: str) -> None:
        key = self._key(provider, symbol)
        self._failures.pop(key, None)
        self._opened_at.pop(key, None)

    def record_failure(self, provider: str, symbol: str) -> None:
        key = self._key(provider, symbol)
        if key not in self._failures and len(self._failures) >= self._MAX_ENTRIES:
            self._evict_garbage()
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= self._fail_threshold:
            self._opened_at[key] = time.monotonic()
            logger.debug(
                "Sembol devre kesici ACIK: %s/%s (%d hata)",
                provider,
                symbol,
                count,
            )

    def _evict_garbage(self) -> None:
        """Tavan asilinca once ACIK OLMAYAN (esige ulasmamis) kayitlari budar —
        bunlar tipik olarak tek-seferlik gecersiz/typo sembol sorgularidir.
        Hala doluysa (hepsi acik) en eski acilan yarisini budar; gercek aktif
        devre kesiciler tamamen kaybolmaz, yalnizca en eskiler feda edilir."""
        closed_keys = [k for k in self._failures if k not in self._opened_at]
        for k in closed_keys:
            self._failures.pop(k, None)
        if len(self._failures) >= self._MAX_ENTRIES:
            oldest = sorted(self._opened_at.items(), key=lambda kv: kv[1])
            for k, _ in oldest[: len(oldest) // 2]:
                self._failures.pop(k, None)
                self._opened_at.pop(k, None)


symbol_circuit = SymbolCircuitRegistry()
