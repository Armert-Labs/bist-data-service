"""Prometheus metrikleri. /metrics ucundan (instrumentator) yayilir."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

QUOTES_CACHED = Gauge("bist_quotes_cached", "Onbellekteki sembol sayisi")
LAST_UPDATE_AGE = Gauge("bist_last_update_age_seconds", "Son guncellemeden bu yana gecen sure (sn)")
UPDATE_DURATION = Histogram(
    "bist_update_duration_seconds",
    "Bir tam guncelleme turunun suresi",
    buckets=(1, 5, 10, 20, 30, 45, 60, 90, 120),
)
UPDATE_SYMBOLS = Gauge("bist_update_symbols_last", "Son turda basariyla guncellenen sembol sayisi")
WATCHLIST_SIZE = Gauge("bist_watchlist_size", "Takip listesindeki (dinamik evren) sembol sayisi")

FETCH_REQUESTS = Counter("bist_fetch_requests_total", "Kaynak cekim istekleri", ["provider"])
FETCH_ERRORS = Counter("bist_fetch_errors_total", "Kaynak cekim hatalari", ["provider"])
PROVIDER_UP = Gauge("bist_provider_up", "Kaynak saglikli mi (1/0)", ["provider"])

SANITY_REJECTS = Counter("bist_sanity_rejects_total", "Sanity-check ile elenen absurt fiyatlar")
SANITY_ESCAPES = Counter(
    "bist_sanity_escapes_total",
    "Kacis mekanizmasi ile kabul edilen sanity redleri (reason: persistence="
    "ayni kaynagin N ardisik turda israrla ayni fiyati tekrarlamasi (tek "
    "kaynakli dunyada TEK canli yol), corroboration=coklu bagimsiz kaynak "
    "uzlasisi (Faz-2, bugun tetiklenemez); eski 'consistency' yolu (kaynagin "
    "previous_close'u ile ic-tutarlilik) HIGH-2 (review-4) ile GUVENLIK "
    "nedeniyle KALDIRILDI -- yanlis-birim/yanlis-enstruman gibi self-"
    "consistent ama hatali payload'lari teyitsiz kabul edebiliyordu)",
    ["provider", "reason"],
)
WRITE_VALIDATE_REJECTS = Counter(
    "bist_write_validate_rejects_total", "Capraz-kaynak yazma dogrulamasinda elenen fiyatlar"
)
FETCH_PARTIAL = Counter(
    "bist_fetch_partial_total", "Kapsam esiginin altinda kalan provider yanitlari", ["provider"]
)
DRIFT_ALERTS = Counter("bist_drift_alerts_total", "Drift monitörü esik asimi uyarilari")
UPDATE_CYCLE_TIMEOUTS = Counter(
    "bist_update_cycle_timeouts_total", "Zaman butcesini asip iptal edilen guncelleme turlari"
)
OLDEST_QUOTE_AGE = Gauge("bist_oldest_quote_age_seconds", "En eski sembol guncelleme yasi (sn)")

SSE_CLIENTS = Gauge("bist_sse_clients", "Aktif SSE baglanti sayisi")
SSE_DROPPED_EVENTS = Counter(
    "bist_sse_dropped_events_total", "Yavas abone kuyrugu dolunca dusen pub/sub olaylari"
)
WEBHOOK_DELIVERIES = Counter("bist_webhook_deliveries_total", "Webhook gonderimleri", ["status"])

CROSS_SOURCE_DRIFT = Gauge(
    "bist_cross_source_drift_pct", "Son dogrulamada kaynaklar arasi maks fiyat sapmasi (%)"
)
VALIDATION_CONSISTENT = Gauge("bist_validation_consistent", "Son dogrulama tutarli mi (1/0)")

STALE_BAR_SKIPPED = Counter(
    "bist_stale_bar_skipped_total",
    "Seans acikken bayat bar (dunku/eski veri noktasi) nedeniyle atlanan quote sayisi",
    ["provider"],
)
QUOTES_BY_SOURCE = Gauge(
    "bist_quotes_by_source", "Son guncelleme turunde kaynak basina yazilan quote sayisi", ["source"]
)
MISSING_EXCHANGE_TIME = Counter(
    "bist_missing_exchange_time_total",
    "Seans acikken hic zaman damgasi (bar_time/exchange_time) uretemeyen kaynak nedeniyle atlanan quote sayisi",
    ["provider"],
)
VALIDATE_NO_REFERENCE = Counter(
    "bist_validate_no_reference_total",
    "Bagimsiz referans bulunamadigi (kendi kaynagi/bayat/damgasiz/veri yok) icin dogrulanamayan karsilastirma",
    ["reason"],
)
PROVIDER_GUARD_COOLDOWN = Gauge(
    "bist_provider_guard_cooldown",
    "Kaynak, tekrarlanan tam guard-dusmesi nedeniyle gecici cooldown'da mi (1/0)",
    ["provider"],
)
GUARD_FAIL_OPEN = Counter(
    "bist_guard_fail_open_total",
    "TUM kaynaklar TUM sembolleri guard'la dusurdugu (sistemik ariza -- tatil "
    "listesi/piyasa-acik varsayimi hatali olabilir) icin guard'in bir tur "
    "boyunca gecici olarak devre disi birakildigi (fail-open) sayisi",
)
