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


def test_fetch_quotes_sets_bar_time_from_dataframe_index(monkeypatch):
    # MEDIUM-5: yahoo (yfinance) hic zaman damgasi vermiyordu -- PROVIDERS'a
    # geri eklenirse seans icinde guard tarafindan %100 duserdi (bkz.
    # .env.example). DataFrame index'i (gunluk bar tarihi) zaten mevcut veri;
    # bar_time'a tasinir.
    idx = pd.to_datetime(["2026-07-10", "2026-07-13"])
    df = pd.DataFrame(
        {
            "Open": [330.0, 335.0],
            "High": [336.0, 338.0],
            "Low": [329.0, 333.0],
            "Close": [333.25, 334.0],
            "Volume": [1000, 44008702],
        },
        index=idx,
    )
    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: df)
    quotes = yahoo.fetch_quotes(["THYAO"])
    q = quotes["THYAO"]
    assert q.bar_time is not None
    assert q.bar_time.date().isoformat() == "2026-07-13"
    assert q.exchange_time is None  # bu kaynak gercek islem-ani vermiyor


def test_fetch_quotes_non_datetime_index_leaves_bar_time_none():
    # Beklenmedik/sentetik bir DataFrame (index datetime degil) crash ETMEMELI;
    # bar_time acikca None kalir.
    from app.providers.yahoo import _quote_from_frame

    df = pd.DataFrame({"Close": [10.0, 11.0]})  # varsayilan RangeIndex
    q = _quote_from_frame("THYAO", df)
    assert q is not None
    assert q.bar_time is None


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


def _reset_shared_session(monkeypatch):
    """Testler arasi izolasyon: modul-seviyesi paylasilan session state'ini sifirla."""
    monkeypatch.setattr(yahoo, "_SHARED_SESSION", None)
    monkeypatch.setattr(yahoo, "_SHARED_SESSION_INITIALIZED", False)


def test_fetch_quotes_reuses_shared_session_across_calls(monkeypatch):
    # yfinance 1.5.1 YfData process-genelinde Singleton: her cagride YENI bir
    # session yaratip set etmek "son yazan kazanir" yarisina yol acar (izole
    # executor'da 2 eszamanli fetch birbirinin session'ini calabilir). Bu yuzden
    # TEK, uzun-omurlu, lazy-init session kullanilmali; her fetch AYNI objeyi
    # almali.
    _reset_shared_session(monkeypatch)
    created = []

    def fake_new_session():
        s = _FakeSession()
        created.append(s)
        return s

    monkeypatch.setattr(yahoo, "_new_timeout_session", fake_new_session)
    captured_sessions = []

    def fake_download(*args, **kwargs):
        captured_sessions.append(kwargs.get("session"))
        return pd.DataFrame()

    monkeypatch.setattr(yahoo.yf, "download", fake_download)
    yahoo.fetch_quotes(["THYAO"])
    yahoo.fetch_quotes(["GARAN"])

    assert len(created) == 1  # yalnizca ilk cagride kuruldu
    assert captured_sessions[0] is captured_sessions[1] is created[0]


def test_fetch_quotes_never_closes_shared_session_on_success_or_error(monkeypatch):
    # Paylasilan session process omru boyunca yasar; hicbir fetch onu
    # kapatmamali (eszamanli baska bir fetch'in devam eden istegini kesmesin).
    _reset_shared_session(monkeypatch)
    fake_session = _FakeSession()
    monkeypatch.setattr(yahoo, "_new_timeout_session", lambda: fake_session)

    monkeypatch.setattr(yahoo.yf, "download", lambda *a, **k: pd.DataFrame())
    yahoo.fetch_quotes(["THYAO"])
    assert fake_session.closed is False

    def fake_download_error(*args, **kwargs):
        raise RuntimeError("yahoo coktu")

    monkeypatch.setattr(yahoo.yf, "download", fake_download_error)
    assert yahoo.fetch_quotes(["GARAN"]) == {}
    assert fake_session.closed is False


def test_fetch_history_uses_shared_session_and_never_closes(monkeypatch):
    _reset_shared_session(monkeypatch)
    fake_session = _FakeSession()
    monkeypatch.setattr(yahoo, "_new_timeout_session", lambda: fake_session)
    captured = {}

    class _FakeTicker:
        def __init__(self, *args, **kwargs):
            captured["session"] = kwargs.get("session")

        def history(self, *args, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(yahoo.yf, "Ticker", _FakeTicker)
    yahoo.fetch_history("THYAO")
    assert captured["session"] is fake_session
    assert fake_session.closed is False


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
