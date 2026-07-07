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
