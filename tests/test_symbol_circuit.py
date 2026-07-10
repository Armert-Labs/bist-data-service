"""SymbolCircuitRegistry testleri (denetim: sifir test vardi)."""

import time

from app.symbol_circuit import SymbolCircuitRegistry


def test_opens_after_threshold_and_blocks():
    reg = SymbolCircuitRegistry(fail_threshold=2, reset_timeout=60.0)
    assert reg.allow("yahoo", "THYAO") is True
    reg.record_failure("yahoo", "THYAO")
    assert reg.allow("yahoo", "THYAO") is True  # esik altinda hala izinli
    reg.record_failure("yahoo", "THYAO")
    assert reg.allow("yahoo", "THYAO") is False  # devre acildi
    assert reg.allow("yahoo", "GARAN") is True  # diger sembol etkilenmez
    assert reg.allow("isyatirim", "THYAO") is True  # provider bazli ayrim


def test_reset_timeout_recovers(monkeypatch):
    import app.symbol_circuit as sc_mod

    reg = SymbolCircuitRegistry(fail_threshold=1, reset_timeout=100.0)
    reg.record_failure("yahoo", "THYAO")
    assert reg.allow("yahoo", "THYAO") is False

    real = time.monotonic()

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return real + 101.0

    monkeypatch.setattr(sc_mod, "time", FakeTime)
    assert reg.allow("yahoo", "THYAO") is True  # timeout sonrasi tekrar izin
    assert reg.allow("yahoo", "THYAO") is True  # sayac sifirlandi


def test_success_resets_counter():
    reg = SymbolCircuitRegistry(fail_threshold=2, reset_timeout=60.0)
    reg.record_failure("yahoo", "THYAO")
    reg.record_success("yahoo", "THYAO")
    reg.record_failure("yahoo", "THYAO")
    assert reg.allow("yahoo", "THYAO") is True  # basari sayaci sifirladi


def test_never_repeated_symbols_do_not_grow_without_bound():
    # /quote/{symbol} ve /quotes?symbols= gecerli BICIM disinda watchlist
    # dogrulamasi yapmaz; tek seferlik (typo/bot) sembol sorgulari _failures'i
    # sonsuza kadar biriktirmemeli.
    reg = SymbolCircuitRegistry(fail_threshold=100, reset_timeout=60.0)
    for i in range(reg._MAX_ENTRIES + 500):
        reg.record_failure("yahoo", f"FAKE{i}")
    assert len(reg._failures) <= reg._MAX_ENTRIES


def test_open_circuit_survives_garbage_eviction():
    # Gercek (watchlist) sembolun ACIK devresi, budama sirasinda tek-seferlik
    # kapali cop kayitlarla birlikte silinmemeli.
    reg = SymbolCircuitRegistry(fail_threshold=3, reset_timeout=9999.0)
    for _ in range(3):
        reg.record_failure("yahoo", "THYAO")
    assert reg.allow("yahoo", "THYAO") is False

    for i in range(reg._MAX_ENTRIES + 500):
        reg.record_failure("yahoo", f"FAKE{i}")  # her biri tek hata, esige ulasmaz

    assert reg.allow("yahoo", "THYAO") is False  # acik devre budanmadi
    assert len(reg._failures) <= reg._MAX_ENTRIES
