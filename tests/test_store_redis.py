"""RedisStore testleri (fakeredis ile, gercek Redis gerektirmez)."""

import fakeredis.aioredis
from app.models import Quote
from app.store import RedisStore


def _store():
    store = RedisStore("redis://fake", "test")
    store._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return store


async def test_set_get_quote():
    store = _store()
    await store.set_quotes({"THYAO": Quote(symbol="THYAO", price=334.0, previous_close=330.0)})
    got = await store.get_quote("THYAO")
    assert got is not None and got.price == 334.0
    assert await store.get_quote("YOK") is None


async def test_get_many_and_all():
    store = _store()
    await store.set_quotes(
        {
            "THYAO": Quote(symbol="THYAO", price=100.0),
            "GARAN": Quote(symbol="GARAN", price=50.0),
        }
    )
    assert await store.size() == 2
    many = await store.get_quotes(["THYAO", "YOK"])
    assert set(many) == {"THYAO"}
    allq = await store.get_all()
    assert set(allq) == {"THYAO", "GARAN"}


async def test_last_update_and_staleness():
    store = _store()
    assert await store.last_update() is None
    assert await store.is_stale() is True
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    assert await store.last_update() is not None
    assert await store.is_stale() is False


async def test_intraday_persistence():
    store = _store()
    for price in (10.0, 11.0, 12.0):
        await store.set_quote("THYAO", Quote(symbol="THYAO", price=price))
    points = await store.get_intraday("THYAO")
    assert len(points) == 3
    assert points[-1]["p"] == 12.0  # eskiden yeniye


async def test_ping():
    store = _store()
    assert await store.ping() is True


async def test_pubsub_roundtrip():
    import asyncio

    store = _store()
    received: list[str] = []

    async def consume():
        async for quotes in store.subscribe():
            received.extend(q.symbol for q in quotes)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=1.0))
    try:
        await asyncio.wait_for(task, timeout=2)
    except TimeoutError:
        task.cancel()
    assert "THYAO" in received


async def test_ping_false_when_redis_down():
    """Redis erisilemezken ping() exception degil False dondurmeli."""

    class DeadRedis:
        async def ping(self):
            raise ConnectionError("baglanti yok")

    store = RedisStore("redis://fake", "test")
    store._redis = DeadRedis()
    assert await store.ping() is False
