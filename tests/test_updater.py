"""BackgroundUpdater._update_once testleri (mock aggregator + in-memory store)."""

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
