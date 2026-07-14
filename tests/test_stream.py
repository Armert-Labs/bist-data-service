"""SSE /stream endpoint testleri (denetim: kritik yol hic test edilmiyordu)."""

import asyncio
import json

from app.main import app
from fastapi.testclient import TestClient

from tests.test_api import _seed_store


class _StubRequest:
    async def is_disconnected(self) -> bool:
        return True


async def test_stream_generator_snapshot_first():
    """Baglanan istemci ILK olay olarak tam snapshot almali (state telafisi)."""
    from app.main import _stream_generator

    _seed_store()
    gen = _stream_generator(_StubRequest(), frozenset({"THYAO"}))
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert event["event"] == "quotes"
    assert payload["quotes"]["THYAO"]["price"] == 334.0
    assert "GARAN" not in payload["quotes"]  # sembol filtresi
    assert payload["market"] in ("OPEN", "CLOSED")
    await gen.aclose()


async def test_stream_generator_receives_pubsub_event():
    """Snapshot sonrasi pub/sub'a dusen guncelleme istemciye ulasmali."""
    from app.main import _stream_generator, store
    from app.models import Quote

    _seed_store()

    class LiveRequest:
        async def is_disconnected(self) -> bool:
            return False

    gen = _stream_generator(LiveRequest(), frozenset({"THYAO"}))
    await asyncio.wait_for(gen.__anext__(), timeout=2)  # snapshot

    next_event = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)  # abone kaydi olussun
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=999.0))
    event = await asyncio.wait_for(next_event, timeout=2)
    payload = json.loads(event["data"])
    assert payload["quotes"]["THYAO"]["price"] == 999.0
    await gen.aclose()


def test_stream_client_counter_restored_after_close():
    from app import main as main_mod
    from app.main import _stream_generator

    _seed_store()
    before = main_mod._sse_clients

    async def run():
        gen = _stream_generator(_StubRequest(), None)
        await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert main_mod._sse_clients == before + 1
        await gen.aclose()

    asyncio.run(run())
    assert main_mod._sse_clients == before  # finally sayaci geri aldi


def test_stream_rejects_over_client_limit(override_settings):
    override_settings(max_sse_clients=0)
    with TestClient(app) as c:
        r = c.get("/stream")
        assert r.status_code == 503


def test_stream_requires_key_when_auth_enabled(monkeypatch):
    from app.auth import registry

    monkeypatch.setattr(registry, "_entries", [("testkey", "test", False)])
    with TestClient(app) as c:
        assert c.get("/stream").status_code == 401


def test_parse_symbols_lenient_splits_valid_invalid():
    from app.main import _parse_symbols_lenient

    valid, invalid = _parse_symbols_lenient("thyao, garan, toolongsymbol, !!, thyao")
    assert valid == ["THYAO", "GARAN"]  # normalize + dedup
    assert invalid == ["TOOLONGSYMBOL", "!!"]  # firlatmaz; ayri liste


def test_parse_symbols_lenient_empty_returns_two_empty():
    from app.main import _parse_symbols_lenient

    assert _parse_symbols_lenient(None) == ([], [])
    assert _parse_symbols_lenient("") == ([], [])


def test_parse_symbols_lenient_truncates_overlong_invalid():
    from app.main import _parse_symbols_lenient

    _, invalid = _parse_symbols_lenient("A" * 500)
    assert len(invalid[0]) == 16  # unavailable[] payload sismesin (DoS koruma)


async def test_stream_snapshot_mixed_available_and_delisted():
    """(a) karisik: available quotes dogru + delisted negative_cache reason'i."""
    from app.main import _stream_generator, store

    _seed_store()  # THYAO, GARAN cached
    await store.negative_cache_add("DELIS")
    gen = _stream_generator(_StubRequest(), frozenset({"THYAO", "DELIS"}), ())
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert payload["quotes"]["THYAO"]["price"] == 334.0
    assert "DELIS" not in payload["quotes"]
    assert {"symbol": "DELIS", "reason": "negative_cache"} in payload["unavailable"]
    await gen.aclose()


async def test_stream_snapshot_all_negative_cache_still_emits():
    """(b) hepsi unavailable (negative_cache): quotes bos AMA snapshot YINE yayinlanir."""
    from app.main import _stream_generator, store

    _seed_store()
    await store.negative_cache_add("AAAA")
    await store.negative_cache_add("BBBB")
    gen = _stream_generator(_StubRequest(), frozenset({"AAAA", "BBBB"}), ())
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert payload["quotes"] == {}  # bos
    reasons = {u["symbol"]: u["reason"] for u in payload["unavailable"]}
    assert reasons == {"AAAA": "negative_cache", "BBBB": "negative_cache"}
    await gen.aclose()


async def test_stream_snapshot_all_invalid_format_still_emits():
    """(b') hepsi unavailable (invalid_format): symbol_filter bos, invalid dolu."""
    from app.main import _stream_generator

    _seed_store()
    gen = _stream_generator(_StubRequest(), frozenset(), ("TOOLONGX", "XX!!"))
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert payload["quotes"] == {}
    reasons = {u["symbol"]: u["reason"] for u in payload["unavailable"]}
    assert reasons == {"TOOLONGX": "invalid_format", "XX!!": "invalid_format"}
    await gen.aclose()


async def test_stream_snapshot_ondemand_fetch_for_uncached(monkeypatch):
    """(c) cache-disi gecerli sembol -> on-demand fetch tetiklenir, snapshot'a girer."""
    from app.main import _stream_generator
    from app.models import Quote

    _seed_store()
    calls = []

    async def fake_fetch(store_arg, symbols, **kw):
        calls.append(list(symbols))
        return {"KCHOL": Quote(symbol="KCHOL", price=42.0)}

    monkeypatch.setattr("app.main.fetch_and_commit", fake_fetch)
    gen = _stream_generator(_StubRequest(), frozenset({"KCHOL"}), ())
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert calls == [["KCHOL"]]  # fetch tetiklendi
    assert payload["quotes"]["KCHOL"]["price"] == 42.0
    assert "unavailable" not in payload  # hepsi karsilandi -> alan YOK
    await gen.aclose()


async def test_stream_snapshot_fetch_failed_reason(monkeypatch):
    """(c') on-demand fetch veri getirmezse -> fetch_failed reason."""
    from app.main import _stream_generator

    _seed_store()

    async def fake_fetch(store_arg, symbols, **kw):
        return {}  # veri gelmedi

    monkeypatch.setattr("app.main.fetch_and_commit", fake_fetch)
    gen = _stream_generator(_StubRequest(), frozenset({"NEWCO"}), ())
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert {"symbol": "NEWCO", "reason": "fetch_failed"} in payload["unavailable"]
    await gen.aclose()


async def test_stream_snapshot_invalid_format_skips_fetch(monkeypatch):
    """(d) invalid_format sembol fetch DENENMEDEN raporlanir."""
    from app.main import _stream_generator

    _seed_store()
    called = False

    async def fake_fetch(*a, **k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("app.main.fetch_and_commit", fake_fetch)
    gen = _stream_generator(_StubRequest(), frozenset({"THYAO"}), ("TOOLONGX",))
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert payload["quotes"]["THYAO"]["price"] == 334.0
    assert {"symbol": "TOOLONGX", "reason": "invalid_format"} in payload["unavailable"]
    assert called is False  # invalid icin fetch yok
    await gen.aclose()


async def test_stream_snapshot_no_unavailable_key_when_all_available():
    """(e) unavailable bos -> payload ESKISIYLE BIREBIR (unavailable ANAHTARI YOK)."""
    from app.main import _stream_generator

    _seed_store()
    gen = _stream_generator(_StubRequest(), frozenset({"THYAO"}), ())
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert set(payload.keys()) == {"market", "quotes"}  # birebir eski sema
    await gen.aclose()


async def test_stream_snapshot_full_list_unchanged():
    """(e') symbols parametresi yok (tum-liste) -> eski davranis, unavailable yok."""
    from app.main import _stream_generator

    _seed_store()
    gen = _stream_generator(_StubRequest(), None, ())
    event = await asyncio.wait_for(gen.__anext__(), timeout=2)
    payload = json.loads(event["data"])
    assert set(payload.keys()) == {"market", "quotes"}
    assert set(payload["quotes"]) == {"THYAO", "GARAN"}
    await gen.aclose()


def test_stream_over_symbol_limit_rejected(override_settings):
    """on-demand DoS tavani: gecerli+gecersiz toplami MAX_SYMBOLS_PER_REQUEST asarsa 400."""
    override_settings(max_symbols_per_request=2)
    with TestClient(app) as c:
        r = c.get("/stream?symbols=THYAO,GARAN,ASELS")
        assert r.status_code == 400


def test_stream_over_symbol_limit_counts_invalid_too(override_settings):
    """MED-2 regresyon-kilidi: _check_symbol_limit(valid + invalid) -- toplam
    limite sayilir. Yalnizca THYAO gecerli (1 adet, limitin altinda); iki
    gecersiz sembol EKLENINCE toplam (3) limiti (2) asar. `valid + invalid`
    yerine yanlislikla `valid` sayilsaydi bu istek 400 DONMEZDI (DoS tavani
    gecersiz-sembol-doldurarak bypass edilebilirdi)."""
    override_settings(max_symbols_per_request=2)
    with TestClient(app) as c:
        r = c.get("/stream?symbols=THYAO,TOOLONGSYMBOL1,TOOLONGSYMBOL2")
        assert r.status_code == 400


def test_stream_all_invalid_uses_empty_frozenset_not_none(monkeypatch):
    """MED-1 regresyon-kilidi: WP2 prod-taşma fix'inin HTTP-katmani lincpini.
    `/stream?symbols=<hepsi format-gecersiz>` icin `symbol_filter`, None DEGIL,
    BOS frozenset olmali -- aksi halde RedisStore.subscribe(None) tum-piyasaya
    abone olur (WP2'nin kapattigi prod-bug geri doner). Gercek `_stream_generator`
    yerine argumanlari yakalayan sahte bir generator ile `stream()`'in gercek
    HTTP-katmani (Query -> _parse_symbols_lenient -> symbol_filter atamasi)
    ucdan uca yurutulur (mevcut testler yalniz 400/401/503 hata-yollarinda
    duruyordu, bu satirlar hic calismiyordu)."""
    captured: dict = {}

    async def fake_stream_generator(request, symbol_filter, invalid_symbols=()):
        captured["symbol_filter"] = symbol_filter
        captured["invalid_symbols"] = invalid_symbols
        return
        yield  # asla calismaz -- bu fonksiyonu async generator yapmak icin sart

    monkeypatch.setattr("app.main._stream_generator", fake_stream_generator)
    with TestClient(app) as c:
        r = c.get("/stream?symbols=TOOLONGSYMBOL")
        assert r.status_code == 200
    assert captured["symbol_filter"] == frozenset()
    assert captured["symbol_filter"] is not None
    assert captured["invalid_symbols"] == ("TOOLONGSYMBOL",)
