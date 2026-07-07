"""BackgroundUpdater._update_once testleri (mock aggregator + in-memory store)."""

import asyncio

import app.updater as updater_mod
from app.models import Quote
from app.store import MemoryStore
from app.updater import BackgroundUpdater


async def test_update_once_writes_to_store(monkeypatch):
    store = MemoryStore()
    await store.connect()

    async def fake_fetch(symbols, previous=None):
        return {s: Quote(symbol=s, price=100.0) for s in symbols}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO", "GARAN"], store=store)
    count = await up._update_once()

    assert count == 2
    got = await store.get_quote("THYAO")
    assert got is not None and got.price == 100.0
    # market_state atandi mi
    assert got.market_state in ("OPEN", "CLOSED")


async def test_update_once_passes_previous_for_sanity(monkeypatch):
    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=100.0))

    seen_previous = {}

    async def fake_fetch(symbols, previous=None):
        seen_previous.update(previous or {})
        return {}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)
    await up._update_once()
    assert seen_previous.get("THYAO") == 100.0


async def test_run_cycle_times_out_and_recovers(monkeypatch, override_settings):
    """Takilan _update_once tur butcesinde iptal edilmeli; hata yukari sizmamali."""
    override_settings(updater_cycle_timeout=0.05)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    async def hang():
        await asyncio.sleep(5)

    monkeypatch.setattr(up, "_update_once", hang)
    ok = await asyncio.wait_for(up._run_cycle(), timeout=1)
    assert ok is False  # timeout'lu tur 'tamamlandi' sayilmamali (warm-up tekrari)


async def test_run_cycle_runs_update(monkeypatch, override_settings):
    override_settings(updater_cycle_timeout=5.0)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    called = []

    async def fake_update():
        called.append(1)
        return 1

    monkeypatch.setattr(up, "_update_once", fake_update)
    ok = await up._run_cycle()
    assert ok is True
    assert called == [1]
