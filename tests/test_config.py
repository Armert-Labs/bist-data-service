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


def test_validate_providers_default_excludes_tradingview(monkeypatch):
    """Patron karari (hukuki, TradingView ToS §3 -- otomatik islem/algoritmik
    karar-verme/fiyat referanslama yasak): tradingview varsayilan
    VALIDATE_PROVIDERS'tan CIKARILDI. BILINEN VE KABUL EDILEN SONUC (HIGH-2'nin
    cozdugu tekli-referans riski GERI DONDU): birincil yahoo_chart'tan gelince
    tek olasi bagimsiz referans isyatirim'dir; o da seans icinde H2 bayat-bar
    guard'i yuzunden elenirse dogrulama fail-quiet doner (bkz.
    test_pipeline.py::test_cross_validate_prod_default_fails_quiet_without_tradingview).
    Provider sinifi silinmedi, env ile geri eklenebilir (yalnizca insan-okur
    dashboard/teshis amaciyla -- bkz. providers/tradingview.py ust uyarisi)."""
    from app.config import Settings

    monkeypatch.delenv("VALIDATE_PROVIDERS", raising=False)
    cfg = Settings()
    assert cfg.validate_providers == ["yahoo_chart", "isyatirim"]
    assert "tradingview" not in cfg.validate_providers


def test_validate_providers_env_can_still_reenable_tradingview(monkeypatch):
    """Provider sinifi silinmedi -- env ile bilinçli olarak geri eklenebilir
    (yalnizca insan-okur dashboard/teshis amaciyla, bot karar yoluna
    baglanmamali; bkz. providers/tradingview.py ust uyarisi)."""
    from app.config import Settings

    monkeypatch.setenv("VALIDATE_PROVIDERS", "yahoo_chart,tradingview,isyatirim")
    cfg = Settings()
    assert cfg.validate_providers == ["yahoo_chart", "tradingview", "isyatirim"]


def test_default_providers_excludes_yahoo_and_tradingview_from_live_chain(monkeypatch):
    """yahoo (yfinance/curl_cffi crumb wedge riski) varsayilan zincirden
    cikarildi; yahoo_chart onceki yerini alir. tradingview de PATRON KARARIYLA
    (hukuki, ToS §3) varsayilan zincirden cikarildi. Ikisinin provider sinifi
    da hala PROVIDERS env'i ile geri eklenebilir (silinmedi)."""
    from app.config import Settings

    monkeypatch.delenv("PROVIDERS", raising=False)
    cfg = Settings()
    assert cfg.providers == ["yahoo_chart", "isyatirim"]
    assert "yahoo" not in cfg.providers
    assert "tradingview" not in cfg.providers


def test_providers_env_can_still_reenable_tradingview(monkeypatch):
    """Provider sinifi silinmedi -- env ile bilinçli olarak geri eklenebilir
    (yalnizca insan-okur dashboard/teshis amaciyla; bkz. providers/tradingview.py
    ust uyarisi)."""
    from app.config import Settings

    monkeypatch.setenv("PROVIDERS", "yahoo_chart,tradingview,isyatirim")
    cfg = Settings()
    assert cfg.providers == ["yahoo_chart", "tradingview", "isyatirim"]


# --------------------------------------------------------------------------- #
# HIGH-1 regresyonu (PR#19 review): `/` iceren Redis parolasi REDIS_URL'i
# kirmamali. `openssl rand -base64 32` uretimi parolalarin ~%50'si `/` (bazen
# `+`,`=`) icerir; docker-compose.yml bunu eskiden dogrudan URL'e gomuyordu --
# urlparse/redis.from_url "ValueError: Port could not be cast" ile patliyor,
# redis sunucusu saglikli kalirken (REDISCLI_AUTH kullanir) api/updater/bot
# crash-loop'a giriyordu.
# --------------------------------------------------------------------------- #
def test_build_redis_url_percent_encodes_slash_password():
    from app.config import _build_redis_url
    from redis.connection import parse_url

    password = "ab/cd+ef=gh"  # / + = iceren tipik base64 parola
    url = _build_redis_url("redis://redis:6379/0", password)

    parsed = parse_url(url)  # regresyon: eskiden ValueError atardi
    assert parsed["password"] == password
    assert parsed["host"] == "redis"
    assert parsed["port"] == 6379


def test_build_redis_url_noop_when_password_empty():
    from app.config import _build_redis_url

    assert _build_redis_url("redis://redis:6379/0", "") == "redis://redis:6379/0"


def test_build_redis_url_noop_when_url_empty():
    from app.config import _build_redis_url

    assert _build_redis_url("", "parola") == ""


def test_build_redis_url_does_not_override_existing_credentials():
    """REDIS_URL elle (compose-disi, orn. harici yonetilen Redis) kimlik
    bilgisiyle verilmisse dokunulmaz -- REDIS_PASSWORD yalniz compose'un
    kendi ayirdigi (kimlik-bilgisiz REDIS_URL) senaryosunda enjekte edilir."""
    from app.config import _build_redis_url

    url = "redis://:existing-pw@redis:6380/0"
    assert _build_redis_url(url, "OVERRIDE") == url


def test_validate_redis_url_raises_for_unencoded_slash_password():
    from app.config import _validate_redis_url

    with pytest.raises(RuntimeError, match="REDIS_URL"):
        _validate_redis_url("redis://:ab/cd+ef=@redis:6379/0")


def test_validate_redis_url_noop_for_valid_or_empty_url():
    from app.config import _validate_redis_url

    _validate_redis_url("redis://:ab%2Fcd%2Bef%3D@redis:6379/0")  # raise etmemeli
    _validate_redis_url("")  # redis kapali -- raise etmemeli


def test_settings_builds_safe_redis_url_from_separate_password_env(monkeypatch):
    """Compose'un gercek uretim senaryosu: REDIS_URL kimlik-bilgisiz, parola
    ayri REDIS_PASSWORD env'inden gelir. `/`+`=` iceren bir parolayla bile
    Settings() FAIL-FAST'e DUSMEMELI -- redis_url otomatik guvenli olur ve
    ham parola kayipsiz geri kazanilabilir olmali."""
    from app.config import Settings
    from redis.connection import parse_url

    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "ab/cd+ef=gh")
    cfg = Settings()
    parsed = parse_url(cfg.redis_url)
    assert parsed["password"] == "ab/cd+ef=gh"
    assert parsed["host"] == "redis"


def test_settings_fails_fast_for_manually_broken_redis_url(monkeypatch):
    """Savunma-katmani (HIGH-1): REDIS_URL elle (compose-disi) kimlik-bilgili
    VE URL-ozel karakter iceren bozuk bir parolayla verilirse Settings()
    ACILISTA net bir RuntimeError ile durmali -- crash-loop + belirsiz
    'ValueError: Port could not be cast' yerine actionable hata."""
    from app.config import Settings

    monkeypatch.setenv("REDIS_URL", "redis://:ab/cd+ef=@redis:6379/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        Settings()
