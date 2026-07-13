"""Pipeline testleri: fetch, cross-validate, commit."""

from dataclasses import replace
from datetime import UTC, datetime

from app.config import settings
from app.models import Quote
from app.pipeline import cross_validate_quotes, fetch_quotes
from app.store import MemoryStore

# 2026-07-06 Pazartesi, seans ici.
_OPEN_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
_YESTERDAY_BAR = datetime(2026, 7, 3, 15, 15, tzinfo=UTC)


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


async def test_cross_validate_excludes_own_source_as_reference(monkeypatch):
    """H3: quote'un KENDI kaynagi referans olarak secilmemeli (totolojik dogrulama
    sahte guven verir -- ayni kaynaktan iki farkli cagri her zaman ayni fiyati
    dondurur). VALIDATE_PROVIDERS=[yahoo_chart, isyatirim]; primary quote
    yahoo_chart'tan geldiginde referans isyatirim'den gelmeli."""
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(
            settings,
            write_cross_validate=True,
            cross_validate_max_pct=1.0,
            validate_providers=["yahoo_chart", "isyatirim"],
        ),
    )

    class SelfSource:
        name = "yahoo_chart"

        async def fetch_quotes(self, symbols):
            # Ayni kaynagin YENI cagrisi -- totolojik referans adayi (dev=0).
            return {s: Quote(symbol=s, price=100.0, source="yahoo_chart") for s in symbols}

    class Independent:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            # Bagimsiz kaynak: gercek %5 sapma.
            return {s: Quote(symbol=s, price=105.0, source="isyatirim") for s in symbols}

    providers = {"yahoo_chart": SelfSource(), "isyatirim": Independent()}
    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: providers.get(name))

    quotes = {"THYAO": Quote(symbol="THYAO", price=100.0, source="yahoo_chart")}
    out = await cross_validate_quotes(quotes)
    # Bagimsiz kaynaga (isyatirim, %5 sapma) gore reddedilmeli -- kendi kaynagina
    # (yahoo_chart, sapma=0) gore SAHTE tutarlilik gorulmemeli.
    assert "THYAO" not in out


async def test_cross_validate_no_independent_reference_is_fail_quiet(monkeypatch):
    """Kendi kaynagi haric hicbir referans yoksa 'dogrulanamadi' sayilir -- fiyat
    SESSIZCE kabul edilir, sahte red/alarm URETILMEZ."""
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(
            settings,
            write_cross_validate=True,
            cross_validate_max_pct=1.0,
            validate_providers=["yahoo_chart"],
        ),
    )

    class SelfSource:
        name = "yahoo_chart"

        async def fetch_quotes(self, symbols):
            return {s: Quote(symbol=s, price=999.0, source="yahoo_chart") for s in symbols}

    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: SelfSource())

    quotes = {"THYAO": Quote(symbol="THYAO", price=100.0, source="yahoo_chart")}
    out = await cross_validate_quotes(quotes)
    assert out["THYAO"].price == 100.0  # degismeden gecti, reddedilmedi


async def test_cross_validate_stale_reference_ignored_fail_quiet(monkeypatch):
    """H2 guard'i referans yolunda da gecerli: isyatirim tek referans olsa bile
    bayat bar dondürüyorsa kullanilamaz -- 'dogrulanamadi' (fail-quiet)."""
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(
            settings,
            write_cross_validate=True,
            cross_validate_max_pct=1.0,
            validate_providers=["isyatirim"],
        ),
    )

    class StaleIsYatirim:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            return {
                s: Quote(symbol=s, price=999.0, source="isyatirim", exchange_time=_YESTERDAY_BAR)
                for s in symbols
            }

    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: StaleIsYatirim())

    quotes = {"THYAO": Quote(symbol="THYAO", price=336.75, source="yahoo_chart")}
    out = await cross_validate_quotes(quotes, now=_OPEN_NOW)
    assert out["THYAO"].price == 336.75  # bayat referans yuzunden reddedilmedi


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


async def test_drift_monitor_ignores_stale_bar_reference(monkeypatch):
    """H2 guard'i drift monitörü yolunda da gecerli: bayat referans karsilastirmaya
    KATILMAZ (sahte drift alarmi uretmez)."""
    from app.pipeline import run_drift_monitor

    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(settings, validate_providers=["isyatirim"], cross_validate_max_pct=1.0),
    )

    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=336.75, source="yahoo_chart"))

    class StaleIsYatirim:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            return {
                s: Quote(symbol=s, price=999.0, source="isyatirim", exchange_time=_YESTERDAY_BAR)
                for s in symbols
            }

    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: StaleIsYatirim())

    result = await run_drift_monitor(store, ["THYAO"], now=_OPEN_NOW)
    # Bayat referans (999.0, gercek fiyattan %197 sapma) karsilastirmaya hic
    # girmedi -- fix olmasaydi bu sahte bir DRIFT_ALERTS tetiklerdi.
    assert result["max_deviation_pct"] == 0.0
