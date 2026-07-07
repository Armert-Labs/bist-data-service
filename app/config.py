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


# BIST tam gun kapali resmi tatiller. 2026 dini bayramlar ilan edilmis takvime
# gore; 2027 icin yalnizca sabit ulusal gunler (dini bayramlari ilan edilince
# MARKET_HOLIDAYS env ile tam liste verin).
_DEFAULT_MARKET_HOLIDAYS = [
    "2026-01-01",
    "2026-03-20",  # Ramazan Bayrami 1. gun
    "2026-04-23",
    "2026-05-01",
    "2026-05-19",
    "2026-05-27",  # Kurban Bayrami 1-3. gun
    "2026-05-28",
    "2026-05-29",
    "2026-07-15",
    "2026-08-30",
    "2026-10-29",
    "2027-01-01",
    "2027-04-23",
    "2027-05-01",
    "2027-05-19",
    "2027-07-15",
    "2027-08-30",
    "2027-10-29",
]


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
    # gapfill: her kaynak bir oncekinin eksiklerini tamamlar (kesintisizlik, onerilen).
    # hybrid: failover + eksikler icin gapfill devam eder.
    provider_mode: str = field(default_factory=lambda: _get_str("PROVIDER_MODE", "gapfill"))
    # Provider yanit kapsam esigi (%). Altinda kalan yanit basarisiz sayilir.
    provider_min_coverage_pct: float = field(
        default_factory=lambda: _get_float("PROVIDER_MIN_COVERAGE_PCT", 95.0)
    )
    # Sembol bazli devre kesici
    symbol_circuit_fail_threshold: int = field(
        default_factory=lambda: _get_int("SYMBOL_CIRCUIT_FAIL_THRESHOLD", 3)
    )
    symbol_circuit_reset_seconds: float = field(
        default_factory=lambda: _get_float("SYMBOL_CIRCUIT_RESET_SECONDS", 300.0)
    )
    # Yazma aninda capraz-kaynak dogrulama (on-demand icin varsayilan acik).
    write_cross_validate: bool = field(
        default_factory=lambda: _get_bool("WRITE_CROSS_VALIDATE", True)
    )
    write_cross_validate_on_demand: bool = field(
        default_factory=lambda: _get_bool("WRITE_CROSS_VALIDATE_ON_DEMAND", True)
    )
    cross_validate_max_pct: float = field(
        default_factory=lambda: _get_float("CROSS_VALIDATE_MAX_PCT", 1.0)
    )
    # Drift monitörü (updater arka plan kontrolu)
    drift_monitor_enabled: bool = field(
        default_factory=lambda: _get_bool("DRIFT_MONITOR_ENABLED", True)
    )
    drift_monitor_every_n_cycles: int = field(
        default_factory=lambda: _get_int("DRIFT_MONITOR_EVERY_N_CYCLES", 5)
    )
    drift_monitor_symbols: list[str] = field(
        default_factory=lambda: _get_list(
            "DRIFT_MONITOR_SYMBOLS",
            [
                "THYAO",
                "GARAN",
                "AKBNK",
                "ASELS",
                "SISE",
                "KCHOL",
                "TUPRS",
                "BIMAS",
                "EREGL",
                "FROTO",
            ],
        )
    )
    # /history onbellek TTL (sn)
    history_cache_ttl: float = field(default_factory=lambda: _get_float("HISTORY_CACHE_TTL", 600.0))
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
    sanity_max_change_percent: float = field(
        default_factory=lambda: _get_float("SANITY_MAX_CHANGE_PCT", 60.0)
    )
    # Ayni sembol bu sureden uzun kesintisiz sanity reddi yerse yeni fiyat kabul
    # edilir. Bedelsiz/split sonrasi "eski fiyata gore hep absurt" kilitlenmesini
    # kirar (onceki fiyat yalnizca kabul edilen quote ile guncellenir). 0 = kapali.
    sanity_reject_escape_seconds: float = field(
        default_factory=lambda: _get_float("SANITY_REJECT_ESCAPE_SECONDS", 900.0)
    )

    # --- Bayatlik (staleness) ---
    # MARKET ACIKKEN onbellek bu sureden uzun guncellenmezse /ready fail eder ve
    # is_stale=true olur. Market kapaliyken veri degisemeyecegi icin bayatlamaz.
    staleness_seconds: float = field(default_factory=lambda: _get_float("STALENESS_SECONDS", 300.0))
    # Taze sembol orani bu yuzdenin altina duserse bayat sayilir. En-eski-sembol
    # yerine kapsama bakilir: tek guncellenemeyen sembol (askidaki hisse,
    # watchlist-disi tek sorgu) tum servisi kalici NOT READY yapamasin.
    staleness_min_fresh_pct: float = field(
        default_factory=lambda: _get_float("STALENESS_MIN_FRESH_PCT", 90.0)
    )
    # Bir guncelleme turunun toplam zaman butcesi (sn). Provider timeout
    # zincirinin turu staleness esiginin uzerine tasimasini engeller. 0 = kapali.
    updater_cycle_timeout: float = field(
        default_factory=lambda: _get_float("UPDATER_CYCLE_TIMEOUT", 240.0)
    )

    # Bulunamayan (kaynaklarda olmayan) semboller icin negatif onbellek TTL'i (sn).
    # Ayni gecersiz sembole tekrarli isteklerin upstream'i dovmesini onler.
    negative_cache_ttl: float = field(
        default_factory=lambda: _get_float("NEGATIVE_CACHE_TTL", 60.0)
    )

    # --- Redis (bos ise in-memory store kullanilir) ---
    redis_url: str = field(default_factory=lambda: _get_str("REDIS_URL", ""))
    redis_prefix: str = field(default_factory=lambda: _get_str("REDIS_PREFIX", "bist"))

    # --- Guvenlik / kimlik dogrulama ---
    api_key: str = field(default_factory=lambda: _get_str("API_KEY"))  # geriye uyum (tekil)
    api_keys: list[str] = field(
        default_factory=lambda: _get_list("API_KEYS", [])
    )  # "key:label,..."
    api_keys_sha256: list[str] = field(default_factory=lambda: _get_list("API_KEYS_SHA256", []))
    # true ise ve hic anahtar tanimli degilse veri uclari 503 doner (fail-safe:
    # yanlislikla auth'suz acik kalmayi onler). Gelistirme icin AUTH_REQUIRED=false.
    auth_required: bool = field(default_factory=lambda: _get_bool("AUTH_REQUIRED", True))
    # Uretim modu: auth + anahtar yoksa servis baslamaz (fail-fast).
    production_mode: bool = field(default_factory=lambda: _get_bool("PRODUCTION_MODE", False))
    # /demo canli test sayfasi (uretimde kapali tutun).
    demo_enabled: bool = field(default_factory=lambda: _get_bool("DEMO_ENABLED", False))
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
    webhooks_config_path: str = field(
        default_factory=lambda: _get_str("WEBHOOKS_CONFIG", "webhooks.json")
    )
    webhook_timeout: float = field(default_factory=lambda: _get_float("WEBHOOK_TIMEOUT", 5.0))
    webhook_max_retries: int = field(default_factory=lambda: _get_int("WEBHOOK_MAX_RETRIES", 3))
    # Bos ise yalnizca https zorunludur; dolu ise hostname allowlist (virgulle).
    webhook_url_allowlist: list[str] = field(
        default_factory=lambda: _get_list("WEBHOOK_URL_ALLOWLIST", [])
    )

    # --- Persistence (intraday snapshot) ---
    persistence_enabled: bool = field(
        default_factory=lambda: _get_bool("PERSISTENCE_ENABLED", True)
    )
    persistence_max_points: int = field(
        default_factory=lambda: _get_int("PERSISTENCE_MAX_POINTS", 500)
    )

    # --- Loglama ---
    log_level: str = field(default_factory=lambda: _get_str("LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _get_bool("LOG_JSON", True))

    # --- BIST piyasa saatleri (Europe/Istanbul, kalici UTC+3) ---
    # Resmi tatiller: virgulle ayrilmis ISO tarihler. Env verilirse LISTEYI TAMAMEN
    # degistirir (varsayilana eklenmez); "none" varsayilanlari da temizler.
    # Yarim gun (arife) seanslari modellenmez.
    market_holidays: list[str] = field(
        default_factory=lambda: (
            []
            if os.environ.get("MARKET_HOLIDAYS", "").strip().lower() == "none"
            else _get_list("MARKET_HOLIDAYS", _DEFAULT_MARKET_HOLIDAYS)
        )
    )
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


def validate_production(cfg: Settings | None = None) -> None:
    """PRODUCTION_MODE acikken guvensiz yapilandirmada baslatmayi reddeder.

    Hem API (main.lifespan) hem updater (updater_main) girisinde cagrilir:
    dev override/.env sizintisi tek bayrakla iki sureci de durdurabilsin.
    """
    import logging

    cfg = cfg or settings
    if not cfg.production_mode:
        return
    if not cfg.auth_required:
        raise RuntimeError(
            "PRODUCTION_MODE=true ancak AUTH_REQUIRED=false. Uretimde kimlik "
            "dogrulama kapatilamaz; dev override/.env sizintisini kontrol edin."
        )
    log = logging.getLogger(__name__)
    if cfg.demo_enabled:
        log.warning("PRODUCTION_MODE altinda DEMO_ENABLED=true — /demo herkese acik.")
    if cfg.metrics_public:
        log.warning("PRODUCTION_MODE altinda METRICS_PUBLIC=true — /metrics auth'suz.")
