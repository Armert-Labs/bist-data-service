"""BackgroundUpdater._update_once testleri (mock aggregator + in-memory store)."""

import asyncio

import app.symbols as sym_mod
import app.updater as updater_mod
from app.models import Quote
from app.store import MemoryStore
from app.updater import BackgroundUpdater


async def test_update_once_writes_to_store(monkeypatch):
    store = MemoryStore()
    await store.connect()

    async def fake_fetch(symbols, previous=None):
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


async def test_update_once_passes_previous_for_sanity(monkeypatch):
    store = MemoryStore()
    await store.connect()
    await store.set_quote("THYAO", Quote(symbol="THYAO", price=100.0))

    seen_previous = {}

    async def fake_fetch(symbols, previous=None):
        seen_previous.update(previous or {})
        return {}

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(updater_mod.aggregator, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(updater_mod.asyncio, "sleep", no_sleep)

    up = BackgroundUpdater(symbols_list=["THYAO"], store=store)
    await up._update_once()
    assert seen_previous.get("THYAO") == 100.0


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
