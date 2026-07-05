# Deploy & Observability

Bu klasör, üretim gözlemlenebilirliği için örnek yapılandırmalar içerir.

## Prometheus

`prometheus.yml` iki hedefi scrape eder:
- `updater:8001` — iş metrikleri (önbellek boyutu, güncelleme süresi, kaynak sağlığı, sapma)
- `api:8000/metrics` — HTTP metrikleri (`METRICS_PUBLIC=true` ya da token gerekir)

Docker Compose ağına Prometheus eklemek için `docker-compose.yml`'e bir `prometheus`
servisi ekleyip bu dosyayı `/etc/prometheus/prometheus.yml` olarak mount edin.

## Grafana

`grafana-dashboard.json` içe aktarılabilir bir dashboard'dur:
Grafana → Dashboards → **Import** → JSON'u yapıştır → Prometheus veri kaynağını seç.

Paneller: önbellek boyutu, son güncelleme yaşı, kaynaklar arası sapma, aktif SSE,
güncelleme süresi (p50/p95), kaynak sağlığı, HTTP istek hızı, çekim hataları.
