"""Standalone updater servisi (Docker'da ayri container).

Redis modunda calisir: veriyi ceker, Redis'e yazar, pub/sub yayinlar ve
webhook alarmlarini degerlendirir. API surecleri yalnizca okur.

Calistirma:  python -m app.updater_main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from prometheus_client import start_http_server

from .config import validate_production
from .logging_config import setup_logging
from .store import get_store
from .updater import updater
from .webhooks import webhook_manager

logger = logging.getLogger("bist-updater")


async def main() -> None:
    setup_logging()
    validate_production()

    # Updater ayri surectir; metriklerini kendi portundan yayar (Prometheus
    # bunu ayrica scrape eder). API'nin /metrics'i yalnizca HTTP metriklerini gosterir.
    metrics_port = int(os.environ.get("UPDATER_METRICS_PORT", "8001"))
    try:
        start_http_server(metrics_port)
        logger.info("Updater metrikleri: :%d/metrics", metrics_port)
    except Exception as exc:
        logger.warning("Updater metrics sunucusu baslatilamadi: %s", exc)

    store = get_store()
    await store.connect()

    updater.start()
    webhook_task = asyncio.create_task(webhook_manager.watch(store))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # bazi platformlar
            loop.add_signal_handler(sig, stop.set)

    logger.info("Updater servisi hazir.")
    try:
        await stop.wait()
    finally:
        logger.info("Updater servisi kapatiliyor...")
        webhook_task.cancel()
        # Cancel'i bekle: watch()'in finally'sindeki drain() ucustaki teslimatlari
        # tamamlasin. Gercek exception'lar loglanir ama stop/close'u engellemez.
        try:
            await webhook_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Webhook izleyici hatayla sonlanmisti")
        await updater.stop()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
