"""Olay bazli webhook (fiyat alarmlari).

`webhooks.json` icindeki kurallar, store'un pub/sub akisi dinlenerek
degerlendirilir; kosul saglaninca hedef URL'e POST atilir (retry + cooldown).

NOT (guvenlik): Webhook URL'leri OPERATOR tarafindan config dosyasina yazilir
(son kullanici girdisi degil). Yine de ic aglara POST'u sinirlamak istiyorsaniz
bir allowlist ekleyin. Bu servis, hedef URL'leri dogrulamaz.

Kural bicimi (webhooks.json):
{
  "rules": [
    {"id":"thyao-ust","symbol":"THYAO","condition":"above","threshold":350,
     "url":"https://ornek/webhook","cooldown":300}
  ]
}
condition: above | below | pct_up | pct_down
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from . import metrics
from .config import settings
from .models import Quote
from .store import Store

logger = logging.getLogger(__name__)

# Pub/sub kopmasinda yeniden abone olmadan once beklenecek sure (sn).
_RECONNECT_DELAY = 5.0

_VALID_CONDITIONS = {"above", "below", "pct_up", "pct_down"}


def _validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Webhook URL https olmali: {url!r}")
    if settings.webhook_url_allowlist:
        host = (parsed.hostname or "").lower()
        allowed = {h.lower() for h in settings.webhook_url_allowlist}
        if host not in allowed:
            raise ValueError(f"Webhook host izin listesinde degil: {host!r}")


class AlarmRule:
    def __init__(self, data: dict) -> None:
        self.id = str(data["id"])
        self.symbol = str(data["symbol"]).strip().upper()
        self.condition = str(data["condition"]).strip().lower()
        if self.condition not in _VALID_CONDITIONS:
            raise ValueError(f"Gecersiz condition: {self.condition}")
        self.threshold = float(data["threshold"])
        self.url = str(data["url"])
        _validate_webhook_url(self.url)
        self.cooldown = float(data.get("cooldown", 300))
        # -inf: henuz tetiklenmedi -> ilk uygun kosulda hemen hazir (monotonic
        # surec-goreli oldugundan 0.0 baslangici, servis acilisinda ilk cooldown
        # saniye boyunca yanlislikla "hazir degil" derdi).
        self._last_fired = float("-inf")

    def matches(self, quote: Quote) -> bool:
        if quote.price is None:
            return False
        if self.condition == "above":
            return quote.price >= self.threshold
        if self.condition == "below":
            return quote.price <= self.threshold
        if self.condition == "pct_up":
            return (quote.change_percent or 0.0) >= self.threshold
        if self.condition == "pct_down":
            return (quote.change_percent or 0.0) <= -abs(self.threshold)
        return False

    def ready(self) -> bool:
        return (time.monotonic() - self._last_fired) >= self.cooldown

    def mark_fired(self) -> None:
        self._last_fired = time.monotonic()


class WebhookManager:
    def __init__(self) -> None:
        self.rules: list[AlarmRule] = []
        self._by_symbol: dict[str, list[AlarmRule]] = {}
        # Teslimatlar arka plan gorevleri olarak kosulur; retry/backoff yapan
        # yavas bir hedef URL, fiyat akisini dinleyen watch dongusunu bloklamaz.
        self._tasks: set[asyncio.Task] = set()
        self._delivery_sem = asyncio.Semaphore(5)
        self.load()

    def load(self) -> None:
        path = settings.webhooks_config_path
        if not os.path.exists(path):
            logger.info("Webhook config bulunamadi (%s); kural yuklenmedi.", path)
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.rules = [AlarmRule(r) for r in data.get("rules", [])]
            self._by_symbol = {}
            for rule in self.rules:
                self._by_symbol.setdefault(rule.symbol, []).append(rule)
            logger.info("%d webhook kurali yuklendi.", len(self.rules))
        except Exception as exc:
            logger.error("Webhook config okunamadi: %s", exc)

    async def _deliver(self, rule: AlarmRule, quote: Quote, client: httpx.AsyncClient) -> None:
        payload = {
            "rule_id": rule.id,
            "symbol": quote.symbol,
            "condition": rule.condition,
            "threshold": rule.threshold,
            "price": quote.price,
            "change_percent": quote.change_percent,
            "triggered_at": datetime.now(UTC).isoformat(),
        }
        for attempt in range(1, settings.webhook_max_retries + 1):
            try:
                resp = await client.post(rule.url, json=payload, timeout=settings.webhook_timeout)
                resp.raise_for_status()
                metrics.WEBHOOK_DELIVERIES.labels(status="success").inc()
                logger.info("Webhook gonderildi: %s -> %s (%s)", rule.id, rule.url, quote.price)
                return
            except Exception as exc:
                logger.warning(
                    "Webhook denemesi %d/%d basarisiz (%s): %s",
                    attempt,
                    settings.webhook_max_retries,
                    rule.id,
                    exc,
                )
                await asyncio.sleep(min(2**attempt, 10))
        metrics.WEBHOOK_DELIVERIES.labels(status="failed").inc()

    async def _deliver_bounded(
        self, rule: AlarmRule, quote: Quote, client: httpx.AsyncClient
    ) -> None:
        async with self._delivery_sem:
            await self._deliver(rule, quote, client)

    async def evaluate(self, quotes: list[Quote], client: httpx.AsyncClient) -> None:
        """Kurallari degerlendirir; tetiklenen teslimatlari BLOKLAMADAN baslatir.

        Teslimat (timeout + retry + backoff) uzun surebilir; watch dongusunu
        bekletmek fiyat guncellemelerinin kacirilmasina yol acar. Bu yuzden
        teslimatlar semaforla sinirli arka plan gorevleridir.
        """
        for quote in quotes:
            for rule in self._by_symbol.get(quote.symbol, []):
                if rule.ready() and rule.matches(quote):
                    rule.mark_fired()
                    task = asyncio.create_task(self._deliver_bounded(rule, quote, client))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Bekleyen tum teslimat gorevlerinin bitmesini bekler (kapanis/test)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def watch(self, store: Store) -> None:
        if not settings.webhooks_enabled or not self.rules:
            logger.info(
                "Webhook izleyici pasif (etkin=%s, kural=%d).",
                settings.webhooks_enabled,
                len(self.rules),
            )
            return
        logger.info("Webhook izleyici baslatildi (%d kural).", len(self.rules))
        try:
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        async for quotes in store.subscribe():
                            try:
                                await self.evaluate(quotes, client)
                            except Exception:
                                logger.exception("Webhook degerlendirme hatasi")
                    except Exception:
                        logger.exception(
                            "Webhook pub/sub akisi koptu; %.0f sn sonra yeniden abone olunacak",
                            _RECONNECT_DELAY,
                        )
                    # Akis normal bitse de (store kapanmasi) ayni bekleyisle tekrar
                    # abone ol: izleyici yalnizca cancel ile olur.
                    await asyncio.sleep(_RECONNECT_DELAY)
        finally:
            # Client kapanmadan once ucuslardaki teslimatlari tamamla.
            await self.drain()


webhook_manager = WebhookManager()
