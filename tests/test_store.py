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
    old = datetime.now(UTC) - timedelta(days=2)
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0, updated_at=old))

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: None)
    assert await store.is_stale() is False


async def test_stale_market_open_old_data_is_stale(monkeypatch):
    """Market acik + tolerans penceresi gecmis + veri eski -> bayat."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    old = datetime.now(UTC) - timedelta(minutes=30)
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0, updated_at=old))

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 3600.0)
    assert await store.is_stale() is True


async def test_stale_single_old_symbol_does_not_poison(monkeypatch, override_settings):
    """Tek guncellenemeyen sembol (askidaki hisse) tum servisi bayat gostermemeli."""
    from datetime import UTC, datetime, timedelta

    override_settings(staleness_min_fresh_pct=90.0)
    store = MemoryStore()
    await store.connect()
    now = datetime.now(UTC)
    quotes = {f"S{i:02d}": Quote(symbol=f"S{i:02d}", price=1.0, updated_at=now) for i in range(19)}
    quotes["HALTED"] = Quote(symbol="HALTED", price=1.0, updated_at=now - timedelta(hours=6))
    await store.set_quotes(quotes)

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 3600.0)
    assert await store.is_stale() is False


async def test_stale_when_fresh_coverage_below_threshold(monkeypatch, override_settings):
    from datetime import UTC, datetime, timedelta

    override_settings(staleness_min_fresh_pct=90.0)
    store = MemoryStore()
    await store.connect()
    now = datetime.now(UTC)
    old = now - timedelta(hours=6)
    quotes = {f"S{i:02d}": Quote(symbol=f"S{i:02d}", price=1.0, updated_at=old) for i in range(8)}
    quotes["FRESH1"] = Quote(symbol="FRESH1", price=1.0, updated_at=now)
    quotes["FRESH2"] = Quote(symbol="FRESH2", price=1.0, updated_at=now)
    await store.set_quotes(quotes)

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 3600.0)
    assert await store.is_stale() is True


async def test_stale_grace_period_after_open(monkeypatch):
    """Acilistan hemen sonra onceki seans verisi bayat sayilmaz (tolerans)."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    old = datetime.now(UTC) - timedelta(days=2)
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0, updated_at=old))

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 60.0)
    assert await store.is_stale() is False


async def test_stale_flagged_quote_does_not_count_as_fresh(monkeypatch, override_settings):
    """HIGH-1: fail-open (bkz. aggregator.end_cycle) `stale=True` isaretiyle
    commit ettigi bir quote'un `updated_at`'i cache YAZIM anini gosterir
    (GERCEK veri yasini degil) -- yalniz `updated_at`'e bakan bir fresh_ratio
    fail-open sirasinda HER SEYI "taze" sanip `/ready`'yi yanlislikla
    yesile cevirirdi, hicbir alarm calmazdi. `stale=True` quote artik TAZE
    SAYILMAZ."""
    from datetime import UTC, datetime

    override_settings(staleness_min_fresh_pct=90.0)
    store = MemoryStore()
    await store.connect()
    now = datetime.now(UTC)
    # 20 sembolun HEPSI fail-open ile stale=True + updated_at=simdi commit edildi.
    quotes = {
        f"S{i:02d}": Quote(symbol=f"S{i:02d}", price=1.0, updated_at=now, stale=True)
        for i in range(20)
    }
    await store.set_quotes(quotes)

    monkeypatch.setattr("app.market.seconds_since_open", lambda now=None: 3600.0)
    assert await store.fresh_ratio() == 0.0
    assert await store.is_stale() is True


async def test_oldest_update_age_uses_real_data_time_for_stale_quotes():
    """HIGH-1 kardes bulgu: `stale=True` isaretli bir quote'ta `updated_at`
    cache YAZIM anini gosterir -- yalniz ona bakan bir yas hesabi fail-open
    sirasinda cache-yazimiyla SIFIRLANIR, `bist_oldest_quote_age_seconds`
    metrigini ve `/ready`'nin `oldest_quote_age_seconds` alanini yaniltir.
    Artik stale=True quote'larda yas GERCEK veri zamanindan (exchange_time)
    hesaplanir -- saatler once bir bar'in yasi saniyelere KUCULTULMEZ."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    now = datetime.now(UTC)
    yesterday_bar = now - timedelta(hours=20)
    await store.set_quotes(
        {
            "THYAO": Quote(
                symbol="THYAO",
                price=1.0,
                updated_at=now,  # cache YAZIM ani -- "simdi"
                exchange_time=yesterday_bar,  # GERCEK veri ani -- 20 saat once
                stale=True,
            )
        }
    )
    age = await store.oldest_update_age()
    assert age is not None
    assert age > 3600 * 15  # cache-yazim yasina (saniyeler) gore DEGIL, gercek bar yasina gore


async def test_oldest_update_age_still_uses_updated_at_for_fresh_quotes():
    """Regresyon kilidi: stale=False (normal) quote'larda davranis DEGISMEDI --
    yas hala `updated_at`'ten hesaplanir."""
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    await store.connect()
    old = datetime.now(UTC) - timedelta(seconds=120)
    await store.set_quotes({"THYAO": Quote(symbol="THYAO", price=1.0, updated_at=old, stale=False)})
    age = await store.oldest_update_age()
    assert age is not None
    assert 110 <= age <= 130


async def test_negative_cache():
    store = MemoryStore()
    await store.connect()
    assert await store.negative_cache_has("ZZZZ") is False
    await store.negative_cache_add("ZZZZ")
    assert await store.negative_cache_has("ZZZZ") is True


async def test_history_cache_roundtrip():
    from datetime import UTC, datetime

    from app.models import HistoryBar, HistoryResponse

    store = MemoryStore()
    await store.connect()
    data = HistoryResponse(
        symbol="THYAO",
        period="1mo",
        interval="1d",
        bars=[HistoryBar(time=datetime.now(UTC), close=10.0)],
    )
    await store.set_history_cached("THYAO", "1mo", "1d", data)
    got = await store.get_history_cached("THYAO", "1mo", "1d")
    assert got is not None and len(got.bars) == 1


async def test_history_cache_bounded_growth():
    # /history/{symbol} sembolu yalnizca BICIM olarak dogrular; farkli
    # sembol x period x interval kombinasyonlariyla tek seferlik (typo/bot)
    # sorgular onbellegi sonsuza kadar biriktirmemeli (bkz. _negative ile
    # ayni tavan+budama deseni).
    from app.models import HistoryResponse

    store = MemoryStore()
    await store.connect()
    for i in range(store._HISTORY_CACHE_MAX + 500):
        await store.set_history_cached(
            f"FAKE{i}",
            "1mo",
            "1d",
            HistoryResponse(symbol=f"FAKE{i}", period="1mo", interval="1d", bars=[]),
        )
    assert len(store._history_cache) <= store._HISTORY_CACHE_MAX


async def test_subscribe_symbol_filter():
    store = MemoryStore()
    await store.connect()
    received: list[str] = []

    async def consume():
        async for quotes in store.subscribe(frozenset({"GARAN"})):
            received.extend(q.symbol for q in quotes)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await store.set_quotes(
        {
            "THYAO": Quote(symbol="THYAO", price=1.0),
            "GARAN": Quote(symbol="GARAN", price=2.0),
        }
    )
    await asyncio.wait_for(task, timeout=2)
    assert received == ["GARAN"]


async def test_pubsub_delivers_updates():
    store = MemoryStore()
    await store.connect()

    received = []

    async def consume():
        async for quotes in store.subscribe():
            received.extend(q.symbol for q in quotes)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
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


async def test_publish_queue_full_counts_drops():
    """Yavas abonede dusen olaylar sessiz kalmamali (metrik artmali)."""
    from app import metrics as m

    store = MemoryStore()
    await store.connect()
    gen = store.subscribe()
    first = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.01)  # abone kaydi olussun

    before = m.SSE_DROPPED_EVENTS._value.get()
    for i in range(120):  # kuyruk maxsize=100; fazlasi dusmeli
        await store.set_quote("THYAO", Quote(symbol="THYAO", price=float(i + 1)))
    assert m.SSE_DROPPED_EVENTS._value.get() > before

    import contextlib

    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    await gen.aclose()
