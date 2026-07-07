"""Arka plan periyodik guncelleyici.

Takip listesini batch'ler halinde pipeline uzerinden ceker ve store'a yazar.
Drift monitörü periyodik capraz-kaynak kontrolu yapar.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import metrics
from . import symbols as sym
from .aggregator import aggregator
from .config import settings
from .market import is_market_open, market_state
from .pipeline import commit_quotes, run_drift_monitor
from .store import Store, get_store

logger = logging.getLogger(__name__)


class BackgroundUpdater:
    def __init__(self, symbols_list: list[str] | None = None, store: Store | None = None) -> None:
        self._symbols: list[str] = symbols_list or sym.default_watchlist()
        self._store: Store = store or get_store()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._update_lock = asyncio.Lock()
        self._update_running = False
        self._cycle = 0
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
            if quotes:
                await commit_quotes(self._store, quotes, market=state)
                for s, q in quotes.items():
                    if q.price is not None:
                        previous[s] = q.price
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
            age = await self._store.oldest_update_age()
            if age is not None:
                metrics.LAST_UPDATE_AGE.set(age)
                metrics.OLDEST_QUOTE_AGE.set(age)

    async def _cycle_work(self) -> None:
        await self._update_once()
        self._cycle += 1
        if (
            settings.drift_monitor_enabled
            and self._cycle % settings.drift_monitor_every_n_cycles == 0
        ):
            await run_drift_monitor(self._store)

    async def _run_cycle(self) -> bool:
        """Bir tam turu (guncelleme + drift monitoru) zaman butcesiyle calistirir.

        Provider timeout zincirleri (orn. yfinance ici takilma) turu suresiz
        uzatamaz; butce asiminda kalan is iptal edilir, o ana kadarki batch'ler
        zaten commit edilmistir. Donus: tur tamamlandi mi (timeout'ta False —
        warm-up turu tamamlanana kadar tekrarlanabilsin).
        """
        timeout = settings.updater_cycle_timeout
        try:
            await asyncio.wait_for(self._cycle_work(), timeout if timeout > 0 else None)
        except TimeoutError:
            metrics.UPDATE_CYCLE_TIMEOUTS.inc()
            logger.error(
                "Guncelleme turu %.0f sn zaman butcesini asti; kalan is iptal edildi.", timeout
            )
            return False
        return True

    async def _loop(self) -> None:
        self.running = True
        first = True
        while not self._stop.is_set():
            try:
                if first or settings.update_when_closed or is_market_open():
                    async with self._update_lock:
                        self._update_running = True
                        try:
                            if await self._run_cycle():
                                first = False
                        finally:
                            self._update_running = False
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
