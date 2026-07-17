"""BackgroundUpdater._update_once testleri (mock aggregator + in-memory store)."""

import asyncio

import app.symbols as sym_mod
import app.updater as updater_mod
import pytest
from app import metrics
from app.models import Quote
from app.store import MemoryStore
from app.updater import BackgroundUpdater


async def test_update_once_writes_to_store(monkeypatch):
    store = MemoryStore()
    await store.connect()

    async def fake_fetch(symbols, previous=None, **kwargs):
        return {s: Quote(symbol=s, price=100.0) for s in symbols}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO", "GARAN"], store=store)
    count = await up._update_once()

    assert count == 2
    got = await store.get_quote("THYAO")
    assert got is not None and got.price == 100.0
    # market_state atandi mi
    assert got.market_state in ("OPEN", "CLOSED")


async def test_update_once_wraps_batches_in_single_cooldown_cycle(monkeypatch, override_settings):
    """HIGH-1 uctan uca kablolama kaniti: _update_once TUM batch'leri
    begin_cycle()/end_cycle() ile TEK bir 'tur' olarak aggregator'a bildirir
    -- her fetch_quotes cagrisi count_toward_cooldown=True gecer. Bu, guard-
    cooldown'un artik BATCH degil TUR bazinda degerlendirilmesinin (bkz.
    aggregator.begin_cycle/end_cycle) uretim koduna dogru kablolandigini
    dogrular (Aggregator'in kendi mantigi test_aggregator.py'de test edilir)."""
    override_settings(batch_size=1)  # 3 sembol -> 3 AYRI batch cagrisi zorlanir
    store = MemoryStore()
    await store.connect()

    calls = {"begin": 0, "end": 0, "fetch": []}

    def fake_begin_cycle():
        calls["begin"] += 1

    def fake_end_cycle(now=None, aborted=False):
        calls["end"] += 1

    async def fake_fetch(symbols, previous=None, **kwargs):
        calls["fetch"].append(kwargs.get("count_toward_cooldown"))
        return {}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "begin_cycle", fake_begin_cycle)
    monkeypatch.setattr(updater_mod.aggregator, "end_cycle", fake_end_cycle)
    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["A", "B", "C"], store=store)
    await up._update_once()

    assert calls["begin"] == 1  # TUM tur icin TEK begin_cycle
    assert calls["end"] == 1  # TUM tur icin TEK end_cycle
    assert calls["fetch"] == [True, True, True]  # her batch count_toward_cooldown=True gecti


async def test_update_once_commits_end_cycle_fail_open_result(monkeypatch):
    """HIGH-3 uctan uca kablolama kaniti: end_cycle() artik (TUR-bazli
    fail-open kararinin sonucu olan) bir dict DONDURUR -- ilgili batch'ler
    zaten bos donmustu (guard onlari dusurmustu); _update_once bu sonucu
    AYRICA commit etmeli, yoksa fail-open'in kurtardigi veri store'a hic
    yazilmaz (Aggregator'in end_cycle kararinin kendisi test_aggregator.py'de
    test edilir, burada yalniz updater'a dogru KABLOLANDIGI dogrulanir)."""
    store = MemoryStore()
    await store.connect()

    async def fake_fetch(symbols, previous=None, **kwargs):
        return {}  # her batch guard'la dustu -- bu tur icin normal donus bos

    fail_open_quotes = {
        "THYAO": Quote(symbol="THYAO", price=344.5, source="yahoo_chart", stale=True),
        "GARAN": Quote(symbol="GARAN", price=50.0, source="yahoo_chart", stale=True),
    }

    def fake_end_cycle(now=None, aborted=False):
        return fail_open_quotes

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.aggregator, "end_cycle", fake_end_cycle)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO", "GARAN"], store=store)
    count = await up._update_once()

    assert count == 2
    got = await store.get_quote("THYAO")
    assert got is not None and got.price == 344.5 and got.stale is True
    assert metrics.QUOTES_BY_SOURCE.labels(source="yahoo_chart")._value.get() == 2


async def test_update_once_calls_end_cycle_even_when_cancelled_mid_batch(monkeypatch):
    """MEDIUM-2 (review-2): `updater_cycle_timeout` asiminda `_run_cycle`
    (bkz. `_run_cycle`) bu coroutine'e `CancelledError` enjekte eder --
    try/finally OLMADAN `end_cycle()` HIC calismazdi (biriken guard-drop
    bilgisi sessizce kaybolur, hicbir provider'in streak'i ne artar ne
    sifirlanir). Artik iptal edilse bile `end_cycle()` HER ZAMAN cagrilir.

    MEDIUM-1 (review-4): iptal edilen bir tur `end_cycle(aborted=True)`
    cagirmali -- kismi tur sistemik kanit SAYILMAZ (fail-open'i HIC
    degerlendirmemesi gerekir, bkz. test_aggregator.py)."""
    store = MemoryStore()
    await store.connect()

    calls = {"end": 0, "aborted": None}

    def fake_end_cycle(now=None, aborted=False):
        calls["end"] += 1
        calls["aborted"] = aborted

    async def slow_fetch(symbols, previous=None, **kwargs):
        await asyncio.sleep(10)  # asla zamaninda bitmez -- disaridan iptal edilir
        return {}

    monkeypatch.setattr(updater_mod.aggregator, "end_cycle", fake_end_cycle)
    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", slow_fetch)

    up = BackgroundUpdater(symbols_list=["A", "B"], store=store)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(up._update_once(), timeout=0.05)
    assert calls["end"] == 1  # iptal edilse bile end_cycle() cagrildi
    assert calls["aborted"] is True  # MEDIUM-1: kismi tur oldugu acikca bildirildi


async def test_update_once_marks_aborted_on_early_stop(monkeypatch):
    """LOW-1: kapanma sirasinda (`BackgroundUpdater.stop()`) erken durdurma
    da (istisna YOK, normal `break`) kismi tur sayilmali -- `aborted=True`
    ile `end_cycle()`'a bildirilmeli (fail-open'i tetiklememesi icin)."""
    store = MemoryStore()
    await store.connect()

    calls = {"aborted": None}

    def fake_end_cycle(now=None, aborted=False):
        calls["aborted"] = aborted
        return {}

    call_count = {"n": 0}

    async def fake_fetch(symbols, previous=None, **kwargs):
        call_count["n"] += 1
        return {}

    async def no_sleep(*a, **k):
        return None

    up = BackgroundUpdater(symbols_list=["A", "B", "C"], store=store)
    up._stop.set()  # zaten durdurulmus -- ilk batch'ten once break tetiklenir

    monkeypatch.setattr(updater_mod.aggregator, "end_cycle", fake_end_cycle)
    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    await up._update_once()

    assert call_count["n"] == 0  # hicbir batch kosmadi
    assert calls["aborted"] is True


async def test_update_once_passes_previous_for_sanity(monkeypatch):
    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=100.0))

    seen_previous = {}

    async def fake_fetch(symbols, previous=None, **kwargs):
        seen_previous.update(previous or {})
        return {}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)
    await up._update_once()
    assert seen_previous.get("THYAO") == 100.0


async def test_update_once_reports_quotes_by_source(monkeypatch):
    """Item 4: kaynak-dagilim metrigi -- failover'i sessizlikten cikarir."""
    store = MemoryStore()
    await store.connect()

    async def fake_fetch(symbols, previous=None, **kwargs):
        return {
            "THYAO": Quote(symbol="THYAO", price=100.0, source="yahoo_chart"),
            "GARAN": Quote(symbol="GARAN", price=50.0, source="isyatirim"),
        }

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO", "GARAN"], store=store)
    await up._update_once()

    assert metrics.QUOTES_BY_SOURCE.labels(source="yahoo_chart")._value.get() == 1
    assert metrics.QUOTES_BY_SOURCE.labels(source="isyatirim")._value.get() == 1


async def test_update_once_zeroes_out_source_with_no_hits_this_cycle(monkeypatch):
    """Bir kaynak bu tur hic sembol saglamadiysa gauge 0'a dusmeli -- eski
    nonzero deger SESSIZCE kalici olmamali (failover'i gizler)."""
    store = MemoryStore()
    await store.connect()

    calls = {"n": 0}

    async def fake_fetch(symbols, previous=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {s: Quote(symbol=s, price=1.0, source="isyatirim") for s in symbols}
        return {s: Quote(symbol=s, price=1.0, source="yahoo_chart") for s in symbols}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)
    await up._update_once()
    assert metrics.QUOTES_BY_SOURCE.labels(source="isyatirim")._value.get() == 1

    await up._update_once()
    assert metrics.QUOTES_BY_SOURCE.labels(source="isyatirim")._value.get() == 0
    assert metrics.QUOTES_BY_SOURCE.labels(source="yahoo_chart")._value.get() == 1


async def test_run_cycle_times_out_and_recovers(monkeypatch, override_settings):
    """Takilan _update_once tur butcesinde iptal edilmeli; hata yukari sizmamali."""
    override_settings(updater_cycle_timeout=0.05)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    async def hang():
        await asyncio.sleep(5)

    monkeypatch.setattr(up, "_update_once", hang)
    ok = await asyncio.wait_for(up._run_cycle(), timeout=1)
    assert ok is False  # timeout'lu tur 'tamamlandi' sayilmamali (warm-up tekrari)


async def test_run_cycle_runs_update(monkeypatch, override_settings):
    override_settings(updater_cycle_timeout=5.0)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    called = []

    async def fake_update():
        called.append(1)
        return 1

    monkeypatch.setattr(up, "_update_once", fake_update)
    ok = await up._run_cycle()
    assert ok is True
    assert called == [1]


async def test_loop_survives_update_exception(monkeypatch, override_settings):
    """_update_once patlasa bile 7/24 dongusu olmemeli, sonraki turda devam etmeli."""
    override_settings(update_interval=0.01, update_when_closed=True, updater_cycle_timeout=5.0)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("beklenmeyen patlama")
        return 0

    monkeypatch.setattr(up, "_update_once", flaky)
    up.start()
    for _ in range(300):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    await up.stop()
    assert calls["n"] >= 2  # hatadan sonra dongu devam etti


async def test_universe_refresh_expands_symbols(monkeypatch, override_settings):
    """fetch_universe iyi liste dondururse _symbols statik+extra+evren birlesimi olur."""
    override_settings(
        symbol_universe_refresh_enabled=True,
        symbol_universe_min_count=3,
        extra_symbols=["EXTRA1"],
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    fetched = ["THYAO", "GARAN", "AKBNK", "NEWCO"]

    async def fake_universe():
        return fetched

    monkeypatch.setattr(sym_mod, "fetch_universe", fake_universe)
    await up._maybe_refresh_universe()

    got = set(up.symbols)
    # Kayipsizlik: statik taban + extra + evren hepsi icinde.
    assert set(sym_mod.BIST_SYMBOLS).issubset(got)
    assert "EXTRA1" in got
    assert {"NEWCO", "GARAN"}.issubset(got)
    assert up.symbols == sorted(up.symbols)


async def test_universe_refresh_guard_preserves_existing(monkeypatch, override_settings):
    """Yetersiz evren (min_count alti) mevcut listeyi BOZMAZ (guard)."""
    override_settings(
        symbol_universe_refresh_enabled=True,
        symbol_universe_min_count=400,
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO", "GARAN"], store=store)
    before = up.symbols

    async def small_universe():
        return ["ONLY1", "ONLY2"]  # min_count alti

    monkeypatch.setattr(sym_mod, "fetch_universe", small_universe)
    await up._maybe_refresh_universe()

    assert up.symbols == before  # mevcut liste korundu


async def test_universe_refresh_disabled_noop(monkeypatch, override_settings):
    override_settings(symbol_universe_refresh_enabled=False)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    called = {"n": 0}

    async def spy():
        called["n"] += 1
        return ["A", "B", "C", "D", "E"]

    monkeypatch.setattr(sym_mod, "fetch_universe", spy)
    await up._maybe_refresh_universe()
    assert called["n"] == 0  # kapaliyken hic cagrilmaz
    assert up.symbols == ["THYAO"]


async def test_universe_refresh_respects_interval(monkeypatch, override_settings):
    """Ikinci cagri refresh_hours dolmadan fetch_universe'u YENIDEN cagirmaz."""
    override_settings(
        symbol_universe_refresh_enabled=True,
        symbol_universe_min_count=1,
        symbol_universe_refresh_hours=24,
        extra_symbols=[],
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    calls = {"n": 0}

    async def counting():
        calls["n"] += 1
        return ["THYAO", "GARAN", "AKBNK"]

    monkeypatch.setattr(sym_mod, "fetch_universe", counting)
    await up._maybe_refresh_universe()
    await up._maybe_refresh_universe()
    assert calls["n"] == 1  # ikinci cagri interval nedeniyle atlanir


async def test_universe_refresh_runs_before_update_not_concurrent(monkeypatch, override_settings):
    """Refresh dongu basinda (update ONCESI) olmali; _update_once ile es zamanli DEGIL.

    Sira: once refresh (symbols swap), sonra _update_once yeni listeyi gorur.
    """
    override_settings(
        update_interval=0.01,
        update_when_closed=True,
        updater_cycle_timeout=5.0,
        symbol_universe_refresh_enabled=True,
        symbol_universe_min_count=2,
        extra_symbols=[],
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    order = []
    seen_symbols = {}

    async def fake_universe():
        order.append("refresh")
        return ["THYAO", "GARAN", "AKBNK", "SISE"]

    async def fake_update():
        order.append("update")
        seen_symbols["snapshot"] = set(up.symbols)
        return len(up.symbols)

    monkeypatch.setattr(sym_mod, "fetch_universe", fake_universe)
    monkeypatch.setattr(up, "_update_once", fake_update)

    up.start()
    for _ in range(300):
        if "update" in order:
            break
        await asyncio.sleep(0.01)
    await up.stop()

    # Refresh update'ten ONCE calisti.
    assert order[0] == "refresh"
    assert "update" in order
    # _update_once refresh sonrasi genisletilmis listeyi gordu (atomik swap).
    assert {"GARAN", "SISE"}.issubset(seen_symbols["snapshot"])


# --------------------------------------------------------------------------- #
# Wedge fix: guard'sizdi -- _maybe_refresh_universe()/_refresh_age_metric()
# updater_cycle_timeout kapsami DISINDA cagrilir, guard'siz bir Redis/HTTP
# cagrisi asilirsa TUM dongu suresiz kilitleniyordu (kok neden).
# --------------------------------------------------------------------------- #
async def test_loop_recovers_when_universe_refresh_hangs(monkeypatch, override_settings):
    """_maybe_refresh_universe() asyncio.wait_for ile sarili -- asilan cagri
    bu turu atlatir, TUM donguyu KALICI kilitlemez."""
    override_settings(
        updater_guard_timeout=0.05,
        update_interval=0.01,
        update_when_closed=True,
        updater_cycle_timeout=5.0,
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    async def hang_forever():
        await asyncio.sleep(1000)

    calls = {"n": 0}

    async def fast_update():
        calls["n"] += 1
        return 0

    monkeypatch.setattr(up, "_maybe_refresh_universe", hang_forever)
    monkeypatch.setattr(up, "_update_once", fast_update)

    up.start()
    for _ in range(300):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.02)
    await up.stop()
    assert calls["n"] >= 2  # asilan refresh_universe sonraki turu ENGELLEMEDI


async def test_loop_recovers_when_refresh_age_metric_hangs(monkeypatch, override_settings):
    """_refresh_age_metric() asyncio.wait_for ile sarili -- asilan cagri bu
    turu atlatir, TUM donguyu KALICI kilitlemez (asil nuks senaryosu)."""
    override_settings(
        updater_guard_timeout=0.05,
        update_interval=0.01,
        update_when_closed=True,
        updater_cycle_timeout=5.0,
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    async def hang_forever():
        await asyncio.sleep(1000)

    calls = {"n": 0}

    async def fast_update():
        calls["n"] += 1
        return 0

    monkeypatch.setattr(up, "_refresh_age_metric", hang_forever)
    monkeypatch.setattr(up, "_update_once", fast_update)

    up.start()
    for _ in range(300):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.02)
    await up.stop()
    assert calls["n"] >= 2  # asilan refresh_age_metric sonraki turu ENGELLEMEDI


async def test_loop_running_flag_resets_on_external_cancellation(monkeypatch, override_settings):
    """LOW (wedge fix): stop()'un fallback yolu (_task.cancel()) CancelledError'i
    while-dongusune enjekte eder -- try/finally OLMADAN `running` sonsuza dek
    True'da TAKILI kalirdi (surecin sessizce oldugunu gizleyen yanlis bir
    canlilik sinyali)."""
    override_settings(update_when_closed=True, updater_cycle_timeout=1000)
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    async def hang_forever():
        await asyncio.sleep(1000)

    monkeypatch.setattr(up, "_update_once", hang_forever)

    up.start()
    for _ in range(200):
        if up.running:
            break
        await asyncio.sleep(0.01)
    assert up.running is True

    up._stop.set()  # watchdog bunu gorup temiz cikar
    up._task.cancel()  # ana dongu _update_once icinde asili -- disaridan iptal simulasyonu
    with pytest.raises(asyncio.CancelledError):
        await up._task
    assert up.running is False

    if up._watchdog_task is not None:
        await asyncio.wait_for(up._watchdog_task, timeout=2)


async def test_watchdog_hard_exits_when_loop_wedged(monkeypatch, override_settings):
    """Savunma-katmani: guard'lara ragmen (varsayimsal -- ust butcenin bir
    sekilde etkisiz kaldigi senaryo) ana dongu tur tamamlayamazsa bagimsiz
    watchdog Task'i sureci sonlandirir. Testte gercek os._exit yerine enjekte
    edilen _exit_fn cagrisi kanit olarak yeterli (surec-sonlandirmayi
    TETIKLEMEZ)."""
    override_settings(
        updater_watchdog_timeout=0.05,
        updater_watchdog_check_interval=0.01,
        update_when_closed=True,
        updater_cycle_timeout=1000,  # bu senaryoda devre disi gibi davransin
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    exit_calls: list[int] = []
    monkeypatch.setattr(up, "_exit_fn", lambda code: exit_calls.append(code))

    async def hang_forever():
        await asyncio.sleep(1000)

    monkeypatch.setattr(up, "_update_once", hang_forever)

    up.start()
    for _ in range(300):
        if exit_calls:
            break
        await asyncio.sleep(0.02)
    await up.stop()
    assert exit_calls == [1]


async def test_watchdog_does_not_exit_during_healthy_loop(monkeypatch, override_settings):
    """Sagliksiz-pozitif regresyonu: saglikli, hizli turlar atan bir dongu
    watchdog'u ASLA tetiklememeli."""
    override_settings(
        updater_watchdog_timeout=5.0,
        updater_watchdog_check_interval=0.01,
        update_interval=0.02,
        update_when_closed=True,
        updater_cycle_timeout=5.0,
    )
    store = MemoryStore()
    await store.connect()
    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)

    exit_calls: list[int] = []
    monkeypatch.setattr(up, "_exit_fn", lambda code: exit_calls.append(code))

    async def fast_update():
        return 0

    monkeypatch.setattr(up, "_update_once", fast_update)

    up.start()
    await asyncio.sleep(0.3)  # bircok saglikli tur + watchdog kontrolu gecsin
    await up.stop()
    assert exit_calls == []
