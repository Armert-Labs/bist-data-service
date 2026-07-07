"""Provider arayuzu + circuit breaker.

Her veri kaynagi (Yahoo, Is Yatirim, ...) Provider'i uygular. Aggregator
bunlari sirayla dener; ard arda hata veren kaynak circuit breaker ile gecici
olarak devre disi birakilir (basarisiz kaynaga bosuna istek atilmaz).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from ..models import HistoryResponse, Quote

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Basit 3 durumlu devre kesici: closed -> open -> half_open -> closed."""

    def __init__(self, name: str, fail_threshold: int = 5, reset_timeout: float = 60.0) -> None:
        self.name = name
        self.fail_threshold = fail_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._state = "closed"

    @property
    def state(self) -> str:
        if (
            self._state == "open"
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self.reset_timeout
        ):
            self._state = "half_open"
        return self._state

    def allow(self) -> bool:
        return self.state in ("closed", "half_open")

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.fail_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
            logger.warning("Circuit breaker ACIK: %s (%d hata)", self.name, self._failures)

    @property
    def healthy(self) -> bool:
        return self.state == "closed"


class Provider(ABC):
    name: str
    # Bu kaynak gecmis OHLCV verebilir mi? False ise aggregator.fetch_history
    # bu saglayiciyi ATLAR (bos yanitini "hata" sayip circuit breaker'i tetikleyip
    # ayni kaynagin QUOTE hizmetini yanlislikla devre disi birakmasin).
    supports_history: bool = True

    @abstractmethod
    async def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Verilen semboller icin anlik fiyat goruntusu. Bulunamayan sembol
        sonuca dahil edilmez. Hata durumunda exception firlatir (aggregator yakalar)."""

    @abstractmethod
    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        """Tek sembol icin gecmis OHLCV."""
