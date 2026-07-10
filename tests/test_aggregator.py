"""Aggregator testleri: failover, sanity-check, circuit breaker (fake provider'larla)."""

import asyncio

import app.aggregator as aggregator_module
import pytest
from app.aggregator import Aggregator
from app.models import HistoryResponse, Quote
from app.providers.base import CircuitBreaker, Provider


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


def _agg(providers):
    agg = Aggregator.__new__(Aggregator)
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
