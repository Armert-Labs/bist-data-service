import asyncio

from app.models import Quote
from app.store import MemoryStore


async def test_set_get_all():
    store = MemoryStore()
    await store.connect()
    q = Quote(symbol="THYAO", price=100.0, previous_close=95.0, change=5.0, change_percent=5.26)
    await store.set_quote("THYAO", q)

    got = await store.get_quote("THYAO")
    assert got is not None and got.price == 100.0
    assert await store.size() == 1
    assert "THYAO" in (await store.get_all())
    assert await store.get_quote("YOKKK") is None


async def test_staleness():
    store = MemoryStore()
    await store.connect()
    # Bos store bayat sayilir.
    assert await store.is_stale() is True
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    assert await store.is_stale() is False


async def test_stale_market_closed_never_stale(monkeypatch):
    """Market kapaliyken eski veri bayat SAYILMAZ (hafta sonu /ready 503 bugu)."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    # last_update'i 2 gun oncesine cek (hafta sonu senaryosu).
    store._last_update = datetime.now(UTC) - timedelta(days=2)

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: None)  # kapali
    assert await store.is_stale() is False


async def test_stale_market_open_old_data_is_stale(monkeypatch):
    """Market acik + tolerans penceresi gecmis + veri eski -> bayat."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    store._last_update = datetime.now(UTC) - timedelta(minutes=30)

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 3600.0)
    assert await store.is_stale() is True


async def test_stale_grace_period_after_open(monkeypatch):
    """Acilistan hemen sonra onceki seans verisi bayat sayilmaz (tolerans)."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    store._last_update = datetime.now(UTC) - timedelta(days=2)  # cuma verisi

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 60.0)  # acilis +1dk
    assert await store.is_stale() is False


async def test_pubsub_delivers_updates():
    store = MemoryStore()
    await store.connect()

    received = []

    async def consume():
        async for quotes in store.subscribe():
            received.extend(q.symbol for q in quotes)
            break  # tek mesaj yeter

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # abonelik kurulsun
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    await asyncio.wait_for(task, timeout=2)
    assert "THYAO" in received


async def test_intraday_persistence():
    store = MemoryStore()
    await store.connect()
    for price in (10.0, 11.0, 12.0):
        await store.set_quote("THYAO", Quote(symbol="THYAO", price=price))
    points = await store.get_intraday("THYAO")
    assert len(points) == 3
    assert points[-1]["p"] == 12.0
