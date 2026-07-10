"""Yahoo (yfinance) provider testleri — yf.download monkeypatch ile agsiz."""

import threading

import pandas as pd
from app.providers import yahoo
from app.providers.yahoo import YahooProvider, _safe_float, _safe_int


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


def test_fetch_quotes_passes_threads_false(monkeypatch):
    # yf.download'in KENDI ic multitasking thread havuzu kapatilmali: asilan bir
    # crumb istegi ticker basina ayri thread yerine TEK cagriyi bloke eder.
    captured = {}

    def fake_download(*args, **kwargs):
        captured.update(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(yahoo.yf, "download", fake_download)
    yahoo.fetch_quotes(["THYAO"])
    assert captured["threads"] is False


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_fetch_quotes_closes_session_after_success(monkeypatch):
    fake_session = _FakeSession()
    monkeypatch.setattr(yahoo, "_new_timeout_session", lambda: fake_session)
    captured = {}

    def fake_download(*args, **kwargs):
        captured.update(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(yahoo.yf, "download", fake_download)
    yahoo.fetch_quotes(["THYAO"])
    assert captured["session"] is fake_session
    assert fake_session.closed is True


def test_fetch_quotes_closes_session_after_error(monkeypatch):
    fake_session = _FakeSession()
    monkeypatch.setattr(yahoo, "_new_timeout_session", lambda: fake_session)

    def fake_download(*args, **kwargs):
        raise RuntimeError("yahoo coktu")

    monkeypatch.setattr(yahoo.yf, "download", fake_download)
    assert yahoo.fetch_quotes(["THYAO"]) == {}
    assert fake_session.closed is True


async def test_yahoo_provider_runs_in_isolated_executor(monkeypatch):
    # Varsayilan asyncio executor'unu (ve dolayisiyla diger to_thread
    # kullanicilarini) paylasmamali; kendi izole ThreadPoolExecutor'unda
    # calismali.
    seen_thread_names = []

    def fake_fetch_quotes(symbols):
        seen_thread_names.append(threading.current_thread().name)
        return {}

    monkeypatch.setattr(yahoo, "fetch_quotes", fake_fetch_quotes)
    provider = YahooProvider()
    await provider.fetch_quotes(["THYAO"])
    assert seen_thread_names[0].startswith("yahoo-fetch")


def test_isolated_executor_is_bounded():
    assert yahoo._EXECUTOR._max_workers == 2
