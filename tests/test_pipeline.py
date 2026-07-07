"""Pipeline testleri: fetch, cross-validate, commit."""

from dataclasses import replace

from app.config import settings
from app.models import Quote
from app.pipeline import cross_validate_quotes, fetch_quotes
from app.store import MemoryStore


async def test_fetch_quotes_passes_previous(monkeypatch):
    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=100.0))

    captured: dict = {}

    async def fake_agg(symbols, previous=None):
        captured["previous"] = previous
        return {s: Quote(symbol=s, price=105.0) for s in symbols}

    monkeypatch.setattr("app.pipeline.aggregator.fetch_quotes", fake_agg)
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(settings, write_cross_validate_on_demand=False),
    )

    res = await fetch_quotes(store, ["THYAO"], cross_validate=False)
    assert res["THYAO"].price == 105.0
    assert captured["previous"] == {"THYAO": 100.0}


async def test_cross_validate_rejects_drift(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(settings, write_cross_validate=True, cross_validate_max_pct=1.0),
    )

    class FakeRef:
        name = "yahoo_chart"

        async def fetch_quotes(self, symbols):
            return {s: Quote(symbol=s, price=200.0) for s in symbols}

    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: FakeRef())

    quotes = {"THYAO": Quote(symbol="THYAO", price=100.0)}
    out = await cross_validate_quotes(quotes)
    assert "THYAO" not in out


async def test_fetch_and_commit_stamps_and_persists(monkeypatch):
    """Gercek zincir: fetch -> commit -> store'da updated_at + market_state."""
    from app.pipeline import fetch_and_commit
    from app.store import MemoryStore

    store = MemoryStore()
    await store.connect()

    async def fake_agg_fetch(symbols, previous=None):
        return {s: Quote(symbol=s, price=10.0) for s in symbols}

    monkeypatch.setattr("app.pipeline.aggregator.fetch_quotes", fake_agg_fetch)
    quotes = await fetch_and_commit(store, ["THYAO"], cross_validate=False)
    assert quotes["THYAO"].price == 10.0

    got = await store.get_quote("THYAO")
    assert got is not None
    assert got.updated_at is not None
    assert got.market_state in ("OPEN", "CLOSED")
