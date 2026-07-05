"""Arka plan periyodik guncelleyici.

Takip listesini batch'ler halinde aggregator uzerinden ceker, sanity-check
icin onceki fiyatlari gecirir ve store'a yazar (store pub/sub yayinlar).

Calisma modu:
- Redis YOK  -> API sureci icinde calisir (tek process, in-memory store).
- Redis VAR  -> ayri `updater_main` servisi olarak calisir; API yalnizca okur
  (cok worker'da cift cekimi onlemek icin).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from . import metrics
from . import symbols as sym
from .aggregator import aggregator
from .config import settings
from .market import is_market_open, market_state
from .store import Store, get_store

logger = logging.getLogger(__name__)


class BackgroundUpdater:
    def __init__(self, symbols_list: list[str] | None = None, store: Store | None = None) -> None:
        self._symbols: list[str] = symbols_list or sym.default_watchlist()
        self._store: Store = store or get_store()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.running = False

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    async def _update_once(self) -> int:
        state = market_state()

        current = await self._store.get_all()
        previous = {s: q.price for s, q in current.items() if q.price is not None}

        total = 0
        started = time.monotonic()
        batch_size = max(1, settings.batch_size)

        for start in range(0, len(self._symbols), batch_size):
            if self._stop.is_set():
                break
            batch = self._symbols[start : start + batch_size]
            quotes = await aggregator.fetch_quotes(batch, previous=previous)
            for quote in quotes.values():
                quote.market_state = state
            if quotes:
                await self._store.set_quotes(quotes)
                total += len(quotes)
            await asyncio.sleep(settings.batch_pause)

        duration = time.monotonic() - started
        metrics.UPDATE_DURATION.observe(duration)
        metrics.UPDATE_SYMBOLS.set(total)
        with contextlib.suppress(Exception):
            metrics.QUOTES_CACHED.set(await self._store.size())
        logger.info(
            "Guncelleme tamamlandi: %d/%d sembol, %.1f sn", total, len(self._symbols), duration
        )
        return total

    async def _refresh_age_metric(self) -> None:
        with contextlib.suppress(Exception):
            lu = await self._store.last_update()
            if lu is not None:
                age = (datetime.now(UTC) - lu).total_seconds()
                metrics.LAST_UPDATE_AGE.set(age)

    async def _loop(self) -> None:
        self.running = True
        first = True
        while not self._stop.is_set():
            try:
                if first or settings.update_when_closed or is_market_open():
                    await self._update_once()
                    first = False
                else:
                    logger.debug("Piyasa kapali; guncelleme atlandi.")
                await self._refresh_age_metric()
            except Exception:
                logger.exception("Guncelleme dongusunde beklenmeyen hata")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=settings.update_interval)
        self.running = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info("Arka plan guncelleyici baslatildi (%d sembol).", len(self._symbols))

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
        logger.info("Arka plan guncelleyici durduruldu.")


updater = BackgroundUpdater()
