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

    async def fetch_quotes(self, symbols, previous=None, now=None, *, count_toward_cooldown=False):
        return await super().fetch_quotes(
            symbols,
            previous=previous,
            now=now or _CLOSED_NOW,
            count_toward_cooldown=count_toward_cooldown,
        )


def _agg(providers):
    agg = _TestAggregator.__new__(_TestAggregator)
    agg._providers = [(p, CircuitBreaker(p.name)) for p in providers]
    agg._sanity_reject_since = {}
    agg._sanity_persist = {}
    agg._guard_drop_streak = {}
    agg._guard_drop_streak_updated_at = {}
    agg._guard_cooldown_until = {}
    agg._cycle_fully_dropped = None
    agg._cooldown_probed_this_cycle = None
    agg._cycle_fresh_symbols = None
    agg._cycle_guard_dropped_candidates = None
    agg._cycle_previous = None
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


async def test_single_source_escape_rejects_without_persistence(override_settings, fake_clock):
    """CRITICAL-1: tek kaynakli dunyada (coklu-kaynak corroboration YAPISAL
    OLARAK imkansiz -- bkz. config.py PROVIDERS yorumu) salt zaman gecmesi
    YETMEZ. Her turda FARKLI bir aykiri fiyat gelirse (gecici tick hatasi
    paterni, ISRAR YOK) escape HICBIR ZAMAN tetiklenmemeli -- tek bozuk
    kaynagin rastgele fiyati commit edilip `previous`'i zehirlemesin."""
    override_settings(sanity_reject_escape_seconds=900.0, sanity_escape_persist_rounds=3)
    quotes = {"THYAO": Quote(symbol="THYAO", price=25.0)}
    agg = _agg([FakeProvider("yahoo", quotes)])
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500
    quotes["THYAO"] = Quote(symbol="THYAO", price=40.0)  # farkli aykiri fiyat -- israr YOK
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 500  # kumulatif 1000 >= 900 (zaman penceresi dolardi)
    quotes["THYAO"] = Quote(symbol="THYAO", price=25.0)
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res  # zaman penceresi dolsa da: TEK kaynak + ISRAR YOK


async def test_single_source_escape_via_persistence(override_settings, fake_clock):
    """CRITICAL-1(b): TEK kaynak ayni (tolerans icindeki) aykiri fiyati
    SANITY_ESCAPE_PERSIST_ROUNDS ardisik TURDA tekrarlarsa kabul edilir --
    coklu-kaynak sartina (candidates>=2) BAGIMLI DEGIL. Bedelsiz/split
    sonrasi TEK intraday kaynakli (yahoo_chart-only) production zincirinde
    escape'in TEK canli yoludur -- eskiden (candidates>=2 sarti) bu senaryoda
    escape YAPISAL OLARAK asla tetiklenemez, sembol KALICI olarak kilitlenirdi."""
    override_settings(sanity_reject_escape_seconds=900.0, sanity_escape_persist_rounds=3)
    before = metrics.SANITY_ESCAPES.labels(
        provider="yahoo_chart", reason="persistence"
    )._value.get()
    quotes = {"THYAO": Quote(symbol="THYAO", price=25.0, source="yahoo_chart")}
    agg = _agg([FakeProvider("yahoo_chart", quotes)])
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})  # 1. tur
    fake_clock.now += 60
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})  # 2. tur
    fake_clock.now += 60  # zaman penceresi (900sn) HENUZ dolmadi -- persist sayaci 3. tur
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert res["THYAO"].price == 25.0
    after = metrics.SANITY_ESCAPES.labels(provider="yahoo_chart", reason="persistence")._value.get()
    assert after == before + 1


async def test_single_source_escape_persistence_gap_resets_streak(override_settings, fake_clock):
    """CRITICAL-1(b): ardisiklik sarti -- ayni (aykiri) fiyat N turda
    tekrarlansa bile araya escape penceresinden (900sn) UZUN bir bosluk
    (restart/kesinti) girerse sayac sifirlanir; ardisiklik yeniden bastan
    saymalidir (kesintisiz red penceresiyle -- _sanity_reject_since -- ayni
    felsefe)."""
    override_settings(sanity_reject_escape_seconds=900.0, sanity_escape_persist_rounds=3)
    quotes = {"THYAO": Quote(symbol="THYAO", price=25.0, source="yahoo_chart")}
    agg = _agg([FakeProvider("yahoo_chart", quotes)])
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})  # persist=1
    fake_clock.now += 2000  # escape penceresinden UZUN bosluk -- ardisiklik BOZULUR
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})  # persist sifirdan 1
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
    # Iki BAGIMSIZ escape anahtari var (corroboration icin SANITY_REJECT_
    # ESCAPE_SECONDS, israr teyidi icin SANITY_ESCAPE_PERSIST_ROUNDS) --
    # "escape tamamen kapali" testi ikisini de sifirlamali.
    override_settings(sanity_reject_escape_seconds=0.0, sanity_escape_persist_rounds=0)
    agg = _agg(_two_agreeing_providers())
    await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 5000
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res
    assert agg._sanity_reject_since == {}  # kapaliyken damga birakilmamali
    assert agg._sanity_persist == {}


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


async def test_wrong_currency_payload_not_accepted_on_first_round(override_settings):
    """HIGH-2 (review-4, GUVENLIK): eski `_internally_consistent` (CRITICAL-1a)
    escape'i KALDIRILDI -- kanitlanmis acik: kaynak THYAO icin yanlislikla bir
    USD satiri dondururse (price=2.50, previous_close=2.40) bu ikisi BIRLIKTE
    kaydigi icin kendi icinde tutarli GORUNUR (%4.2 degisim) VE previous_close
    bizim TRY fiyatimizdan (100) da "onemli olcude farkli" GORUNUR -- eski
    tasarim bunu TEK TURDA, teyitsiz "bedelsiz/split" sanip kabul ediyordu.
    Guard'in var olus sebebi olan hata sinifinin (yanlis-birim/yanlis-
    enstruman) TAM KENDISI escape'i aciyordu. Artik boyle bir payload ILK
    turda REDDEDILIR (yalniz israr teyidi -- 3 ardisik tur -- kalan tek yol)."""
    quote = Quote(symbol="THYAO", price=2.50, previous_close=2.40, source="yahoo_chart")
    agg = _agg([FakeProvider("yahoo_chart", {"THYAO": quote})])
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert "THYAO" not in res


async def test_wrong_currency_payload_eventually_escapes_if_it_persists(
    override_settings, fake_clock
):
    """HIGH-2 tamamlayici: kaldirilan (a) yolunun yerini alan israr teyidi
    (b) mutlak bir garanti degildir -- BOZUK bir payload da GERCEKTEN 3
    ardisik tur (~3 dk) israr ederse escape eder (kabul edilen taviz: bu,
    tek-turluk ANINDA kabulden cok daha yuksek bir bar'dir ve gecici/rastgele
    bir hata 3 dk boyunca ayni degeri tekrarlamaz; PM/reviewer bu gecikmeyi
    kabul edilebilir buluyor -- bkz. review)."""
    override_settings(sanity_escape_persist_rounds=3)
    quotes = {"THYAO": Quote(symbol="THYAO", price=2.50, previous_close=2.40, source="yahoo_chart")}
    agg = _agg([FakeProvider("yahoo_chart", quotes)])
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 60
    assert "THYAO" not in await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    fake_clock.now += 60
    res = await agg.fetch_quotes(["THYAO"], previous={"THYAO": 100.0})
    assert res["THYAO"].price == 2.50  # 3. turda israr teyidiyle kabul (reason=persistence)


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


async def test_stale_bar_time_dropped_when_exchange_time_absent():
    # HIGH-4: TradingView `time` artik bar_time'a tasiniyor (exchange_time
    # DEGIL); bayat-bar guard'i (Block-1) `bar_time` uzerinden CALISMALI.
    stale_tv = FakeProvider(
        "tradingview",
        {
            "THYAO": Quote(
                symbol="THYAO", price=999.0, bar_time=_YESTERDAY_BAR, source="tradingview"
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
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert res["THYAO"].source == "yahoo_chart"  # stale_tv'nin quote'u Block-1'le dustu


async def test_fresh_bar_time_survives_when_exchange_time_absent():
    # HIGH-4 regresyon kilidi: exchange_time'siz ama bar_time TAZE bir quote
    # yanlislikla "damgasiz" guard'ina (Block-2) TAKILMAMALI. Block-2 eskiden
    # yalniz `exchange_time is None` bakiyordu -- artik exchange_time hic
    # doldurmayan kaynaklar (isyatirim/tradingview) icin bu, bar_time taze
    # olsa BILE HER TURDA yanlislikla tetiklenip feed'i seans icinde fiilen
    # tek kaynaga indirirdi. Tek provider (fallback YOK) ile test edildigi
    # icin dogru davranmazsa sonuc BOS donerdi.
    fresh_tv = FakeProvider(
        "tradingview",
        {"THYAO": Quote(symbol="THYAO", price=336.75, bar_time=_TODAY_BAR, source="tradingview")},
    )
    agg = _agg([fresh_tv])
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert res["THYAO"].source == "tradingview"


async def test_tradingview_fresh_bar_survives_default_chain_during_session():
    """HIGH-4 regresyon kilidi (varsayilan PROVIDERS zinciri): yahoo_chart bu
    sembolu bulamazsa (gap) gapfill TradingView'e duser; TV'nin bugunku
    bar_time'li ama exchange_time'siz quote'u KABUL EDILMELI -- aksi halde
    feed seans icinde fiilen tek kaynaga (yahoo_chart) iner ve isyatirim'e
    (asil son care) her turda bosuna dusulur."""
    yahoo_chart = FakeProvider("yahoo_chart", {})  # THYAO'yu saglamiyor (gap)
    tradingview = FakeProvider(
        "tradingview",
        {"THYAO": Quote(symbol="THYAO", price=336.75, bar_time=_TODAY_BAR, source="tradingview")},
    )
    isyatirim = FakeProvider(
        "isyatirim", {"THYAO": Quote(symbol="THYAO", price=999.0, source="isyatirim")}
    )
    agg = _agg([yahoo_chart, tradingview, isyatirim])
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert res["THYAO"].source == "tradingview"
    assert isyatirim.calls == 0  # TV'nin taze quote'u kabul edildi -- isyatirim'e hic dusulmedi


async def _run_cycle(agg, symbols, *, now=None, batches=None, previous=None):
    """Test yardimcisi: begin_cycle/fetch_quotes(count_toward_cooldown=True)/
    end_cycle sirasini tek bir 'tur' olarak simule eder -- gercek updater
    kullanimini (bkz. updater.py) birebir yansitir. `batches` verilirse
    `symbols` yerine her biri ayri bir fetch_quotes cagrisi (batch) olarak
    kullanilir (HIGH-1: bir turda birden fazla batch). HIGH-3: end_cycle()
    artik (TUR-bazli fail-open kararinin sonucu olan) bir dict DONDURUR --
    updater.py'deki gercek kablolamayi (fail_open_quotes ayrica commit edilir)
    yansitmak icin buraya da MERGE edilir."""
    agg.begin_cycle()
    results = {}
    for batch in batches or [symbols]:
        res = await agg.fetch_quotes(batch, previous=previous, now=now, count_toward_cooldown=True)
        results.update(res)
    results.update(agg.end_cycle(now=now))
    return results


def _backup_intraday_provider():
    # HIGH-2(a) (review-2): cooldown yalniz >=2 intraday-capable kaynak varsa
    # UYGULANIR -- tek kaynagi susturmak feed'i kendi kendine keser. Bu
    # testlerin amaci cooldown STATE MACHINE'inin kendisi (mekanik); ikinci
    # (saglikli) bir kaynak, mekanigin devreye girebilmesi icin gerekli.
    return FakeProvider(
        "yahoo_chart",
        {
            "THYAO": Quote(
                symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR, source="yahoo_chart"
            )
        },
    )


async def test_provider_enters_cooldown_after_repeated_full_guard_drop(override_settings):
    # MEDIUM-7: guard'in symbol_circuit muafiyeti (MEDIUM-3) dogruydu ama
    # bunun yaninda HICBIR fren kalmamisti -- seans icinde yapisal olarak
    # taze veri uretemeyen bir kaynak sonsuza kadar (her turda) sorulmaya
    # devam ederdi (yuzlerce bosuna istek + tur butcesi). N TUR ust uste
    # TAMAMEN guard'la duserse kaynak cooldown'a alinmali (HIGH-1: TUR
    # bazinda -- begin_cycle/end_cycle ile simule edilir).
    override_settings(guard_cooldown_fail_threshold=3, guard_cooldown_seconds=1800.0)
    frozen = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([frozen, _backup_intraday_provider()])
    for _ in range(3):
        await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert frozen.calls == 3
    after = metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get()
    assert after == 1

    # HIGH-2(b): cooldown'dayken bile TUR basina 1 "prob" denemesi yapilir
    # (half-open) -- frozen hep bayat dondugu icin prob BASARISIZ olur,
    # cooldown DEVAM eder (gauge hala 1); calls yalniz 1 artar (her batch
    # degil, TUR basina 1 prob).
    await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert frozen.calls == 4
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 1


async def test_provider_cooldown_expires_and_retries(override_settings, fake_clock):
    override_settings(guard_cooldown_fail_threshold=2, guard_cooldown_seconds=100.0)
    quotes = {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)}
    frozen = FakeProvider("isyatirim", quotes)
    agg = _agg([frozen, _backup_intraday_provider()])
    for _ in range(2):
        await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert frozen.calls == 2
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 1

    # Cooldown suresi (100sn) henuz dolmadi -- yine de half-open prob yapilir
    # (HIGH-2b), basarisiz olur (hala bayat donuyor), cooldown DEVAM eder.
    await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert frozen.calls == 3
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 1

    # Kaynak IYILESIR (taze bar) + cooldown suresi (100sn) tamamen doldu.
    quotes["THYAO"] = Quote(symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR)
    fake_clock.now += 200
    res = await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert frozen.calls == 4  # tamamen acildi, normal sorgulandi
    assert res["THYAO"].price == 336.75
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 0


async def test_cooldown_probe_success_clears_cooldown_immediately(override_settings):
    """HIGH-2(b): cooldown suresi (GUARD_COOLDOWN_SECONDS) HENUZ COK UZAK olsa
    bile -- basarili bir prob cooldown'u ANINDA kaldirir. Feed'in iyilesme
    icin tam cooldown suresini beklemesi gerekmez."""
    override_settings(guard_cooldown_fail_threshold=2, guard_cooldown_seconds=1800.0)
    quotes = {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)}
    frozen = FakeProvider("isyatirim", quotes)
    agg = _agg([frozen, _backup_intraday_provider()])
    for _ in range(2):
        await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 1

    # Saat HIC ilerlemedi (1800 sn cooldown'un cok uzaginda) -- yine de
    # prob BASARILI olunca cooldown ANINDA kalkmali.
    quotes["THYAO"] = Quote(symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR)
    res = await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 0


async def test_single_intraday_source_never_cooldowns(override_settings):
    # HIGH-2(a): TradingView cikarildi + Is Yatirim EOD-only (intraday_capable=
    # False) oldugu icin seans icinde TEK intraday kaynak (yahoo_chart) kaldi.
    # Cooldown'un amaci "bozuk kaynagi dovme, DIGERLERI servis etsin"di --
    # digerleri yoksa bu kaynagi susturmak feed'i KENDI KENDINE keser.
    # Esik ne kadar asilirsa asilsin cooldown ASLA aktiflesmemeli; provider
    # her tur yeniden denenmeye devam etmeli (iyilesme aninda feed donsun).
    override_settings(guard_cooldown_fail_threshold=2, guard_cooldown_seconds=1800.0)
    frozen = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([frozen])  # TEK kaynak -- yararlanacak baska intraday kaynak yok
    for _ in range(10):  # esigi (2) cok asan sayida tur
        await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)
    assert frozen.calls == 10  # HICBIR tur atlanmadi -- cooldown asla devreye girmedi
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 0


async def test_provider_streak_resets_on_recovery(override_settings):
    override_settings(guard_cooldown_fail_threshold=2, guard_cooldown_seconds=1800.0)
    quotes = {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)}
    prov = FakeProvider("isyatirim", quotes)
    agg = _agg([prov])

    await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)  # 1. tam-guard-dusme (TUR 1)
    quotes["THYAO"] = Quote(symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR)
    res = await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)  # iyilesme -- streak sifirlanir
    assert res["THYAO"].price == 336.75

    quotes["THYAO"] = Quote(symbol="THYAO", price=999.0, exchange_time=_YESTERDAY_BAR)
    await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)  # yeniden 1. tam-guard-dusme
    after = metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get()
    assert after == 0  # esik (2) henuz asilmadi -- streak gercekten sifirlanmisti
    assert prov.calls == 3  # hala cooldown'da degil, her seferinde soruldu


async def test_cycle_collapses_multiple_batches_into_single_streak_increment(override_settings):
    """HIGH-1 regresyon kilidi: bir TUR ~13 batch cagrisi uretebilir (updater
    varsayilan BATCH_SIZE=40, ~500 sembol). Batch-bazli sayimda tek bir turda
    esik (3) hemen asilirdi -- ayni turun 3 FARKLI batch'i, 3 AYRI TUR gibi
    sayilmamali."""
    override_settings(guard_cooldown_fail_threshold=3, guard_cooldown_seconds=1800.0)
    frozen = FakeProvider(
        "isyatirim",
        {s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR) for s in ("A", "B", "C")},
    )
    agg = _agg([frozen])
    # TEK TUR icinde 3 batch (HER BIRI TAMAMEN guard'la dusuyor).
    await _run_cycle(agg, None, now=_OPEN_NOW, batches=[["A"], ["B"], ["C"]])
    assert frozen.calls == 3
    after = metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get()
    assert after == 0  # esik 3 ama bu AYNI turun 3 batch'i -- streak sadece 1 arti


async def test_on_demand_call_never_counts_toward_cooldown(override_settings):
    """HIGH-2 regresyon kilidi: on-demand (count_toward_cooldown=False,
    varsayilan) cagrilar -- kac kez tekrarlanirsa tekrarlansin -- provider-
    seviyesi cooldown'u ASLA tetiklememeli. Aksi halde bugun islem gormemis
    TEK bir hissenin tekrarli on-demand sorgusu, koca bir kaynagi TUM
    semboller icin cooldown'a sokabilirdi."""
    override_settings(guard_cooldown_fail_threshold=1, guard_cooldown_seconds=1800.0)
    frozen = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([frozen])
    for _ in range(10):
        await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)  # count_toward_cooldown=False (varsayilan)
    after = metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get()
    assert after == 0
    assert frozen.calls == 10  # hicbir zaman cooldown'a girmedi


async def test_guard_drop_during_opening_grace_does_not_count(override_settings, fake_clock):
    """HIGH-1 acilis toleransi: seans acilisindan hemen sonraki pencerede
    (GUARD_OPEN_GRACE_SECONDS) guard-dususleri streak'e YAZILMAZ -- kaynaklar
    acilisin ilk saniyelerinde henuz dunku barlarini guncellemiyor olabilir
    (yapisal gecikme, kalici ariza degil)."""
    override_settings(guard_cooldown_fail_threshold=1, guard_open_grace_seconds=300.0)
    # TR 10:01 -- acilistan (10:00) 60 sn sonra, grace penceresi (300 sn) icinde.
    near_open = datetime(2026, 7, 6, 7, 1, tzinfo=UTC)
    frozen = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=1.0, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([frozen])
    await _run_cycle(agg, ["THYAO"], now=near_open)
    after = metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get()
    assert after == 0  # esik (1) olsa bile grace penceresinde sayilmadi


async def test_fail_open_within_opening_grace_skips_metric_and_log(override_settings):
    """HIGH-1 (review-4, REGRESYON) kilit testi: veri ~15 dk gecikmeli oldugu
    icin acilisin ilk ~15 dk'sinda kaynagin regularMarketTime'i hala dunku
    gune ait olabilir -- guard TUM sembolleri duser, HER ISLEM GUNU rutin
    olarak fail-open tetiklenir (bu BEKLENEN bir durumdur, ariza degildir).
    Acilis toleransi penceresinde (< GUARD_OPEN_GRACE_SECONDS) fail-open
    quote'lari YINE dondurulur (veri sürekliligi) ama bist_guard_fail_open_total
    sayaci ARTMAZ -- aksi halde bu sayaca bagli bir alarm HER SABAH rutin
    olarak ates alir, gercek bir takvim-hatasi sinyalinden ayirt edilemez
    hale gelirdi (alarm yorgunlugu)."""
    override_settings(guard_fail_open_min_symbols=20, guard_open_grace_seconds=1200.0)
    five_min_after_open = datetime(2026, 7, 6, 7, 5, tzinfo=UTC)  # TR 10:05 (acilis 10:00)
    symbols = [f"SYM{i}" for i in range(20)]
    stale_only = FakeProvider(
        "yahoo_chart",
        {
            s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
            for s in symbols
        },
    )
    agg = _agg([stale_only])
    before = metrics.GUARD_FAIL_OPEN._value.get()
    res = await _run_cycle(agg, symbols, now=five_min_after_open)
    assert len(res) == len(symbols)
    assert all(q.stale for q in res.values())  # veri sürekliligi korunur (stale=true)
    after = metrics.GUARD_FAIL_OPEN._value.get()
    assert after == before  # sayac ARTMADI -- rutin acilis, alarm-degeri korunur


async def test_fail_open_outside_opening_grace_fires_metric_and_log(override_settings):
    """Karsit durum: acilis toleransi penceresi DISINDA (>= GUARD_OPEN_GRACE_
    SECONDS) hala TUM sembol guard'la duserse bu artik GERCEK bir sorundur
    (bkz. review) -- sayac eskisi gibi artar."""
    override_settings(guard_fail_open_min_symbols=20, guard_open_grace_seconds=1200.0)
    twenty_five_min_after_open = datetime(2026, 7, 6, 7, 25, tzinfo=UTC)  # TR 10:25
    symbols = [f"SYM{i}" for i in range(20)]
    stale_only = FakeProvider(
        "yahoo_chart",
        {
            s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
            for s in symbols
        },
    )
    agg = _agg([stale_only])
    before = metrics.GUARD_FAIL_OPEN._value.get()
    res = await _run_cycle(agg, symbols, now=twenty_five_min_after_open)
    assert len(res) == len(symbols)
    after = metrics.GUARD_FAIL_OPEN._value.get()
    assert after == before + 1  # pencere disinda -- GERCEK sorun, sayac artar


async def test_guard_drop_streak_ages_out(override_settings, fake_clock):
    """MEDIUM-2: streak'in yaslanmasi -- son artistan uzun sure (esik) sonra
    yeni bir tam-dusme gelirse eski streak GECERSIZ sayilir (sifirdan
    baslar) -- aksi halde sabah erken saatte birikmis bir streak saatlerce
    durup ogleden sonraki TEK kotu turla cooldown'a donusebilirdi."""
    override_settings(
        guard_cooldown_fail_threshold=2,
        guard_cooldown_seconds=1800.0,
        guard_drop_streak_max_age_seconds=900.0,
    )
    frozen = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=1.0, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([frozen])
    await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)  # 1. tam-dusme (streak=1)
    fake_clock.now += 1000  # max_age (900 sn) asildi
    await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)  # streak yaslanip sifirlandi, sonra 1 oldu
    after = metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get()
    assert after == 0  # esik (2) henuz asilmadi -- eski streak sayilmadi
    assert frozen.calls == 2


async def test_intraday_incapable_provider_skipped_entirely_while_market_open():
    """YAPISAL ONERI: EOD-only (intraday_capable=False) kaynak seans acikken
    HIC SORGULANMAZ -- guncelleme boyunca guard zaten her turda dusurecegi
    icin sorgulamak yapisal olarak bosuna bir istektir."""

    class EodOnly(FakeProvider):
        intraday_capable = False

    eod = EodOnly(
        "isyatirim", {"THYAO": Quote(symbol="THYAO", price=1.0, exchange_time=_YESTERDAY_BAR)}
    )
    fresh = FakeProvider(
        "yahoo_chart",
        {
            "THYAO": Quote(
                symbol="THYAO", price=336.75, exchange_time=_TODAY_BAR, source="yahoo_chart"
            )
        },
    )
    agg = _agg([eod, fresh])
    res = await agg.fetch_quotes(["THYAO"], now=_OPEN_NOW)
    assert res["THYAO"].price == 336.75
    assert eod.calls == 0  # seans acikken HIC sorulmadi


async def test_intraday_incapable_provider_still_asked_while_market_closed():
    class EodOnly(FakeProvider):
        intraday_capable = False

    eod = EodOnly(
        "isyatirim", {"THYAO": Quote(symbol="THYAO", price=1.0, exchange_time=_YESTERDAY_BAR)}
    )
    agg = _agg([eod])
    res = await agg.fetch_quotes(["THYAO"], now=_CLOSED_NOW)
    assert res["THYAO"].price == 1.0
    assert eod.calls == 1  # kapali piyasada normal sorgulanir


async def test_single_source_representative_batch_triggers_fail_open(override_settings):
    """HIGH-1 (review-2): fail-open esigi KAYNAK SAYISINA degil BATCH
    BUYUKLUGUNE baglidir. TradingView cikarildi + Is Yatirim EOD-only oldugu
    icin seans icinde TEK intraday kaynak (yahoo_chart) kaldi -- "en az 2
    kaynak" sarti bu dunyada YAPISAL OLARAK asla saglanamazdi (bilinmeyen
    tatilde tum gun karanlik + sayac hic artmazdi). Artik TEK kaynaktan bile
    TEMSILI BUYUKLUKTE (>= GUARD_FAIL_OPEN_MIN_SYMBOLS) bir batch'in TAMAMI
    guard'la duserse fail-open tetiklenir."""
    override_settings(
        guard_cooldown_fail_threshold=1,
        guard_cooldown_seconds=1800.0,
        guard_fail_open_min_symbols=20,
    )
    before = metrics.GUARD_FAIL_OPEN._value.get()
    symbols = [f"SYM{i}" for i in range(20)]
    stale_only = FakeProvider(
        "yahoo_chart",
        {
            s: Quote(symbol=s, price=100.0 + i, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
            for i, s in enumerate(symbols)
        },
    )
    agg = _agg([stale_only])
    res = await _run_cycle(agg, symbols, now=_OPEN_NOW)
    assert len(res) == len(symbols)
    assert all(q.stale for q in res.values())
    after = metrics.GUARD_FAIL_OPEN._value.get()
    assert after == before + 1
    # Sistemik sinyal -- TEK kaynak bile olsa cooldown'a SOKULMAZ.
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="yahoo_chart")._value.get() == 0


async def test_small_batch_guard_drop_does_not_fail_open(override_settings):
    """HIGH-1 (review-2) sinir durumu: batch TEMSILI BUYUKLUKTE (>=
    GUARD_FAIL_OPEN_MIN_SYMBOLS) DEGILSE fail-open TETIKLENMEMELI -- kucuk
    bir kume (on-demand tek-sembol istekleri dahil) "bugun islem gormedi"
    gibi legitim/lokal bir durumla karistirilmasin; guvenli varsayilan
    normal cooldown yoludur."""
    override_settings(
        guard_cooldown_fail_threshold=1,
        guard_cooldown_seconds=1800.0,
        guard_fail_open_min_symbols=20,
    )
    before = metrics.GUARD_FAIL_OPEN._value.get()
    frozen = FakeProvider(
        "isyatirim",
        {"THYAO": Quote(symbol="THYAO", price=344.5, exchange_time=_YESTERDAY_BAR)},
    )
    agg = _agg([frozen])
    res = await _run_cycle(agg, ["THYAO"], now=_OPEN_NOW)  # tek sembol -- esigin (20) cok altinda
    assert "THYAO" not in res  # fail-open YOK -- normal guard-drop sonucu (bos)
    after = metrics.GUARD_FAIL_OPEN._value.get()
    assert after == before
    # tek intraday kaynak oldugu icin (HIGH-2a) cooldown da UYGULANMAZ.
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 0


async def test_escape_unaffected_by_guard_drop_in_same_batch(override_settings, fake_clock):
    """LOW-1: fail-open'in eskiden erken `return result`'i AYNI batch'teki
    ILGISIZ (guard'la degil SANITY ile reddedilen) sembollerin kacis (escape)
    firsatini sessizce atliyordu. HIGH-3'ten sonra fail-open artik batch
    donusune HIC DOKUNMUYOR (karari yalniz end_cycle()'da verilir) -- bu
    yuzden escape blogu, ayni batch'te guard'la dusen baska semboller olsa
    bile HER ZAMAN normal calisir (yapisal olarak artik BLOKLANAMAZ)."""
    override_settings(
        guard_cooldown_fail_threshold=1,
        guard_fail_open_min_symbols=20,
        sanity_reject_escape_seconds=900.0,
    )
    stale_symbols = [f"SYM{i}" for i in range(20)]
    quotes_a = {
        s: Quote(symbol=s, price=100.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
        for s in stale_symbols
    }
    quotes_a["ODDBALL"] = Quote(
        symbol="ODDBALL", price=25.0, exchange_time=_TODAY_BAR, source="yahoo_chart"
    )
    provider_a = FakeProvider("yahoo_chart", quotes_a)
    provider_b = FakeProvider(
        "tradingview",
        {"ODDBALL": Quote(symbol="ODDBALL", price=25.1, bar_time=_TODAY_BAR, source="tradingview")},
    )
    agg = _agg([provider_a, provider_b])
    symbols = [*stale_symbols, "ODDBALL"]
    previous = {"ODDBALL": 100.0}

    # HIGH-3: bu senaryoyu (TEK batch = TEK cycle) gercek updater'in olagan
    # kullanimini yansitmak icin begin_cycle/end_cycle ile sarmalar.
    agg.begin_cycle()
    # Kesintisiz red penceresi: ardisik +=500 artislarla (HER ARA 900sn escape
    # esiginin ALTINDA kalmali -- aksi halde "gece/kesinti bosluğu" sayilip
    # pencere sifirlanir, bkz. _is_sane); kumulatif 1000 >= 900'de escape acilir.
    res1 = await agg.fetch_quotes(
        symbols, previous=previous, now=_OPEN_NOW, count_toward_cooldown=True
    )
    assert "ODDBALL" not in res1
    fake_clock.now += 500
    res_mid = await agg.fetch_quotes(
        symbols, previous=previous, now=_OPEN_NOW, count_toward_cooldown=True
    )
    assert "ODDBALL" not in res_mid
    fake_clock.now += 500  # kumulatif 1000 >= 900, red kesintisiz surdu

    res2 = await agg.fetch_quotes(
        symbols, previous=previous, now=_OPEN_NOW, count_toward_cooldown=True
    )
    assert res2.get("ODDBALL") is not None and res2["ODDBALL"].price == 25.0  # escape calisti
    assert all(s not in res2 for s in stale_symbols)  # guard'la dustu, batch donusu hala bos

    # TUR sonu: bu cycle'da ODDBALL fresh oldugu icin ("TUM watchlist sifir
    # taze quote uretti mi" sorusunun cevabi HAYIR) fail-open TETIKLENMEZ --
    # 20 guard-dusmus sembol bu TUR icin normal (fail-open'siz) "veri yok"
    # olarak kalir; bu, TEK bir sembolun escape'i digerlerini "sistemik degil"
    # gostermesi acisindan dogru/beklenen bir etkilesimdir (bkz.
    # test_cycle_wide_guard_drop_triggers_fail_open_even_with_small_remainder_batch
    # icin TAMAMEN karanlik bir cycle senaryosu).
    fail_open_result = agg.end_cycle(now=_OPEN_NOW)
    assert fail_open_result == {}


async def test_cycle_wide_guard_drop_triggers_fail_open_even_with_small_remainder_batch(
    override_settings,
):
    """HIGH-3(i): eski (batch-bazli) tasarimda watchlist/BATCH_SIZE
    boluminden kalan KUCUK son batch (orn. 525/40 = kalan 5) esigi (20) hicbir
    zaman asamayacagi icin sistemik bir kesinti sirasinda bile fail-open'dan
    YARARLANAMAZDI (korunma `len(watchlist) % BATCH_SIZE`'a bagliydi).
    Cycle-bazli tasarimda esik CYCLE'in TOPLAM guard-dusen aday sayisina
    uygulanir -- kucuk son batch de dahil TUM sembolleri kurtarir."""
    override_settings(guard_fail_open_min_symbols=20, guard_cooldown_fail_threshold=1)
    big_batch = [f"SYM{i}" for i in range(17)]
    small_remainder_batch = ["A", "B", "C"]  # tek basina esigin (20) COK altinda
    quotes = {
        s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
        for s in (*big_batch, *small_remainder_batch)
    }
    stale_only = FakeProvider("yahoo_chart", quotes)
    agg = _agg([stale_only])
    res = await _run_cycle(agg, None, now=_OPEN_NOW, batches=[big_batch, small_remainder_batch])
    # Cycle TOPLAMI (17+3=20) esigi karsiladigi icin remainder batch'teki
    # semboller de (tek basina hicbir zaman esigi asamayacakken) kurtarildi.
    assert len(res) == len(big_batch) + len(small_remainder_batch)
    assert all(q.stale for q in res.values())
    for s in small_remainder_batch:
        assert res[s].stale is True


async def test_end_cycle_aborted_skips_fail_open_entirely(override_settings):
    """MEDIUM-1 (review-4): iptal/timeout veya erken durdurma ile YARIM kalan
    bir tur "hicbir sembol taze degil" olcusunu yalniz kosulan az sayida
    batch'e dayandirir -- TUM watchlist icin sistemik kanit SAYILMAZ.
    `end_cycle(aborted=True)` fail-open'i (metrik/log/donus degeri) HIC
    degerlendirmemeli, bos dict donmeli."""
    override_settings(guard_fail_open_min_symbols=20)
    symbols = [f"SYM{i}" for i in range(20)]
    stale_only = FakeProvider(
        "yahoo_chart",
        {
            s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
            for s in symbols
        },
    )
    agg = _agg([stale_only])
    agg.begin_cycle()
    res = await agg.fetch_quotes(symbols, now=_OPEN_NOW, count_toward_cooldown=True)
    assert res == {}  # guard hepsini dusurdu (normal batch donusu)
    before = metrics.GUARD_FAIL_OPEN._value.get()
    fail_open_result = agg.end_cycle(now=_OPEN_NOW, aborted=True)
    assert fail_open_result == {}  # aborted=True -- fail-open HIC degerlendirilmedi
    after = metrics.GUARD_FAIL_OPEN._value.get()
    assert after == before  # sayac artmadi


async def test_fail_open_still_applies_sanity_check(override_settings):
    """MEDIUM-1: fail-open'in KENDISI sanity-check'i ATLAMAMALI -- aksi halde
    absurt bir fiyat + bayat damga TOGETHER commit edilip `previous`'i kalici
    olarak zehirleyebilir (CRITICAL-1 ile birlesince zehir KALICI olurdu).
    Fail-open adaylarindan biri BIZIM bildigimiz previous'a gore absurt
    (>%60 degisim) ise o sembol fail-open'dan da GECMEMELI; digerleri normal
    kurtarilir."""
    override_settings(guard_fail_open_min_symbols=20, guard_cooldown_fail_threshold=1)
    symbols = [f"SYM{i}" for i in range(19)] + ["POISON"]
    quotes = {
        s: Quote(symbol=s, price=100.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
        for s in symbols[:-1]
    }
    # POISON: guard'in dusurdugu (bayat bar) aday absurt bir fiyat tasiyor --
    # previous'a gore (%900) kesinlikle absurt, gercek bir fiyat degil.
    quotes["POISON"] = Quote(
        symbol="POISON", price=999.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart"
    )
    stale_only = FakeProvider("yahoo_chart", quotes)
    agg = _agg([stale_only])
    previous = {"POISON": 100.0}
    res = await _run_cycle(agg, symbols, now=_OPEN_NOW, previous=previous)
    assert "POISON" not in res  # sanity reddetti -- fail-open bunu KURTARAMADI
    assert len(res) == len(symbols) - 1  # digerleri normal fail-open ile kurtarildi
    assert all(res[s].stale for s in symbols if s != "POISON")


async def test_fail_open_in_one_batch_vetoes_whole_cycle_streak(override_settings):
    """LOW-2: bir TURDA (cycle) HERHANGI bir batch fail-open'a girerse, AYNI
    turdaki BASKA batch'lerin 'tam dustu' bilgisi de suphelidir -- fail-open
    sistemik bir isarettir. Tum turun guard-drop degerlendirmesi (streak
    +1/cooldown) bu durumda VETO edilir (bkz. end_cycle)."""
    override_settings(guard_cooldown_fail_threshold=1, guard_fail_open_min_symbols=20)
    big_batch = [f"SYM{i}" for i in range(20)]  # esigi (20) tek basina asar -- fail-open
    small_batch = ["THYAO"]  # esigin ALTINDA -- TEK BASINA fail-open tetiklemezdi
    quotes = {
        s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
        for s in (*big_batch, *small_batch)
    }
    frozen = FakeProvider("yahoo_chart", quotes)
    # >=2 intraday-capable kaynak (HIGH-2a'nin tek-kaynak muafiyetiyle
    # KARISMASIN diye) -- boylece cooldown'un aktiflesMEMESI SADECE LOW-2'nin
    # veto'suna atfedilebilir.
    backup = FakeProvider("isyatirim", {})
    agg = _agg([frozen, backup])
    await _run_cycle(agg, None, now=_OPEN_NOW, batches=[big_batch, small_batch])
    # big_batch fail-open'a girdi; small_batch TEK BASINA esigi asmazdi ama
    # AYNI turda oldugu icin streak'e HIC YAZILMADI -- esik (1) olsa bile
    # cooldown asla aktiflesmedi.
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="yahoo_chart")._value.get() == 0


async def test_production_default_chain_fail_open_with_real_isyatirim_provider(
    override_settings, monkeypatch
):
    """MEDIUM-3: testler URETIM varsayilan zincirini (PROVIDERS=[yahoo_chart,
    isyatirim], GERCEK IsYatirimProvider.intraday_capable=False) hic
    kosmuyordu -- HIGH-1'in fail-open kacisi tam da bu yuzdendi (eski testler
    sevk edilmeyen bir zinciri -- yahoo_chart+tradingview -- veya sahte
    intraday-capable FakeProvider kullaniyordu). GERCEK `Aggregator()`
    (gercek `IsYatirimProvider` dahil) ile: seans acikken isyatirim HIC
    sorulmaz (gercek siniftan intraday_capable=False); yahoo_chart TEK
    basina temsili buyuklukte bir batch'i TAMAMEN guard'la duserse fail-open
    TETIKLENMELI."""
    override_settings(
        providers=["yahoo_chart", "isyatirim"],
        guard_fail_open_min_symbols=20,
        guard_cooldown_fail_threshold=1,
    )
    agg = Aggregator()  # GERCEK PROVIDERS listesi (gercek yahoo_chart + isyatirim siniflari)
    yahoo_chart_provider = agg.get_provider("yahoo_chart")
    isyatirim_provider = agg.get_provider("isyatirim")
    assert yahoo_chart_provider is not None
    assert isyatirim_provider is not None
    assert isyatirim_provider.intraday_capable is False  # GERCEK siniftan gelen deger

    symbols = [f"SYM{i}" for i in range(20)]

    async def fake_yahoo_fetch(syms):
        return {
            s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
            for s in syms
        }

    async def fail_if_called(syms):
        raise AssertionError("isyatirim seans icinde (intraday_capable=False) HIC sorulmamaliydi")

    monkeypatch.setattr(yahoo_chart_provider, "fetch_quotes", fake_yahoo_fetch)
    monkeypatch.setattr(isyatirim_provider, "fetch_quotes", fail_if_called)

    # HIGH-3: fail-open karari artik end_cycle()'da verilir -- gercek
    # updater'in begin_cycle/fetch_quotes(count_toward_cooldown=True)/
    # end_cycle sirasini yansitir (bkz. _run_cycle).
    res = await _run_cycle(agg, symbols, now=_OPEN_NOW)
    assert len(res) == len(symbols)
    assert all(q.stale for q in res.values())
    # Sistemik sinyal -- gercek isyatirim de dahil hicbir kaynak cooldown'a girmez.
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="yahoo_chart")._value.get() == 0
    assert metrics.PROVIDER_GUARD_COOLDOWN.labels(provider="isyatirim")._value.get() == 0


async def test_on_demand_call_never_triggers_fail_open(override_settings):
    """HIGH-3(ii): fail-open karari yalniz begin_cycle/end_cycle SARMALAYAN
    (updater'in yapilandirilmis TUR dongusu) cagrilar icin degerlendirilir.
    On-demand `/quotes?symbols=` gibi cagrilar begin_cycle() HIC CAGIRMAZ --
    esik (20) asilacak kadar cok cache-miss sembol istense bile fail-open
    ASLA tetiklenmemeli (eskiden batch-bazli karar count_toward_cooldown'a
    bakmadigi icin bunu tetikleyebiliyordu, bkz. review)."""
    override_settings(guard_fail_open_min_symbols=20)
    symbols = [f"SYM{i}" for i in range(30)]
    stale_only = FakeProvider(
        "yahoo_chart",
        {
            s: Quote(symbol=s, price=1.0, exchange_time=_YESTERDAY_BAR, source="yahoo_chart")
            for s in symbols
        },
    )
    agg = _agg([stale_only])
    before = metrics.GUARD_FAIL_OPEN._value.get()
    # begin_cycle() HIC cagrilmadi -- tipik on-demand kullanim.
    res = await agg.fetch_quotes(symbols, now=_OPEN_NOW)
    assert res == {}
    after = metrics.GUARD_FAIL_OPEN._value.get()
    assert after == before


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
