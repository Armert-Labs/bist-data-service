"""Sembol bazli devre kesici: provider+symbol cifti icin gecici devre disi birakma."""

from __future__ import annotations

import logging
import time

from .config import settings

logger = logging.getLogger(__name__)


class SymbolCircuitRegistry:
    """Provider basina sembol bazli hata sayaci."""

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


symbol_circuit = SymbolCircuitRegistry()
