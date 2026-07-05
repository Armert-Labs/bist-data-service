"""Yahoo (yfinance) provider testleri — yf.download monkeypatch ile agsiz."""

import pandas as pd
from app.providers import yahoo
from app.providers.yahoo import _safe_float, _safe_int


def test_safe_float():
    assert _safe_float(None) is None
    assert _safe_float("") is None
    assert _safe_float("3.14159", 2) == 3.14
    assert _safe_float(float("nan")) is None


def test_safe_int():
    assert _safe_int(None) is None
    assert _safe_int(1000.0) == 1000
    assert _safe_int("42") == 42


def test_fetch_quotes_parses_close_and_prev(monkeypatch):
    df = pd.DataFrame(
        {
            "Open": [330.0, 335.0],
            "High": [336.0, 338.0],
            "Low": [329.0, 333.0],
            "Close": [333.25, 334.0],
            "Volume": [1000, 44008702],
        }
    )
    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: df)
    quotes = yahoo.fetch_quotes(["THYAO"])
    q = quotes["THYAO"]
    assert q.price == 334.0
    assert q.previous_close == 333.25
    assert q.change == 0.75
    assert q.volume == 44008702


def test_fetch_quotes_empty_frame(monkeypatch):
    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: pd.DataFrame())
    assert yahoo.fetch_quotes(["THYAO"]) == {}


def test_fetch_quotes_invalid_symbol_filtered():
    # Gecersiz sembol hic Yahoo'ya gitmeden elenir.
    assert yahoo.fetch_quotes(["!!!"]) == {}
