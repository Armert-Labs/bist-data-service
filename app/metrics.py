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

FETCH_REQUESTS = Counter("bist_fetch_requests_total", "Kaynak cekim istekleri", ["provider"])
FETCH_ERRORS = Counter("bist_fetch_errors_total", "Kaynak cekim hatalari", ["provider"])
PROVIDER_UP = Gauge("bist_provider_up", "Kaynak saglikli mi (1/0)", ["provider"])

SANITY_REJECTS = Counter("bist_sanity_rejects_total", "Sanity-check ile elenen absurt fiyatlar")

SSE_CLIENTS = Gauge("bist_sse_clients", "Aktif SSE baglanti sayisi")
WEBHOOK_DELIVERIES = Counter("bist_webhook_deliveries_total", "Webhook gonderimleri", ["status"])

CROSS_SOURCE_DRIFT = Gauge("bist_cross_source_drift_pct", "Son dogrulamada kaynaklar arasi maks fiyat sapmasi (%)")
VALIDATION_CONSISTENT = Gauge("bist_validation_consistent", "Son dogrulama tutarli mi (1/0)")
