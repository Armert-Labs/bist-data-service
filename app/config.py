"""Uygulama ayarlari (12-factor: ortam degiskenlerinden okunur).

Ek bagimlilik (pydantic-settings) gerektirmemesi icin sade os.environ kullanilir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "evet"}


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _get_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # --- Guncelleme / cekim ---
    update_interval: float = field(default_factory=lambda: _get_float("UPDATE_INTERVAL", 60.0))
    batch_size: int = field(default_factory=lambda: _get_int("BATCH_SIZE", 40))
    batch_pause: float = field(default_factory=lambda: _get_float("BATCH_PAUSE", 1.0))
    update_when_closed: bool = field(default_factory=lambda: _get_bool("UPDATE_WHEN_CLOSED", False))
    max_concurrent_fetch: int = field(default_factory=lambda: _get_int("MAX_CONCURRENT_FETCH", 8))

    # --- Kaynak / dogrulama ---
    # Oncelik sirasi. yahoo=yfinance(batch), yahoo_chart=v8 chart(dayanikli),
    # isyatirim=Turkiye (yurtdisi IP'lerden erisilemeyebilir).
    providers: list[str] = field(
        default_factory=lambda: _get_list("PROVIDERS", ["yahoo", "yahoo_chart", "isyatirim"])
    )
    # failover: ilk veri donduren kaynak yeter (verimli).
    # gapfill: her kaynak bir oncekinin eksiklerini tamamlar (kesintisizlik, daha yavas).
    provider_mode: str = field(default_factory=lambda: _get_str("PROVIDER_MODE", "failover"))
    # /validate icin bagimsiz referans kaynaklar (birincilden farkli olmali).
    # Erisilemeyen kaynaklar raporda "erisilemedi" olarak isaretlenir.
    validate_providers: list[str] = field(
        default_factory=lambda: _get_list("VALIDATE_PROVIDERS", ["yahoo_chart", "isyatirim"])
    )

    # --- Is Yatirim erisim ayarlari ---
    # TR disi IP'lerden Is Yatirim'a erisim engellenebilir. Bir TR cikisli proxy
    # verilirse Is Yatirim istekleri oradan gecer (Yahoo dogrudan kalir).
    isyatirim_proxy: str = field(default_factory=lambda: _get_str("ISYATIRIM_PROXY", ""))
    isyatirim_timeout: float = field(default_factory=lambda: _get_float("ISYATIRIM_TIMEOUT", 10.0))
    isyatirim_retries: int = field(default_factory=lambda: _get_int("ISYATIRIM_RETRIES", 2))
    isyatirim_concurrency: int = field(default_factory=lambda: _get_int("ISYATIRIM_CONCURRENCY", 5))
    # Bir onceki fiyata gore kabul edilebilir maksimum mutlak degisim (%). Absurt
    # degerleri (veri bozulmasi) elemek icin. BIST tavan/taban +-%10; gap paylari
    # icin genis tutuyoruz.
    sanity_max_change_percent: float = field(default_factory=lambda: _get_float("SANITY_MAX_CHANGE_PCT", 60.0))

    # --- Bayatlik (staleness) ---
    # Onbellek bu sureden uzun sure guncellenmezse /ready fail eder ve is_stale=true.
    staleness_seconds: float = field(default_factory=lambda: _get_float("STALENESS_SECONDS", 300.0))

    # --- Redis (bos ise in-memory store kullanilir) ---
    redis_url: str = field(default_factory=lambda: _get_str("REDIS_URL", ""))
    redis_prefix: str = field(default_factory=lambda: _get_str("REDIS_PREFIX", "bist"))

    # --- Guvenlik / kimlik dogrulama ---
    api_key: str = field(default_factory=lambda: _get_str("API_KEY"))  # geriye uyum (tekil)
    api_keys: list[str] = field(default_factory=lambda: _get_list("API_KEYS", []))  # "key:label,..."
    api_keys_sha256: list[str] = field(default_factory=lambda: _get_list("API_KEYS_SHA256", []))
    # true ise ve hic anahtar tanimli degilse veri uclari 503 doner (fail-safe:
    # yanlislikla auth'suz acik kalmayi onler).
    auth_required: bool = field(default_factory=lambda: _get_bool("AUTH_REQUIRED", False))
    # /metrics herkese acik mi. Guvenli varsayilan: false (auth ister).
    metrics_public: bool = field(default_factory=lambda: _get_bool("METRICS_PUBLIC", False))
    cors_origins: list[str] = field(default_factory=lambda: _get_list("CORS_ORIGINS", ["*"]))
    rate_limit: str = field(default_factory=lambda: _get_str("RATE_LIMIT", "120/minute"))
    rate_limit_enabled: bool = field(default_factory=lambda: _get_bool("RATE_LIMIT_ENABLED", True))

    # --- SSE ---
    stream_interval: float = field(default_factory=lambda: _get_float("STREAM_INTERVAL", 5.0))
    max_sse_clients: int = field(default_factory=lambda: _get_int("MAX_SSE_CLIENTS", 200))

    # --- Performans ---
    # /all yanitini kisa sure onbellekler (yuksek trafikte tekrar serialize maliyetini keser).
    all_cache_ttl: float = field(default_factory=lambda: _get_float("ALL_CACHE_TTL", 3.0))

    # --- Webhook (olay bazli alarmlar) ---
    webhooks_enabled: bool = field(default_factory=lambda: _get_bool("WEBHOOKS_ENABLED", False))
    webhooks_config_path: str = field(default_factory=lambda: _get_str("WEBHOOKS_CONFIG", "webhooks.json"))
    webhook_timeout: float = field(default_factory=lambda: _get_float("WEBHOOK_TIMEOUT", 5.0))
    webhook_max_retries: int = field(default_factory=lambda: _get_int("WEBHOOK_MAX_RETRIES", 3))

    # --- Persistence (intraday snapshot) ---
    persistence_enabled: bool = field(default_factory=lambda: _get_bool("PERSISTENCE_ENABLED", True))
    persistence_max_points: int = field(default_factory=lambda: _get_int("PERSISTENCE_MAX_POINTS", 500))

    # --- Loglama ---
    log_level: str = field(default_factory=lambda: _get_str("LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _get_bool("LOG_JSON", True))

    # --- BIST piyasa saatleri (Europe/Istanbul, kalici UTC+3) ---
    market_tz_offset_hours: int = 3
    market_open_hour: int = 10
    market_open_minute: int = 0
    market_close_hour: int = 18
    market_close_minute: int = 15

    @property
    def api_key_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)


settings = Settings()
