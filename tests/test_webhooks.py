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


@respx.mock
async def test_evaluate_delivers_when_triggered():
    route = respx.post("https://hook.test/x").mock(return_value=httpx.Response(200))
    mgr = WebhookManager.__new__(WebhookManager)
    mgr.rules = [_rule(condition="above", threshold=100)]
    mgr._by_symbol = {"THYAO": mgr.rules}

    async with httpx.AsyncClient() as client:
        await mgr.evaluate([Quote(symbol="THYAO", price=150)], client)

    assert route.called
