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
