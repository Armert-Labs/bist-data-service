"""Aggregator testleri: failover, sanity-check, circuit breaker (fake provider'larla)."""

import asyncio
from datetime import UTC, datetime

import app.aggregator as aggregator_module
import pytest
from app import metrics
from app.aggregator import Aggregator
from app.models import HistoryResponse, Quote
from app.providers.base import CircuitBreaker, Provider

# 2026-07-06 Pazartesi, seans ici (TR 12:00 = UTC 09:00).
_OPEN_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
_TODAY_BAR = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
_YESTERDAY_BAR = datetime(2026, 7, 3, 15, 15, tzinfo=UTC)  # Cuma kapanisi (UTC)
# 2026-07-05 Pazar -- market kapali (hafta sonu), bar'in tazeligi kural disi.
_CLOSED_NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(aggregator_module, "time", clock)
    return clock


class FakeProvider(Provider):
    def __init__(self, name, quotes=None, fail=False):
        self.name = name
        self._quotes = quotes or {}
        self._fail = fail
        self.calls = 0

    async def fetch_quotes(self, symbols):
        self.calls += 1
        if self._fail:
            raise RuntimeError("kaynak coktu")
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    async def fetch_history(self, symbol, period, interval):
        return HistoryResponse(symbol=symbol, period=period, interval=interval, bars=[])


class _TestAggregator(Aggregator):
    """`now` verilmezse gercek duvar-saati yerine SABIT kapali-seans anina
    duser -- boylece HIGH-1/H2 guard'lariyla ilgisiz genel testler (failover,
    sanity-check, circuit breaker) test calistirma saatine bagli olarak
    kirilmaz (guard yalnizca seans ACIKKEN devreye girer)."""

    async def fetch_quotes(self, symbols, previous=None, now=None):
        return await super().fetch_quotes(symbols, previous=previous, now=now or _CLOSED_NOW)


def _agg(providers):
    agg = _TestAggregator.__new__(_TestAggregator)
    agg._providers = [(p, CircuitBreaker(p.name)) for p in providers]
    agg._sanity_reject_since = {}
    return agg


async def test_gapfill_prefers_first_source():
    primary = FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=100.0)})
    secondary = FakeProvider("isyatirim", {"THYAO": Quote(symbol="THYAO", price=999.0)})
    agg = _agg([primary, secondary])
    res = await agg.fetch_quotes(["THYAO"])
    assert res["THYAO"].price == 100.0
    assert secondary.calls == 0


async def test_gapfill_fills_missing_symbols():
    primary = FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=100.0)})
    secondary = FakeProvider("isyatirim", {"GARAN": Quote(symbol="GARAN", price=50.0)})
    agg = _agg([primary, secondary])
    res = await agg.fetch_quotes(["THYAO", "GARAN"])
    assert res["THYAO"].price == 100.0
    assert res["GARAN"].price == 50.0
    assert secondary.calls == 1


async def test_failover_falls_back_on_failure():
    failing = FakeProvider("yahoo", fail=True)
    backup = FakeProvider("isyatirim", {"THYAO": Quote(symbol="THYAO", price=50.0)})
    agg = _agg([failing, backup])
    res = await agg.fetch_quotes(["THYAO"])
    assert res["THYAO"].price == 50.0
    assert backup.calls == 1


async def test_partial_response_triggers_fallback():
    partial = FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=100.0)})
    backup = FakeProvider("isyatirim", {"GARAN": Quote(symbol="GARAN", price=50.0)})
    agg = _agg([partial, backup])
    res = await agg.fetch_quotes(["THYAO", "GARAN"])
    assert "THYAO" in res
    assert "GARAN" in res
    assert backup.calls == 1


async def test_gapfill_partial_coverage_keeps_breaker_healthy():
    # Gapfill'de fallback yalnizca onceki kaynaklarin bulamadigi (zor/illikit)
    # sembolleri sorar; her batch FARKLI leftover verir -> dusuk kapsama provider
    # sagligini DEGIL sembol yoklugunu gosterir. Circuit acilmamali (yoksa cok
    # kaynak birbirini bosuna devre disi birakip kapsamayi dusurur).
    prov = FakeProvider("isyatirim", {})  # illikitleri bulamaz, bos doner
    agg = _agg([prov])
    _, breaker = agg._providers[0]
    for i in range(10):  # her tur farkli sembol -> symbol_circuit degil provider breaker
        await agg.fetch_quotes([f"DEADA{i}"])
    assert breaker.healthy
    assert breaker.allow()


async def test_failover_partial_coverage_still_trips_breaker(override_settings):
    # Failover'da provider TUM sembolleri sorar; dusuk kapsama gercek saglik
    # sinyalidir -> circuit acilmaya devam etmeli (regresyon guard).
    override_settings(provider_mode="failover")
    prov = FakeProvider("yahoo", {})  # hep bos doner
    agg = _agg([prov])
    _, breaker = agg._providers[0]
    for i in range(6):
        await agg.fetch_quotes([f"DEADB{i}"])
    assert not breaker.healthy


async def test_sanity_rejects_absurd_jump():
    agg = _agg([FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=999.0)})])
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res


async def test_sanity_allows_normal_move():
    agg = _agg([FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=105.0)})])
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert res["THYAO"].price == 105.0


def _two_agreeing_providers():
    # Split/bedelsiz senaryosu: iki bagimsiz kaynak da yeni (dusuk) fiyati veriyor.
    return [
        FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=25.0)}),
        FakeProvider("isyatirim", {"THYAO": Quote(symbol="THYAO", price=25.1)}),
    ]


async def test_sanity_escape_two_agreeing_sources(override_settings, fake_clock):
    override_settings(sanity_reject_escape_seconds=900.0)
    agg = _agg(_two_agreeing_providers())
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500  # pencere doldu (1000 >= 900), red kesintisiz surdu
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert res["THYAO"].price == 25.0


async def test_sanity_escape_needs_corroboration(override_settings, fake_clock):
    # Tek kaynak ne kadar israr ederse etsin teyitsiz kabul edilmemeli
    # (tek bozuk kaynagin fiyati commit edilip previous'i zehirlemesin).
    override_settings(sanity_reject_escape_seconds=900.0)
    agg = _agg([FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=25.0)})])
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res


async def test_sanity_escape_gap_resets_window(override_settings, fake_clock):
    # Kesintisiz red sarti: fetch bosluklarinda (gece, kesinti) pencere birikmez.
    override_settings(sanity_reject_escape_seconds=900.0)
    agg = _agg(_two_agreeing_providers())
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 2000  # escape'ten uzun bosluk -> yeni pencere
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500  # yeni pencerede henuz 500 sn
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500  # yeni pencere doldu
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert res["THYAO"].price == 25.0


async def test_sanity_escape_disabled_writes_no_state(override_settings, fake_clock):
    override_settings(sanity_reject_escape_seconds=0.0)
    agg = _agg(_two_agreeing_providers())
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 5000
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res
    assert agg._sanity_reject_since == {}  # kapaliyken damga birakilmamali


async def test_sanity_timer_cleared_when_prev_gone(override_settings, fake_clock):
    # Erken kabul yolu (prev yok) bayat damga birakmamali; yoksa 900 sn sonra
    # gelen tek bozuk tick aninda kacis alir.
    override_settings(sanity_reject_escape_seconds=900.0)
    agg = _agg([FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=25.0)})])
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    res = await agg.fetch_quotes(["THYAO"], previous={})
    assert res["THYAO"].price == 25.0
    assert agg._sanity_reject_since == {}


async def test_sanity_escape_timer_resets_on_accept(override_settings, fake_clock):
    override_settings(sanity_reject_escape_seconds=900.0)
    quotes = {"THYAO": Quote(symbol="THYAO", price=25.0)}
    other = {"THYAO": Quote(symbol="THYAO", price=25.1)}
    agg = _agg([FakeProvider("yahoo", quotes), FakeProvider("isyatirim", other)])
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    quotes["THYAO"] = Quote(symbol="THYAO", price=98.0)
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert res["THYAO"].price == 98.0  # normal fiyat kabul, damga silinir
    fake_clock.now += 1000
    quotes["THYAO"] = Quote(symbol="THYAO", price=25.0)
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res  # yeni pencere; hemen kacis olmamali


async def test_history_falls_back_to_source_with_bars():
    from datetime import UTC, datetime

    from app.models import HistoryBar

    class WithBars(FakeProvider):
        async def fetch_history(self, symbol, period, interval):
            return HistoryResponse(
                symbol=symbol,
                period=period,
                interval=interval,
                bars=[HistoryBar(time=datetime(2026, 1, 1, tzinfo=UTC), close=10.0)],
            )

    agg = _agg([FakeProvider("yahoo"), WithBars("isyatirim")])
    res = await agg.fetch_history("THYAO", "1mo", "1d")
    assert len(res.bars) == 1


async def test_open_breaker_skips_provider():
    primary = FakeProvider("yahoo", {"THYAO": Quote(symbol="THYAO", price=100.0)})
    backup = FakeProvider("isyatirim", {"THYAO": Quote(symbol="THYAO", price=99.0)})
    agg = _agg([primary, backup])
    agg._providers[0][1].allow = lambda: False  # devre acik
    res = await agg.fetch_quotes(["THYAO"])
    assert primary.calls == 0  # hic sorulmadi
    assert res["THYAO"].price == 99.0


async def test_all_providers_failing_returns_empty():
    agg = _agg([FakeProvider("yahoo", fail=True), FakeProvider("isyatirim", fail=True)])
    res = await agg.fetch_quotes(["THYAO"])
    assert res == {}  # exception yukari sizmaz, mevcut store verisi korunur


async def test_provider_timeout_falls_back_to_next_source(override_settings):
    # yfinance/curl_cffi wedge senaryosu: bir kaynak sonsuza kadar asilirsa
    # sert timeout onu keser, breaker basarisizlik kaydeder, sonraki kaynaktan
    # veri gelir ve tur SUSMUYOR (asilmiyor).
    override_settings(provider_fetch_timeout=0.05)

    class HangingProvider(FakeProvider):
        async def fetch_quotes(self, symbols):
            self.calls += 1
            await asyncio.Event().wait()  # hicbir zaman donmez

    hanging = HangingProvider("yahoo")
    backup = FakeProvider("isyatirim", {"THYAO": Quote(symbol="THYAO", price=50.0)})
    agg = _agg([hanging, backup])
    res = await agg.fetch_quotes(["THYAO"])
    assert res["THYAO"].price == 50.0
    assert backup.calls == 1
    _, breaker = agg._providers[0]
    assert breaker._failures == 1


async def test_provider_timeout_outer_cancel_does_not_record_failure(override_settings):
    # Disaridan (guncelleme turu butcesi gibi) gelen bir iptal, provider-timeout
    # olarak YUTULMAMALI: CancelledError yukari yayilmali ve breaker'a basarisizlik
    # KAYDEDILMEMELI (dis butce coroutine'i iptal eder, thread'i degil).
    override_settings(provider_fetch_timeout=10.0)  # ic timeout disaridakinden uzun

    class HangingProvider(FakeProvider):
        async def fetch_quotes(self, symbols):
            self.calls += 1
            await asyncio.Event().wait()

    hanging = HangingProvider("yahoo")
    agg = _agg([hanging])
    _, breaker = agg._providers[0]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(agg.fetch_quotes(["THYAO"]), timeout=0.05)
    assert breaker._failures == 0


async def test_history_timeout_falls_back_to_next_source(override_settings):
    # Quote yolundaki ayni wedge korumasi history icin de gecerli olmali:
    # asilan bir history saglayicisi sert timeout ile kesilir, breaker
    # basarisizlik kaydeder, sonraki kaynaktan bar gelir.
    from datetime import UTC, datetime

    from app.models import HistoryBar

    override_settings(provider_fetch_timeout=0.05)

    class HangingHistory(FakeProvider):
        async def fetch_history(self, symbol, period, interval):
            await asyncio.Event().wait()  # hicbir zaman donmez

    class WithBars(FakeProvider):
        async def fetch_history(self, symbol, period, interval):
            return HistoryResponse(
                symbol=symbol,
                period=period,
                interval=interval,
                bars=[HistoryBar(time=datetime(2026, 1, 1, tzinfo=UTC), close=10.0)],
            )

    hanging = HangingHistory("yahoo")
    backup = WithBars("isyatirim")
    agg = _agg([hanging, backup])
    res = await agg.fetch_history("THYAO", "1mo", "1d")
    assert len(res.bars) == 1
    _, breaker = agg._providers[0]
    assert breaker._failures == 1


async def test_history_timeout_outer_cancel_does_not_record_failure(override_settings):
    # Disaridan gelen iptal history yolunda da yutulmamali (quote yolunun
    # simetrigi): CancelledError yukari yayilmali, breaker'a hic kayit dusmemeli.
    override_settings(provider_fetch_timeout=10.0)

    class HangingHistory(FakeProvider):
        async def fetch_history(self, symbol, period, interval):
            await asyncio.Event().wait()

    hanging = HangingHistory("yahoo")
    agg = _agg([hanging])
    _, breaker = agg._providers[0]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(agg.fetch_history("THYAO", "1mo", "1d"), timeout=0.05)
    assert breaker._failures == 0


async def test_stale_bar_rejected_while_market_open_falls_back():
    # H2: seans acikken dunku bar'a dayanan quote (exchange_time=dun) kabul
    # edilmemeli; sonraki kaynaga (gapfill) dusulmeli.
    stale = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)},
    )
    fresh = FakeProvider(
        "yahoo_chart",
        {
            "THYAO": Quote(
                symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR, source="yahoo_chart"
            )
        },
    )
    agg = _agg([stale, fresh])
    before = metrics.STALE_BAR_SKIPPED.labels(provider="isyatirim")._value.get()
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert res["THYAO"].source == "yahoo_chart"
    after = metrics.STALE_BAR_SKIPPED.labels(provider="isyatirim")._value.get()
    assert after == before + 1


async def test_tradingview_stale_bar_rejected_while_market_open_falls_back():
    # HIGH-1 regresyon guard: TradingView artik `time` kolonundan exchange_time
    # uretiyor (bkz. providers/tradingview.py); genel is_stale_bar kurali bu
    # kaynak icin de gecerli olmali -- canli olayin tam tersi (eskiden
    # TradingView damgasiz oldugu icin guard'dan MUAFTI).
    stale_tv = FakeProvider(
        "tradingview",
        {
            "THYAO": Quote(
                symbol="THYAO", price=999.0, exchange_time=_YESTERDAY_BAR, source="tradingview"
            )
        },
    )
    fresh = FakeProvider(
        "yahoo_chart",
        {
            "THYAO": Quote(
                symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR, source="yahoo_chart"
            )
        },
    )
    agg = _agg([stale_tv, fresh])
    before = metrics.STALE_BAR_SKIPPED.labels(provider="tradingview")._value.get()
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert res["THYAO"].source == "yahoo_chart"
    after = metrics.STALE_BAR_SKIPPED.labels(provider="tradingview")._value.get()
    assert after == before + 1


async def test_stale_bar_accepted_when_market_closed():
    # Seans kapaliyken son kapanis mesru veridir; bayat-bar guard'i devreye girmez.
    prov = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([prov])
    res = await agg.fetch_quotes(["THYAO"], now=_CLOSED_NOW)
    assert res["THYAO"].price == 344.5


async def test_todays_bar_accepted_while_market_open():
    prov = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR)},
    )
    agg = _agg([prov])
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75


async def test_missing_exchange_time_rejected_while_market_open_falls_back():
    # HIGH-1 (canli olay): exchange_time URETEMEYEN bir kaynak (eskiden
    # TradingView) guard'dan tamamen MUAF kaliyordu -- 2 yillik bayat bir
    # fiyat "taze" (stale=false, age~0) diye servis edildi. Artik seans
    # ACIKKEN damgasiz quote da "hic gelmemis" sayilir, sonraki kaynaga dusulur.
    no_stamp = FakeProvider("tradingview", {"THYAO": Quote(symbol="THYAO", price=999.0)})
    fresh = FakeProvider(
        "yahoo_chart",
        {
            "THYAO": Quote(
                symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR, source="yahoo_chart"
            )
        },
    )
    agg = _agg([no_stamp, fresh])
    before = metrics.MISSING_EXCHANGE_TIME.labels(provider="tradingview")._value.get()
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert res["THYAO"].source == "yahoo_chart"
    after = metrics.MISSING_EXCHANGE_TIME.labels(provider="tradingview")._value.get()
    assert after == before + 1


async def test_missing_exchange_time_accepted_when_market_closed():
    # Kapali seansta exchange_time zorunlulugu yok (tazelik zaten kural disi).
    prov = FakeProvider("tradingview", {"THYAO": Quote(symbol="THYAO", price=1.0)})
    agg = _agg([prov])
    res = await agg.fetch_quotes(["THYAO"], now=_CLOSED_NOW)
    assert res["THYAO"].price == 1.0


async def test_guard_dropped_symbol_does_not_trip_symbol_circuit():
    # MEDIUM-3: guard'in dusurdugu (bayat bar / damgasiz) semboller
    # symbol_circuit'e HATA olarak yazilmamali -- "bugun islem gormedi" veya
    # "damga yok" provider'in o sembol icin veri VEREMEDIGI anlamina gelmez;
    # aksi halde 3 turda sembol 300sn'lik bir devre-disi'na dusup gec islem
    # goren hisseleri de gereksiz yere atlar.
    from app.symbol_circuit import symbol_circuit

    sym = "CIRCUITGUARD1"
    prov = FakeProvider(
        "isyatirim", {sym: Quote(symbol=sym, price=1.0, exchange_time=_YESTERDAY_BAR)}
    )
    agg = _agg([prov])
    for _ in range(5):  # esik (varsayilan 3) asilacak kadar tekrar
        await agg.fetch_quotes([sym], now=_OPEN_NOW)
    assert symbol_circuit.allow("isyatirim", sym) is True


async def test_history_skips_non_history_provider_and_keeps_quotes():
    """supports_history=False kaynak (orn. TradingView), bos history yuzunden
    breaker'i actirmamali; QUOTE hizmeti korunmali."""
    from datetime import UTC, datetime

    from app.models import HistoryBar

    class NoHistory(FakeProvider):
        supports_history = False

    class WithBars2(FakeProvider):
        async def fetch_history(self, symbol, period, interval):
            return HistoryResponse(
                symbol=symbol,
                period=period,
                interval=interval,
                bars=[HistoryBar(time=datetime(2026, 1, 1, tzinfo=UTC), close=10.0)],
            )

    nh = NoHistory("tradingview", {"THYAO": Quote(symbol="THYAO", price=100.0)})
    agg = _agg([nh, WithBars2("isyatirim")])
    res = await agg.fetch_history("THYAO", "1mo", "1d")
    assert len(res.bars) == 1  # bars ikinci kaynaktan geldi
    assert agg._providers[0][1].state == "closed"  # tradingview breaker ACILMADI
    quotes = await agg.fetch_quotes(["THYAO"])
    assert quotes["THYAO"].price == 100.0  # quote hizmeti hala calisiyor
