"""Config yardimci fonksiyonlari testleri."""

from dataclasses import replace

import pytest
from app.config import _get_bool, _get_float, _get_int, _get_list, settings, validate_production


def test_get_bool(monkeypatch):
    monkeypatch.setenv("X", "true")
    assert _get_bool("X", False) is True
    monkeypatch.setenv("X", "0")
    assert _get_bool("X", True) is False
    monkeypatch.setenv("X", "evet")
    assert _get_bool("X", False) is True
    assert _get_bool("YOK_BOOL", True) is True


def test_get_list(monkeypatch):
    monkeypatch.setenv("L", "a, b ,c")
    assert _get_list("L", []) == ["a", "b", "c"]
    monkeypatch.setenv("L", "")
    assert _get_list("L", ["d"]) == ["d"]
    assert _get_list("YOK_LIST", ["x"]) == ["x"]


def test_get_int(monkeypatch):
    monkeypatch.setenv("N", "42")
    assert _get_int("N", 0) == 42
    monkeypatch.setenv("N", "bozuk")
    assert _get_int("N", 7) == 7


def test_get_float(monkeypatch):
    monkeypatch.setenv("F", "3.14")
    assert _get_float("F", 0.0) == 3.14
    monkeypatch.setenv("F", "x")
    assert _get_float("F", 1.5) == 1.5


def test_validate_production_rejects_auth_disabled():
    cfg = replace(settings, production_mode=True, auth_required=False)
    with pytest.raises(RuntimeError, match="AUTH_REQUIRED"):
        validate_production(cfg)


def test_validate_production_passes_with_auth_on():
    validate_production(replace(settings, production_mode=True, auth_required=True))


def test_validate_production_noop_outside_production():
    validate_production(replace(settings, production_mode=False, auth_required=False))


def test_market_holidays_none_sentinel_clears_defaults(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("MARKET_HOLIDAYS", "none")
    assert Settings().market_holidays == []
    monkeypatch.delenv("MARKET_HOLIDAYS")
    assert "2026-10-29" in Settings().market_holidays


def test_provider_fetch_timeout_default():
    from app.config import Settings

    assert Settings().provider_fetch_timeout == 45.0


def test_provider_fetch_timeout_env_override(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("PROVIDER_FETCH_TIMEOUT", "12.5")
    assert Settings().provider_fetch_timeout == 12.5


def test_cors_origins_default_is_same_origin_only(monkeypatch):
    """Guvenli varsayilan: bos liste (cross-origin tarayici istegi reddedilir).
    Panel/dashboard nginx reverse-proxy ile ayni-origin gittigi icin bu
    varsayilandan etkilenmez (bkz. deploy/panel/default.conf.template)."""
    from app.config import Settings

    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == []


def test_cors_origins_env_override(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("CORS_ORIGINS", "https://ornek.com")
    assert Settings().cors_origins == ["https://ornek.com"]


def test_max_symbols_per_request_default():
    from app.config import Settings

    assert Settings().max_symbols_per_request == 100


def test_max_symbols_per_request_env_override(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("MAX_SYMBOLS_PER_REQUEST", "50")
    assert Settings().max_symbols_per_request == 50


def test_validate_providers_default_includes_tradingview(monkeypatch):
    """HIGH-2 regresyon guard: eski varsayim [yahoo_chart, isyatirim] idi --
    birincil yahoo_chart'tan gelince tek olasi bagimsiz referans isyatirim
    kalirdi; isyatirim seans icinde H2 bayat-bar guard'i yuzunden elenince
    dogrulama TAMAMEN olu kaliyordu. tradingview (artik exchange_time
    sagliyor -- HIGH-1) bu tekli-referans riskini gideriyor."""
    from app.config import Settings

    monkeypatch.delenv("VALIDATE_PROVIDERS", raising=False)
    cfg = Settings()
    assert cfg.validate_providers == ["yahoo_chart", "tradingview", "isyatirim"]


def test_default_providers_excludes_yahoo_from_live_chain(monkeypatch):
    """yahoo (yfinance/curl_cffi crumb wedge riski) varsayilan zincirden cikarildi;
    yahoo_chart onceki yerini alir. Provider sinifi hala PROVIDERS env'i ile geri
    eklenebilir (silinmedi)."""
    from app.config import Settings

    monkeypatch.delenv("PROVIDERS", raising=False)
    cfg = Settings()
    assert cfg.providers == ["yahoo_chart", "tradingview", "isyatirim"]
    assert "yahoo" not in cfg.providers
