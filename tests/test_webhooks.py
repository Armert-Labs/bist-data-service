"""Webhook alarm kurallari testleri."""

import httpx
import pytest
import respx
from app.models import Quote
from app.webhooks import AlarmRule, WebhookManager


def _rule(**kw):
    base = {
        "id": "r",
        "symbol": "THYAO",
        "condition": "above",
        "threshold": 100,
        "url": "https://hook.test/x",
    }
    base.update(kw)
    return AlarmRule(base)


def test_above_below():
    r = _rule(condition="above", threshold=100)
    assert r.matches(Quote(symbol="THYAO", price=101)) is True
    assert r.matches(Quote(symbol="THYAO", price=99)) is False

    r2 = _rule(condition="below", threshold=100)
    assert r2.matches(Quote(symbol="THYAO", price=99)) is True


def test_pct_conditions():
    up = _rule(condition="pct_up", threshold=5)
    assert up.matches(Quote(symbol="THYAO", price=1, change_percent=6)) is True
    assert up.matches(Quote(symbol="THYAO", price=1, change_percent=4)) is False

    down = _rule(condition="pct_down", threshold=5)
    assert down.matches(Quote(symbol="THYAO", price=1, change_percent=-6)) is True


def test_no_price_never_matches():
    assert _rule().matches(Quote(symbol="THYAO", price=None)) is False


def test_cooldown():
    r = _rule(cooldown=100)
    assert r.ready() is True
    r.mark_fired()
    assert r.ready() is False


def test_invalid_condition_raises():
    with pytest.raises(ValueError):
        _rule(condition="gecersiz")


def _manager(rules):
    import asyncio

    mgr = WebhookManager.__new__(WebhookManager)
    mgr.rules = rules
    mgr._by_symbol = {}
    for rule in rules:
        mgr._by_symbol.setdefault(rule.symbol, []).append(rule)
    mgr._tasks = set()
    mgr._delivery_sem = asyncio.Semaphore(5)
    return mgr


@respx.mock
async def test_evaluate_delivers_when_triggered():
    route = respx.post("https://hook.test/x").mock(return_value=httpx.Response(200))
    mgr = _manager([_rule(condition="above", threshold=100)])

    async with httpx.AsyncClient() as client:
        await mgr.evaluate([Quote(symbol="THYAO", price=150)], client)
        await mgr.drain()  # teslimatlar arka plan gorevi; bitmesini bekle

    assert route.called


@respx.mock
async def test_evaluate_does_not_block_on_slow_delivery():
    """Yavas/basarisiz teslimat evaluate()'i bloklamamali (watch dongusu korunur)."""
    import time as _time

    respx.post("https://hook.test/x").mock(return_value=httpx.Response(500))
    mgr = _manager([_rule(condition="above", threshold=100, cooldown=0)])

    async with httpx.AsyncClient() as client:
        started = _time.monotonic()
        await mgr.evaluate([Quote(symbol="THYAO", price=150)], client)
        elapsed = _time.monotonic() - started
        # 3 retry + backoff senkron olsaydi saniyeler surerdi; gorev olarak aninda doner.
        assert elapsed < 0.5
        # Retry backoff'unu bekleme; gorevleri iptal edip temizle (test hizi).
        import asyncio

        for task in list(mgr._tasks):
            task.cancel()
        await asyncio.gather(*list(mgr._tasks), return_exceptions=True)
