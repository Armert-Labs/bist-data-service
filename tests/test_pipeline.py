"""Pipeline testleri: fetch, cross-validate, commit."""

from dataclasses import replace
from datetime import UTC, datetime

from app import metrics
from app.config import settings
from app.models import Quote
from app.pipeline import compare_against_references, cross_validate_quotes, fetch_quotes
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
            # MEDIUM-5 (test hijyeni): damgasiz-referans guard'i seans acikken
            # devrededir -- bu testin amaci drift reddi, tazelik degil; guncel
            # bar_time vererek wall-clock seans durumundan bagimsiz kalir.
            return {s: Quote(symbol=s, price=200.0, bar_time=datetime.now(UTC)) for s in symbols}

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
            # Bagimsiz kaynak: gercek %5 sapma. bar_time verilir (MEDIUM-5
            # damgasiz-referans guard'i wall-clock seans durumundan bagimsiz
            # kalsin -- bu testin amaci own-source dislama, tazelik degil).
            return {
                s: Quote(symbol=s, price=105.0, source="isyatirim", bar_time=datetime.now(UTC))
                for s in symbols
            }

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


async def test_cross_validate_prod_default_fails_quiet_without_tradingview(monkeypatch):
    """Patron karari (hukuki, TradingView ToS §3): tradingview varsayilan
    VALIDATE_PROVIDERS'tan CIKARILDI. HIGH-2'nin cozdugu tekli-referans
    riski (prod DEFAULT config, override YOK) bilinçli olarak GERI DONDU:
    birincil yahoo_chart'tan gelince tek olasi bagimsiz referans isyatirim'dir;
    isyatirim seans icinde H2 bayat-bar guard'i yuzunden elenirse dogrulama
    BAGIMSIZ REFERANS BULAMAZ. Bu artik SESSIZ BOZULMA degil -- fail-quiet
    (fiyat degismeden gecer) + bist_validate_no_reference_total{reason="stale"}
    sayaci artar (bkz. README/CHANGELOG "bilinen ve kabul edilen sonuc")."""

    class YahooChartSelf:
        name = "yahoo_chart"

        async def fetch_quotes(self, symbols):
            return {s: Quote(symbol=s, price=336.75, source="yahoo_chart") for s in symbols}

    class StaleIsYatirim:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            return {
                s: Quote(symbol=s, price=344.5, source="isyatirim", exchange_time=_YESTERDAY_BAR)
                for s in symbols
            }

    providers = {"yahoo_chart": YahooChartSelf(), "isyatirim": StaleIsYatirim()}
    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: providers.get(name))

    before = metrics.VALIDATE_NO_REFERENCE.labels(reason="stale")._value.get()
    quotes = {"THYAO": Quote(symbol="THYAO", price=336.75, source="yahoo_chart")}
    out = await cross_validate_quotes(quotes, now=_OPEN_NOW)
    after = metrics.VALIDATE_NO_REFERENCE.labels(reason="stale")._value.get()
    # Fail-quiet: fiyat SESSIZCE (reddedilmeden) geciyor -- ama artik gercekten
    # dogrulanmadi, bu da sayaca yansimali (sessiz bozulma degil, olculebilir).
    assert out["THYAO"].price == 336.75
    assert after == before + 1


async def test_cross_validate_untimed_reference_ignored_during_session(monkeypatch):
    """MEDIUM-5: aggregator seans icinde damgasiz (ne bar_time NE exchange_time)
    bir quote'u guvenilmez sayip dusürür -- referans secici de ayni kurala
    uymali, aksi halde feed'in attigi veri arka kapidan referans olarak
    girerdi (asimetri)."""
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(
            settings,
            write_cross_validate=True,
            cross_validate_max_pct=1.0,
            validate_providers=["isyatirim"],
        ),
    )

    class UntimedIsYatirim:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            # exchange_time VE bar_time yok -- aggregator bu quote'u seans
            # icinde tamamen dusürürdü (HIGH-1 guard'i).
            return {s: Quote(symbol=s, price=999.0, source="isyatirim") for s in symbols}

    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: UntimedIsYatirim())

    quotes = {"THYAO": Quote(symbol="THYAO", price=336.75, source="yahoo_chart")}
    out = await cross_validate_quotes(quotes, now=_OPEN_NOW)
    # Damgasiz referans yuzunden reddedilmedi -- fail-quiet oldugu gibi gecti.
    assert out["THYAO"].price == 336.75


async def test_cross_validate_untimed_reference_accepted_when_market_closed(monkeypatch):
    """Kapali seansta damgasizlik kurali gecerli degil (tazelik zaten kural
    disi) -- damgasiz referans yine de karsilastirmaya girebilmeli."""
    monkeypatch.setattr(
        "app.pipeline.settings",
        replace(
            settings,
            write_cross_validate=True,
            cross_validate_max_pct=1.0,
            validate_providers=["isyatirim"],
        ),
    )
    _closed_now = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)  # Pazar

    class UntimedIsYatirim:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            return {s: Quote(symbol=s, price=999.0, source="isyatirim") for s in symbols}

    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: UntimedIsYatirim())

    quotes = {"THYAO": Quote(symbol="THYAO", price=336.75, source="yahoo_chart")}
    out = await cross_validate_quotes(quotes, now=_closed_now)
    # %197 sapma referans olarak KABUL EDILIP reddedilmeli (damgasizlik engeli yok).
    assert "THYAO" not in out


async def test_compare_against_references_default_records_metric():
    """LOW-3 varsayilan davranis: record_metrics parametresi verilmezse
    (run_drift_monitor gibi arka plan cagrilari) sayac ARTAR."""
    before = metrics.VALIDATE_NO_REFERENCE.labels(reason="no_data")._value.get()
    primary = {"THYAO": Quote(symbol="THYAO", price=336.75, source="yahoo_chart")}
    compare_against_references(primary, ["THYAO"], {}, now=_OPEN_NOW)
    after = metrics.VALIDATE_NO_REFERENCE.labels(reason="no_data")._value.get()
    assert after == before + 1


async def test_compare_against_references_validate_endpoint_skips_metric():
    """LOW-3: `/validate` (main.py) `record_metrics=False` gecer -- insan-teshis
    poll'u arka plan drift-monitörünün gercek 'referans bulunamadi' oranini
    sismemeli."""
    before = metrics.VALIDATE_NO_REFERENCE.labels(reason="no_data")._value.get()
    primary = {"THYAO": Quote(symbol="THYAO", price=336.75, source="yahoo_chart")}
    compare_against_references(primary, ["THYAO"], {}, now=_OPEN_NOW, record_metrics=False)
    after = metrics.VALIDATE_NO_REFERENCE.labels(reason="no_data")._value.get()
    assert after == before


async def test_drift_monitor_prod_default_detects_real_drift(monkeypatch):
    """HIGH-3 regresyon guard (prod DEFAULT config): own-source dislama
    olmadan yahoo_chart kendini dogrulayip VALIDATION_CONSISTENT'i kalici 1'de
    birakiyordu. Prod defaultla gercek bir sapma artik yakalanabilmeli."""
    from app.pipeline import run_drift_monitor

    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=336.75, source="yahoo_chart"))

    class FreshIsYatirimRef:
        name = "isyatirim"

        async def fetch_quotes(self, symbols):
            # bar_time verilir (MEDIUM-5 damgasiz-referans guard'i + H2
            # bayat-bar guard'i wall-clock seans durumundan bagimsiz kalsin --
            # bu testin amaci drift, tazelik degil). tradingview artik
            # varsayilan VALIDATE_PROVIDERS'ta degil (ToS karari) -- tek
            # bagimsiz referans isyatirim'dir.
            return {
                s: Quote(symbol=s, price=360.0, source="isyatirim", bar_time=datetime.now(UTC))
                for s in symbols
            }

    providers = {"isyatirim": FreshIsYatirimRef()}
    monkeypatch.setattr("app.pipeline.aggregator.get_provider", lambda name: providers.get(name))

    result = await run_drift_monitor(store, ["THYAO"], now=_OPEN_NOW)
    assert result["consistent"] is False
    assert result["max_deviation_pct"] > settings.cross_validate_max_pct


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
