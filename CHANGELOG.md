# Değişiklik Günlüğü

Bu projenin tüm önemli değişiklikleri bu dosyada belgelenir.

Biçim [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) temellidir ve
proje [Semantic Versioning](https://semver.org/lang/tr/) kullanır.

## [Yayınlanmamış]

### Eklendi
- Modern paketleme (`pyproject.toml`), ruff + mypy + pre-commit
- MIT lisansı, SECURITY / CONTRIBUTING / CODE_OF_CONDUCT
- GitHub şablonları (issue/PR), Dependabot, CODEOWNERS
- Genişletilmiş CI (lint + tip kontrolü + kapsam + matris + Docker imajı)
- `Makefile`, Prometheus + Grafana örnek yapılandırmaları
- `PROVIDER_FETCH_TIMEOUT` (varsayılan 45sn) — tek `provider.fetch_quotes()` çağrısı için sert üst sınır
- `MAX_SYMBOLS_PER_REQUEST` (varsayılan 100) — `/quotes` ve `/validate` için tek istekte sembol sayısı üst sınırı; aşımda 400
- `docker-compose.yml`'e `cadvisor` servisi — VM140 Prometheus'un container bazlı bellek/CPU/OOM metriklerini scrape edebilmesi için (host port `8081:8080`; `panel` zaten `8080:80` kullandığından port çakışmasını önlemek için `8081` seçildi — bkz. Güvenlik notu)

### Güvenlik
- CI (`ci.yml`): üst seviye `permissions: contents: read` eklendi (`release.yml`'deki desenle tutarlı, en az yetki ilkesi)
- `CORS_ORIGINS` varsayılanı `*` → boş (same-origin only) değiştirildi. Panel/dashboard nginx reverse-proxy ile aynı-origin gittiği için (bkz. `deploy/panel/default.conf.template`) bu değişiklik canlı paneli etkilemez; cross-origin bir tarayıcı istemciniz varsa `CORS_ORIGINS` ile açıkça izin verin
- **cAdvisor port notu (operasyonel, dikkat):** VM130'da host port `8080` zaten `panel` servisi tarafından kullanıldığından (`8080:80`, canlı public dashboard) cAdvisor `8081:8080` ile eşlendi. Daha önce ayrı bir işte VM140 Prometheus scrape hedefi `10.10.10.130:8080` olarak yapılandırılmıştı — bu hedefin `10.10.10.130:8081` olarak güncellenmesi gerekiyor, aksi halde cAdvisor scrape edilemez.

### Düzeltildi
- **Canlı donma (hang) fix'i:** `yfinance`'in Yahoo crumb/cookie auth isteği (curl_cffi) bazı
  koşullarda süresiz asılıp güncelleme turunu tıkıyordu (yalnız süreç restart'ı açıyordu).
  Üç katmanlı fix:
  - Aggregator artık her kaynağı (quote **ve** history yolunda) `PROVIDER_FETCH_TIMEOUT`
    ile sarmalıyor; bir kaynak asılırsa circuit breaker'a hata kaydedilip sonraki kaynağa
    düşülüyor (dış iptal/`CancelledError` bundan ayrı tutulur, yutulmaz).
  - `yahoo` (yfinance) provider'ı: `yf.download(threads=False)`, tüm çağrılar için (crumb
    dahil) TEK, uzun ömürlü, timeout'lu paylaşılan session (yfinance 1.5.1'in process-geneli
    `YfData` Singleton'ıyla uyumlu; per-call oluşturup kapatmak eşzamanlı fetch'lerin
    birbirinin session'ını kapatmasına yol açabiliyordu — bkz. review bulgusu), ve varsayılan
    asyncio executor'ından izole, sınırlı (`max_workers=2`) bir `ThreadPoolExecutor` kullanıyor
    — bir hang artık uygulamanın geri kalanını (paylaşılan thread havuzu) zehirlemiyor.
  - Varsayılan `PROVIDERS` sırasından `yahoo` çıkarıldı (`yahoo_chart,tradingview,isyatirim`
    oldu); `yahoo_chart` aynı Yahoo verisini crumb'sız, saf async `httpx` ile sağlıyor.
    `yahoo` provider sınıfı silinmedi, `PROVIDERS` env'i ile geri eklenebilir.
- **Sınırsız bellek büyümesi hardening:** `/quote/{symbol}`, `/quotes`, `/history/{symbol}`
  sembolü yalnızca BİÇİM olarak doğrular (gerçek takip listesi üyeliğini değil). Kod
  incelemesinde tavan/periyodik temizliği olmayan iki dict tespit edildi ve `_negative`
  önbellekle aynı tavan+budama desenine kavuşturuldu: `SymbolCircuitRegistry._failures`/
  `_opened_at` (tek seferlik/typo sembol sorguları kalıcı "kapalı" kayıt bırakıyordu) ve
  `MemoryStore._history_cache` (sembol×period×interval kombinasyonları hiç temizlenmiyordu).
  İkisi de artık 4096 kayıt tavanına sahip.

## [0.1.0] - 2026-07-05

### Eklendi
- FastAPI + Redis (önbellek + pub/sub) tabanlı mikroservis; Docker Compose (api + updater + redis)
- Çoklu kaynak: Yahoo (yfinance + v8 chart) + İş Yatırım fallback + circuit breaker
- REST uç noktaları: `/all`, `/quote`, `/quotes`, `/history`, `/intraday`, `/symbols`
- SSE canlı akış (`/stream`) — Redis pub/sub fan-out
- Fiyat doğrulama (`/validate`) — çapraz kaynak karşılaştırma + sapma metriği
- Prometheus `/metrics`, JSON loglama, `/health` + `/ready` probe'ları
- API key kimlik doğrulama (çoklu key, timing-safe, SHA-256), rate limit, sanity-check, staleness
- pytest test paketi + GitHub Actions CI

[Yayınlanmamış]: https://github.com/Armert-Labs/bist-data-service/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Armert-Labs/bist-data-service/releases/tag/v0.1.0
