# BIST Canlı API (mikroservis)

BIST hisseleri için **~15 dk gecikmeli**, halka açık fiyat verisini toplayan,
**Redis**'te önbellekleyen ve **REST + SSE (pub/sub)** ile sunan üretim sınıfı bir
veri kaynağı mikroservisi.

Kaynaklar: **Yahoo Finance** (birincil) + **İş Yatırım** (fallback). Login/oturum
gerektirmez.

---

## ⚠️ Yasal ve teknik gerçek

| İhtiyaç | Nasıl | Kısıt |
|---|---|---|
| **Gerçek zamanlı** BIST | BIST lisanslı veri satıcısı + BISTECH | Aylık ücretli + sözleşme |
| **~15 dk gecikmeli** (bu proje) | Yahoo / İş Yatırım | Ücretsiz, **kişisel/iç kullanım** |

Borsa İstanbul ücretsiz gerçek zamanlı API sunmaz. Bu servis bilinçli olarak
**gecikmeli + ücretsiz + login'siz** yolu seçer.

---

## Mimari

```
                 ┌──────────────┐
                 │    Redis     │  cache + pub/sub + staleness + persistence
                 └───┬──────┬───┘
          yazar/yayın│      │okur/dinler
        ┌────────────▼─┐  ┌─▼─────────────────────┐
        │  updater     │  │  api (FastAPI, N adet) │
        │ Yahoo+İşYat. │  │ REST + SSE + /metrics  │
        │ circuit brkr │  │ + webhook alarmları    │
        │ sanity-check │  └───────────────────────┘
        └──────────────┘
```

- **updater**: Tek yazıcı. Batch'ler halinde çeker, sanity-check yapar, Redis'e
  yazar, pub/sub yayınlar, webhook alarmlarını değerlendirir.
- **api**: Stateless, N kopyaya ölçeklenir. Redis'ten okur; SSE'yi pub/sub ile
  besler (polling yok).
- **Redis yoksa** (tek instance): updater + webhook izleyici API sürecinde çalışır
  (in-memory store). `REDIS_URL` boş bırakmak yeterli.

---

## Hızlı başlangıç (Docker Compose — önerilen)

```bash
cd ~/development/bist-canli-api
cp .env.example .env          # opsiyonel; düzenleyebilirsin
docker compose up -d --build
```

Servisler: `redis`, `updater`, `api` (`:8000`). İlk tam tur ~55 sn sürer.

- API: <http://localhost:8000>  •  Dokümanlar: `/docs`  •  Demo: `/demo`

```bash
docker compose logs -f updater   # updater günlüğü
docker compose ps                # durum
docker compose down              # durdur
```

## Alternatif: tek süreç (Redis'siz, geliştirme)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000     # REDIS_URL boş → in-memory + updater içeride
```

---

## Uç noktalar

| Yöntem | Yol | Açıklama |
|---|---|---|
| GET | **`/all`** | **TEK çağrıyla tüm BIST anlık fiyatları** (`sort`, `order`) |
| GET | `/quote/{symbol}` | Tek hisse (cache-miss'te on-demand) |
| GET | `/quotes?symbols=` | Çoklu / tümü |
| GET | `/history/{symbol}` | Geçmiş OHLCV (`period`, `interval`) |
| GET | `/intraday/{symbol}` | Servis içi biriken gün-içi snapshot'lar |
| GET | `/stream?symbols=` | SSE canlı akış (Redis pub/sub fan-out) |
| GET | `/symbols` | Takip listesi |
| GET | `/health` | Liveness (süreç ayakta mı) |
| GET | `/ready` | Readiness (store + taze veri + kaynak durumu) |
| GET | `/metrics` | Prometheus (API HTTP metrikleri) |
| GET | `/demo` | Canlı test sayfası |

`/ready` 503 dönerse: veri bayat veya store erişilemez (yük dengeleyici trafiği kesmeli).

```bash
curl localhost:8000/all
curl "localhost:8000/all?sort=change_percent&order=desc"   # en çok yükselenler
curl localhost:8000/ready
curl -N "localhost:8000/stream?symbols=THYAO,GARAN"
```

---

## Gözlemlenebilirlik

- **API metrikleri**: `http://localhost:8000/metrics` (HTTP istek süreleri, SSE bağlantı sayısı).
- **Updater metrikleri**: updater konteynerinde `:8001/metrics` (güncelleme süresi,
  önbellek boyutu, kaynak sağlığı, sanity redleri, webhook gönderimleri).
- **Loglar**: JSON (LOG_JSON=true) + her isteğe `X-Request-ID`.

Örnek Prometheus scrape hedefleri: `api:8000` ve `updater:8001`.

---

## Yapılandırma (.env)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `REDIS_URL` | *(boş)* | Boş = in-memory. Compose: `redis://redis:6379/0` |
| `UPDATE_INTERVAL` | `60` | Güncelleme aralığı (sn) |
| `BATCH_SIZE` / `BATCH_PAUSE` | `40` / `1.0` | Batch boyutu / arası bekleme |
| `PROVIDERS` | `yahoo,isyatirim` | Kaynak öncelik sırası (fallback) |
| `SANITY_MAX_CHANGE_PCT` | `60` | Absürt fiyat sıçraması filtresi (%) |
| `STALENESS_SECONDS` | `300` | Bu süre güncellenmezse `/ready` fail + `is_stale` |
| `API_KEY` | *(boş)* | Dolu ise `X-API-Key` zorunlu |
| `RATE_LIMIT` | `120/minute` | IP başına limit |
| `MAX_CONCURRENT_FETCH` | `8` | On-demand cekim üst sınırı |
| `MAX_SSE_CLIENTS` | `200` | Eşzamanlı SSE bağlantı sınırı |
| `WEBHOOKS_ENABLED` | `false` | Olay bazlı alarmlar |
| `PERSISTENCE_ENABLED` | `true` | Gün-içi snapshot saklama |

---

## Webhook (olay bazlı alarmlar)

Sürekli akış için **değil** (onun için SSE var); fiyat **eşik alarmları** için.
`webhooks.json` oluştur (`webhooks.example.json`'a bak), `WEBHOOKS_ENABLED=true` yap,
compose'da ilgili `volumes` satırını aç.

```json
{"rules":[{"id":"thyao-ust","symbol":"THYAO","condition":"above",
           "threshold":350,"url":"https://.../webhook","cooldown":300}]}
```
`condition`: `above | below | pct_up | pct_down`. Koşul sağlanınca hedef URL'e POST atılır.

---

## Test

```bash
pip install -r requirements-dev.txt
pytest
```

---

## Ölçekleme notları

- `api`'yi çok kopyaya ölçekle (stateless). `updater` **tek** olmalı (çift çekim olmasın).
- Redis Pub/Sub, SSE fan-out'u tüm `api` kopyalarına dağıtır.
- Kubernetes: `/health` → livenessProbe, `/ready` → readinessProbe.

## Sınırlamalar

- Veri **~15 dk gecikmeli**. Resmî tatil takvimi kontrol edilmez (hafta içi + saat).
- İş Yatırım fallback tek tek sembol çeker (yavaş); yalnızca Yahoo çökünce devreye girer.
- Yalnızca kişisel/iç kullanım; ticari/gerçek zamanlı dağıtım BIST lisansı gerektirir.
