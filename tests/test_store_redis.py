"""RedisStore testleri (fakeredis ile, gercek Redis gerektirmez)."""

import fakeredis.aioredis
from app.models import Quote
from app.store import RedisStore, _mask_redis_url


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


# --------------------------------------------------------------------------- #
# HIGH-1/HIGH-2 regresyonu (PR#19 review)
# --------------------------------------------------------------------------- #
async def test_connect_with_slash_password_builds_parseable_url(monkeypatch):
    """HIGH-1: `/`+`=` iceren parola (openssl rand -base64 32 ~%50 uretir)
    RedisStore.connect()'i kirmamali. app.config._build_redis_url'in urettigi
    URL, redis.asyncio.from_url'e GECERLI (urlparse edilebilir, parola
    kayipsiz geri kazanilabilir) sekilde iletilmeli -- eskiden
    'ValueError: Port could not be cast' ile patlardi."""
    from app.config import _build_redis_url
    from redis.connection import parse_url

    password = "ab/cd+ef=gh"
    url = _build_redis_url("redis://redis:6379/0", password)

    captured: dict[str, str] = {}

    class FakeRedis:
        async def ping(self):
            return True

    def fake_from_url(target_url, **kwargs):
        captured["url"] = target_url
        return FakeRedis()

    monkeypatch.setattr("redis.asyncio.from_url", fake_from_url)

    store = RedisStore(url, "test")
    await store.connect()  # regresyon: eskiden ValueError ile patlardi

    parsed = parse_url(captured["url"])
    assert parsed["password"] == password
    assert parsed["host"] == "redis"
    assert parsed["port"] == 6379


def test_mask_redis_url_hides_password_in_encoded_url():
    """HIGH-2: baglanti loglarinda parola ASLA gorunmemeli."""
    from app.config import _build_redis_url

    password = "ab/cd+ef=gh"
    url = _build_redis_url("redis://redis:6379/0", password)

    masked = _mask_redis_url(url)
    assert password not in masked
    assert "***" in masked
    assert "redis:6379/0" in masked


def test_mask_redis_url_no_leak_on_malformed_url():
    """Savunma-katmani: ayristirilamayan (bozuk) bir URL bile parolayi
    log'a sizdirmamali (fallback: tamami maskelenir)."""
    masked = _mask_redis_url("redis://:ab/cd+ef=@redis:6379/0")
    assert "ab/cd+ef=" not in masked
