# 📈 BIST Data Service

[![CI](https://github.com/Armert-Labs/bist-data-service/actions/workflows/ci.yml/badge.svg)](https://github.com/Armert-Labs/bist-data-service/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg?logo=docker&logoColor=white)](docker-compose.yml)

> BIST (Borsa İstanbul) hisseleri için **~15 dk gecikmeli**, halka açık fiyat verisini
> toplayan; Redis'te önbellekleyen; **REST + SSE + Prometheus** ile sunan üretim sınıfı
> bir veri kaynağı mikroservisi. Login/oturum gerektirmez.

Kaynaklar: **Yahoo Finance** (v8 chart, birincil) + **İş Yatırım** (bağımsız fallback,
piyasa kapalıyken). **TradingView** (scanner) provider sınıfı vardır ama **hukuki
gerekçeyle (ToS) varsayılan zincirden çıkarılmıştır** — bkz. aşağıdaki not.

---

## ✨ Özellikler

- 🔌 **Tek uç noktadan tüm BIST** — `GET /all` ile ~500+ hissenin anlık fiyatı
- 🔁 **Çok katmanlı fallback** — Yahoo chart → İş Yatırım + circuit breaker (TradingView opsiyonel, env ile)
- 📡 **Canlı akış** — Redis pub/sub tabanlı SSE fan-out (`/stream`)
- ✅ **Fiyat doğrulama** — çapraz-kaynak karşılaştırma + sapma metriği (`/validate`)
- 🔐 **Kimlik doğrulama** — çoklu API key, timing-safe, SHA-256 hash saklama
- 🛡️ **Dayanıklılık** — sanity-check, staleness tespiti, rate limit, bounded fetch
- 📊 **Gözlemlenebilirlik** — Prometheus `/metrics`, JSON log, `/health` + `/ready`
- 🐳 **Üretime hazır** — Docker Compose, multi-stage imaj, non-root, CI/CD

## 🏗️ Mimari

```mermaid
flowchart LR
    subgraph sources[Veri Kaynaklari]
        YC[Yahoo v8 chart]
        IY[Is Yatirim]
        Y[Yahoo yfinance / opsiyonel]
        TV[TradingView scanner / opsiyonel, ToS-kisitli]
    end
    U[updater\ncircuit breaker + sanity-check] -->|cek| sources
    U -->|yaz + publish| R[(Redis\ncache + pub/sub)]
    A[API FastAPI\nN kopya] -->|oku / dinle| R
    C[Istemci / Ana Uygulama] -->|REST + SSE| A
    P[Prometheus] -->|scrape| A
    P -->|scrape| U
```

- **updater** — tek yazıcı; batch çeker, doğrular, Redis'e yazar, pub/sub yayınlar
- **api** — stateless, N kopyaya ölçeklenir; Redis'ten okur, SSE'yi pub/sub ile besler
- **Redis yoksa** — updater API içinde çalışır (tek instance, in-memory); `REDIS_URL` boş bırakın
- **`yahoo` ve `tradingview` varsayılan zincirde DEĞİL** (provider sınıfları silinmedi,
  env ile geri eklenebilir) — `yahoo` teknik risk (crumb/cookie asılma), `tradingview`
  ise **hukuki** gerekçeyle (ToS §3, aşağıya bakın) dışarıda tutuluyor.

### 🕰️ Seans-içi kaynak zinciri gerçeği + bayat-veri düşürme politikası

**İş Yatırım günlük EOD (End-Of-Day) çubuk döndürür** — gün içinde birden fazla
kez sorgulansa da her seferinde *aynı günün* tek kapanış fiyatını verir. Seans
**açıkken** bu artık kabul edilmez: her quote'un dayandığı bar'ın günü
(`bar_time`; sağlamıyorsa `exchange_time`) bugüne ait değilse (piyasa açıkken)
kaynak o sembol için **"hiç veri dönmemiş"** sayılır ve fallback zincirinde bir
sonraki kaynağa düşülür (`bist_stale_bar_skipped_total`). Aynı kural,
**hiçbir zaman damgası** (ne `bar_time` ne `exchange_time`) üretemeyen bir
kaynak için de geçerlidir (`bist_missing_exchange_time_total`) — damgasız veri
seans içinde güvenilmez kabul edilir. `exchange_time` ve `bar_time` farklı
amaçlara hizmet eder — bkz. Quote şeması tablosu.

Pratik sonucu: **seans içinde fiili canlı-fiyat kaynağı yalnızca
`yahoo_chart`'tır** — İş Yatırım yukarıdaki EOD-guard'ı yüzünden seans boyunca
elenir, yalnızca **piyasa kapalıyken** (son kapanış meşru veridir) devreye
girer. Bu, bilinçli bir tasarım kararıdır (sessizce bayat fiyat servis etmek
yerine açıkça düşürmek); `/ready`'deki `providers` durumu "sağlıklı" görünse
bile bir sembolün seans içinde İş Yatırım'dan hiç veri gelmiyor olması
**beklenen** davranıştır. Bu yüzden İş Yatırım (`intraday_capable=False` —
yalnızca günlük EOD çubuk verir) seans **açıkken updater tarafından hiç
sorgulanmaz** (yapısal bir kısıt, arıza değil; provider'ın kendisi seans
kapalıyken normal sorgulanır).

**Provider-seviyesi guard-cooldown (backpressure, half-open):** bir kaynak
ardışık `GUARD_COOLDOWN_FAIL_THRESHOLD` (vars. 3) **TUR** (updater'in tam bir
güncelleme döngüsü — tipik olarak ~13 batch isteğinden oluşur) boyunca
yukarıdaki guard'lar yüzünden isteğinin **tamamını** kaybederse
provider-seviyesinde `GUARD_COOLDOWN_SECONDS` (vars. 1800 sn) süreyle geçici
olarak devre dışı bırakılabilir (`bist_provider_guard_cooldown{provider}`
gauge=1) — aksi halde sembol devre kesici bu düşüşleri saymadığı için kaynak
sonsuza kadar boşuna sorgulanmaya devam ederdi. Sayaç **TUR bazındadır, BATCH
bazında değil**; yalnızca **updater'ın yapılandırılmış döngüsü** bu sayaca
katkıda bulunur — on-demand istekler asla cooldown tetiklemez. En az bir
quote geçtiğinde ardışık sayaç sıfırlanır; `GUARD_DROP_STREAK_MAX_AGE_SECONDS`
(vars. 900 sn) süreden uzun süredir artmayan bir sayaç da geçersiz sayılır.
Seans açılışından sonraki `GUARD_OPEN_GRACE_SECONDS` (vars. **1200 sn = 20
dk**) içindeki düşüşler sayaca hiç yazılmaz — veri ~15 dk gecikmeli olduğu
için (bkz. `delayed: true`) kaynakların açılışta bugüne ait bir damga
üretmesi bu gecikme + tampon kadar sürebilir.

> **Cooldown yalnız ≥2 intraday-capable kaynak varsa uygulanır:**
> cooldown'un amacı "bozuk kaynağı dövme, DİĞERLERİ servis etsin"dir —
> TradingView çıkarıldı + İş Yatırım EOD-only olduğu için **varsayılan
> yapılandırmada seans içinde TEK intraday kaynak (`yahoo_chart`) kalmıştır**.
> Yararlanacak başka bir kaynak yoksa bu tek kaynağı susturmak feed'i KENDİ
> KENDİNE keser; bu durumda cooldown **hiç uygulanmaz** (guard yine bayat
> veriyi eler, sayaç izlenebilirlik için birikmeye devam eder ama kaynak her
> tur yeniden denenir). Cooldown fiilen uygulandığında bile **half-open**
> davranır: cooldown süresince tur başına 1 "prob" denemesi yapılır; prob
> başarılı olursa cooldown **anında** kalkar (tam süreyi beklemez).

> **Süreç kapsamı notu:** cooldown durumu ve `bist_provider_guard_cooldown`
> gauge'u **yalnızca updater'ın döngüsünden** (yukarıdaki `count_toward_cooldown`
> mekanizmasıyla) yazılır; on-demand yol asla yazmaz (yalnızca mevcut bir
> cooldown'a **saygı gösterir**, tetiklemez). Redis'siz (tek-instance)
> dağıtımda updater ve API aynı süreçte çalışsa bile bu ayrım korunur; Redis'li
> (çok-instance) dağıtımda ise updater ve API süreçlerinin ayrı `Aggregator`
> nesneleri olduğu için bu gauge doğal olarak yalnızca updater sürecinin
> `:8001/metrics`'inde anlamlıdır.

**Fail-open emniyet supabı (`bist_guard_fail_open_total`):** bir batch'teki
sembollerin **TAMAMI** guard'la düşerse VE bu batch **temsili büyüklükteyse**
(`len(symbols) >= GUARD_FAIL_OPEN_MIN_SYMBOLS`, vars. 20) fail-open tetiklenir
— **kaynak sayısına değil batch büyüklüğüne bağlıdır** (TradingView çıktı +
İş Yatırım EOD-only olduğu için seans içinde tek kaynak kaldı; "en az 2
kaynak" şartı bu dünyada hiç sağlanamaz, fail-open'i ölü bırakırdı). Büyük/
çeşitli bir sembol kümesinin TAMAMININ tek bir kaynaktan bile aynı anda
guard'a düşmesi tesadüfi değildir, genellikle bir kaynak arızası değil
**piyasa-açık varsayımının** (örn. `MARKET_HOLIDAYS` listesinde eksik bir
resmi tatil) **yanlış** olduğunun işaretidir. Küçük kümeler (on-demand
tek-sembol istekleri dahil, `len(symbols)=1`) eşiğin altında kalır, fail-open'i
hiç tetiklemez. Tetiklendiğinde: guard o batch için geçici olarak devre dışı
bırakılır, elde bulunan (guard'ın düşürdüğü) veri `stale=true` işaretiyle
geçirilir (bu bayrak `/quote`, `/quotes`, `/all`, `/stream` yanıtlarında
KORUNUR — yaş-tabanlı hesap onu asla ezmez), hiçbir kaynak cooldown'a girmez
(sistemik bir sorun tek bir kaynağa atfedilmez; aynı turdaki BAŞKA batch'lerin
guard-drop bilgisi de bu yüzden veto edilir) ve bir CRITICAL log + sayaç
artışı operatörü uyarır.

### ⚖️ TradingView'in varsayılan zincirden çıkarılması (hukuki karar) + bilinen açık maddeler

**TradingView Kullanım Şartları §3** veriyi **yalnızca ekranda-gösterim
(display-only)** ile sınırlar; **otomatik işlem, algoritmik karar-verme, fiyat
referanslama, order verification, risk-yönetim programları** kullanımını **ismen
yasaklar** ve TradingView içeriğine dayalı ürün/servis üretmeyi de yasaklar.
Abonelik satın almak bunu çözmez (satılan şey display-use lisansıdır). BistEye'in
Faz-2'deki client-side stop-loss'u tam bu tanımın ortasına düşer — bu yüzden
`tradingview` **varsayılan `PROVIDERS`/`VALIDATE_PROVIDERS` zincirinden çıkarıldı**.
Provider sınıfı **silinmedi** (`app/providers/tradingview.py`), env ile geri
eklenebilir (`PROVIDERS=yahoo_chart,tradingview,isyatirim`) **ama yalnızca
insan-okur dashboard/teşhis amacıyla, bilinçli bir karar sonucu — çıktısı bot
karar-yoluna (fiyat referanslama, stop-loss/emir tetikleme, otomatik işlem)
bağlanmamalıdır.**

**Bilinen ve kabul edilen sonuçlar (Faz-2 lisanslı realtime kaynak kararına
bağlı açık madde):**

- **Seans içi fallback kalmıyor:** İş Yatırım seans boyunca EOD/bayat bar
  verdiği için guard onu düşürür → seans içinde fiilen **tek kaynak
  `yahoo_chart`**. `yahoo_chart` düşerse feed durur (donma değil, veri
  yokluğu — `/ready` `not ready` döner, alarm çalar). Bilinçli bir takas:
  hukuki temizlik > dayanıklılık, çünkü bot henüz bu veriyi tüketmiyor.
- **Çapraz doğrulama fiilen devre dışı:** seans içinde bağımsız referans
  kalmıyor (birincil `yahoo_chart` kendi kaynağı olarak dışlanır, İş Yatırım
  bayat) → `_pick_reference` **fail-quiet** döner (fiyatı reddetmez, sessizce
  kabul eder), `bist_validate_no_reference_total{reason="stale"}` sayacı
  artar. Bu, HIGH-2 fix'inin sağladığı kazanımın bilinçli olarak geri
  alınması demektir — gerçek (lisanslı) bir realtime referans kaynağı
  gelene kadar başka çözümü yok.

---

## ⚠️ Yasal Not

Borsa İstanbul ücretsiz gerçek zamanlı API sunmaz. Bu servis bilinçli olarak
**gecikmeli + ücretsiz + login'siz** yolu seçer. Gerçek zamanlı/ticari dağıtım
**BIST lisansı** gerektirir. Yalnızca kişisel/iç kullanım içindir.

## 🚀 Hızlı Başlangıç

### Docker Compose (önerilen)

```bash
git clone https://github.com/Armert-Labs/bist-data-service.git
cd bist-data-service
cp .env.example .env          # anahtarları/ayarları düzenleyin
docker compose up -d --build
```
API `:8000` → Dokümanlar `/docs` · Demo `/demo` · Sağlık `/health`

### Yerel (Redis'siz, geliştirme)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make run        # uvicorn app.main:app --reload
```

## 🔗 Uç Noktalar

| Yöntem | Yol | Açıklama | Auth |
|---|---|---|:--:|
| GET | **`/all`** | Tüm BIST anlık fiyatları (`sort`, `order`) | 🔑 |
| GET | `/quote/{symbol}` | Tek hisse | 🔑 |
| GET | `/quotes?symbols=` | Çoklu / tümü | 🔑 |
| GET | `/history/{symbol}` | Geçmiş OHLCV | 🔑 |
| GET | `/intraday/{symbol}` | Gün-içi snapshot'lar | 🔑 |
| GET | `/validate` | Çapraz-kaynak fiyat doğrulama | 🔑 |
| GET | `/stream?symbols=` | SSE canlı akış | 🔑 |
| GET | `/symbols` | Takip listesi | 🔑 |
| GET | `/health` · `/ready` | Liveness · Readiness | — |
| GET | `/metrics` | Prometheus | 🔑* |

`*` `METRICS_PUBLIC=true` ise açık. `🔑` = `API_KEYS` tanımlıysa `X-API-Key` gerekir.

```bash
curl -H "X-API-Key: <anahtar>" http://localhost:8000/all
curl -H "X-API-Key: <anahtar>" "http://localhost:8000/all?sort=change_percent&order=desc"
```

## 🔌 Entegrasyon Rehberi

Bu bölüm, başka bir projeyi (mobil uygulama, backend, dashboard, bot vb.) bu
mikroservise **adım adım** bağlar. Tüm örnekler gerçek istek/yanıtlardır; örnek
veriler piyasa **kapalıyken** (son kapanış) alınmıştır. Uç nokta özeti için
yukarıdaki [🔗 Uç Noktalar](#-uç-noktalar) tablosuna bakın.

### 📶 Üç kanal özeti

Servis veriyi üç farklı şekilde sunar; ihtiyacınıza göre birini veya birkaçını kullanın:

| Kanal | Tip | Ne zaman kullanılır | Protokol |
|---|---|---|---|
| **REST** (`/quote`, `/quotes`, `/all`, `/history`, `/intraday`) | **Pull** — anlık + geçmiş | İhtiyaç anında tek seferlik sorgu, periyodik yoklama | HTTP GET → JSON |
| **`/stream`** | **Push** — canlı akış | Sürekli güncel fiyat isteyen dashboard/ticker | **SSE (Server-Sent Events)** |
| **Webhook** | **Push** — olay bazlı alarm | Fiyat bir eşiği/koşulu tetiklediğinde bildirim | HTTPS POST → JSON |

> ⚠️ **Canlı akış WebSocket DEĞİL, SSE'dir.** `/stream` tek yönlü (sunucu→istemci)
> `text/event-stream` yayınıdır. Tarayıcıda `EventSource`, sunucu tarafında
> fetch-stream / SSE client ile tüketilir. WS handshake beklemeyin.

### 🔑 Bağlanırken kimlik doğrulama

En az bir anahtar tanımlıysa veri uçları `X-API-Key` ister (`AUTH_REQUIRED`
değerinden bağımsız); her istekte bu başlığı gönderin. `AUTH_REQUIRED=true` iken
_hiç anahtar tanımlı değilse_ veri uçları `503` döner (fail-safe: yanlışlıkla
auth'suz açık kalmayı önler). `/health` ve `/ready` her zaman açıktır.

```bash
curl -H "X-API-Key: <anahtar>" http://localhost:8000/quote/THYAO
```

- **`/health` ve `/ready` her zaman açıktır** (probe için); bunlar anahtar istemez.
- Anahtar **eksik/yanlış** → `401 Unauthorized`.
- `AUTH_REQUIRED=true` ama **hiç anahtar tanımlı değilse** fail-safe devreye girer,
  veri uçları `503` döner (yanlışlıkla korumasız açılmayı engeller).
- **SSE ve tarayıcı uyarısı:** Tarayıcı `EventSource` özel başlık **gönderemez**.
  Auth açıkken `/stream`'i **sunucu tarafından** (fetch-stream / `sseclient`,
  `X-API-Key` ile) tüketin. Detay için [SSE bölümüne](#-sse--canlı-akış) bakın.

### 💹 REST — Anlık fiyat

#### `GET /quote/{symbol}` — tek hisse

```bash
curl -H "X-API-Key: <anahtar>" http://localhost:8000/quote/THYAO
```

```json
{
  "symbol": "THYAO",
  "price": 348.25,
  "previous_close": 328.5,
  "change": 19.75,
  "change_percent": 6.01,
  "open": null,
  "day_high": 355.5,
  "day_low": 345.25,
  "volume": 60645389,
  "currency": "TRY",
  "market_state": "CLOSED",
  "source": "yahoo_chart",
  "delayed": true,
  "updated_at": "2026-07-08T05:32:28.389121Z",
  "exchange_time": "2026-07-07T15:09:55Z",
  "data_age_seconds": 3.2,
  "stale": false
}
```

Alanların anlamı için [Yanıt alan referansı](#-yanıt-alan-referansı).

#### `GET /quotes?symbols=` — çoklu hisse

Virgülle ayrılmış liste verin; boş bırakırsanız tüm takip listesi döner.
Bulunamayan semboller `missing` dizisinde raporlanır (404 **değil**).

```bash
curl -H "X-API-Key: <anahtar>" "http://localhost:8000/quotes?symbols=THYAO,YOKSYM"
```

```json
{
  "count": 1,
  "market": "CLOSED",
  "missing": ["YOKSYM"],
  "quotes": {
    "THYAO": { "symbol": "THYAO", "price": 348.25, "change_percent": 6.01, "...": "…tam Quote alanları" }
  }
}
```

#### `GET /all` — tüm BIST + sıralama, ETag, gzip

`sort=symbol|price|change|change_percent|volume` ve `order=asc|desc` ile sıralanır.
Yanıt bir **zarf** (envelope) içinde gelir; her hisse `quotes` dizisindedir.

```bash
curl -H "X-API-Key: <anahtar>" \
  "http://localhost:8000/all?sort=change_percent&order=desc"
```

```json
{
  "market": "CLOSED",
  "count": 616,
  "last_update": "2026-07-08T05:32:30.622217+00:00",
  "is_stale": false,
  "delayed": true,
  "quotes": [
    { "symbol": "THYAO", "price": 348.25, "change_percent": 6.01, "...": "…" }
  ]
}
```

**Bant genişliği tasarrufu — ETag / 304 ve gzip:** `/all` yanıtı bir `ETag`
döndürür. Değişmediyse `If-None-Match` ile `304 Not Modified` (boş gövde) alırsınız.
`Accept-Encoding: gzip` ile gövde sıkıştırılır.

```bash
# 1) İlk istek: ETag başlığını al (gzip ile)
curl -sD- -o /dev/null -H "X-API-Key: <anahtar>" -H "Accept-Encoding: gzip" \
  "http://localhost:8000/all?sort=change_percent&order=desc"
# ... yanıt başlıkları:  ETag: W/"76b3bc0f454e53b3"   Content-Encoding: gzip

# 2) Sonraki istek: veri değişmediyse 304 (gövde boş → veri tekrar indirilmez)
curl -sD- -o /dev/null -H "X-API-Key: <anahtar>" \
  -H 'If-None-Match: W/"76b3bc0f454e53b3"' \
  "http://localhost:8000/all?sort=change_percent&order=desc"
# ... HTTP/1.1 304 Not Modified
```

#### `GET /symbols` — takip listesi

Servisin takip ettiği BIST sembol listesini döner (bir sorgudan önce geçerli
sembolleri öğrenmek için kullanışlıdır).

```bash
curl -H "X-API-Key: <anahtar>" http://localhost:8000/symbols
```

#### `GET /validate?symbols=` — çapraz-kaynak doğrulama (salt-okunur)

Fiyatı ikinci bir kaynakla karşılaştırıp sapmayı raporlar; veriyi değiştirmez.

```bash
curl -H "X-API-Key: <anahtar>" "http://localhost:8000/validate?symbols=THYAO,GARAN"
```

```json
{
  "checked": 2,
  "compared": false,
  "threshold_pct": 1.0,
  "reference_status": { "yahoo_chart": "ok", "isyatirim": "veri_yok" },
  "max_deviation_pct": 0.0,
  "consistent": false,
  "comparisons": [
    {
      "symbol": "THYAO",
      "primary": 348.25,
      "primary_source": "yahoo_chart",
      "references": {
        "yahoo_chart": { "price": 348.25, "deviation_pct": 0.0, "ok": true, "self": true },
        "isyatirim": { "price": null, "deviation_pct": null, "ok": false, "self": false }
      }
    }
  ]
}
```

> **Seans içi tipik durum (bilinçli takas, yukarıya bakın):** varsayılan zincirde
> (`tradingview` yok) birincil kaynak `yahoo_chart` iken tek olası bağımsız
> referans İş Yatırım'dır; o da seans içinde EOD-guard'ı yüzünden elenir —
> yukarıdaki örnekte olduğu gibi `compared: false` ("tutarsız" DEĞİL, "hiçbir
> bağımsız referansla karşılaştırılamadı") görmek **beklenen** davranıştır.
> `PROVIDERS`/`VALIDATE_PROVIDERS`'a `tradingview` env ile eklenirse (yalnızca
> insan-teşhis amacıyla) üçüncü bir referans daha görünür.

> **`self` alanı:** Quote'un KENDİ kaynağıyla karşılaştırılan referans (`self: true`)
> her zaman ~%0 sapma verir (aynı kaynağın tekrar sorgulanmasıdır) — bu **totolojik**
> bir kontroldür, gerçek bağımsız doğrulama DEĞİLDİR. Resmî `max_deviation_pct`/
> `consistent` alanları bu girdiyi otomatik dışlar (yalnızca bağımsız — `self:
> false` — referanslara bakar); tablo yalnızca **şeffaflık** için tüm referansları
> gösterir. Bağımsız hiçbir referans yoksa (hepsi kendi kaynağı/bayat/erişilemez)
> `compared:false` döner ("tutarsız" değil "karşılaştırılamadı").

#### `GET /ready` — hazırlık yoklaması (probe, auth'suz)

Hazırsa `200`, hazır değilse **aynı gövdeyle** `503` döner. Orchestrator/health-check
için idealdir (bkz. [Hata ve limitler](#-hata-ve-limitler)).

```bash
curl http://localhost:8000/ready
```

```json
{
  "ready": true,
  "store_ok": true,
  "quotes_cached": 616,
  "is_stale": false,
  "fresh_pct": 100.0,
  "last_update_age_seconds": 3.2,
  "oldest_quote_age_seconds": 15.3,
  "market_open": false,
  "providers": { "yahoo": "closed", "yahoo_chart": "closed", "tradingview": "closed", "isyatirim": "closed" }
}
```

> **Provider durumu ters okunur:** `"closed"` = circuit **kapalı** = **SAĞLIKLI**.
> `"open"` = circuit açık = kaynak geçici devre dışı. `"half_open"` = toparlanıyor.

#### `GET /health` — liveness (auth'suz)

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok", "version": "0.1.0" }
```

### 🕰️ REST — Geçmiş

#### `GET /history/{symbol}` — OHLCV barları

| Parametre | İzin verilen değerler |
|---|---|
| `period` | `1d` `5d` `1mo` `3mo` `6mo` `1y` `2y` `5y` `10y` `ytd` `max` |
| `interval` | `1m` `2m` `5m` `15m` `30m` `60m` `90m` `1h` `1d` `5d` `1wk` `1mo` `3mo` |

```bash
curl -H "X-API-Key: <anahtar>" \
  "http://localhost:8000/history/THYAO?period=5d&interval=1d"
```

```json
{
  "symbol": "THYAO",
  "period": "5d",
  "interval": "1d",
  "currency": "TRY",
  "bars": [
    { "time": "2026-07-07T00:00:00+03:00", "open": 346.0, "high": 355.5, "low": 345.25, "close": null, "volume": 60645389 }
  ]
}
```

> Devam eden/erken seansta `close` `null` olabilir (bar henüz kapanmadı).

#### `GET /intraday/{symbol}` — gün-içi noktalar

Servisin biriktirdiği gün-içi `(zaman, fiyat)` noktalarını döner (mini grafik/ticker için).

```bash
curl -H "X-API-Key: <anahtar>" http://localhost:8000/intraday/THYAO
```

```json
{
  "symbol": "THYAO",
  "count": 33,
  "points": [
    { "t": "2026-07-08T05:28:55.551047+00:00", "p": 348.25 }
  ]
}
```

### 📡 SSE — Canlı akış

`GET /stream` bir `text/event-stream` yayını açar. **Bağlanır bağlanmaz ilk olay
tam bir snapshot'tır** (o anki tüm fiyatlar); sonra store her güncellendiğinde yeni
olay gelir. `symbols=` ile filtrelenir (boş = tüm liste). Her ~15 sn'de bir `: ping`
keep-alive yorumu gönderilir.

**Olay formatı:**

```
event: quotes
data: {"market":"CLOSED","quotes":{"THYAO":{...Quote},"GARAN":{...Quote}}}

: ping - 2026-07-08T05:32:28Z
```

> `: ` ile başlayan satır SSE yorum satırıdır (keep-alive); istemci yok sayar.

#### (a) Tarayıcı — `EventSource`

```html
<script>
  // ⚠️ Tarayıcı EventSource ÖZEL BAŞLIK GÖNDEREMEZ.
  // Auth açıksa (AUTH_REQUIRED=true) bu yol çalışmaz; sunucu-taraf istemci kullanın.
  const es = new EventSource("http://localhost:8000/stream?symbols=THYAO,GARAN");
  es.addEventListener("quotes", (e) => {
    const data = JSON.parse(e.data);
    console.log(data.market, data.quotes.THYAO?.price);
  });
  es.onerror = (e) => console.warn("SSE hata / yeniden bağlanıyor", e);
</script>
```

#### (b) Python — `httpx` stream (X-API-Key ile)

```python
import json
import httpx

BASE, HEADERS = "http://localhost:8000", {"X-API-Key": "<anahtar>"}

with httpx.stream("GET", f"{BASE}/stream?symbols=THYAO,GARAN",
                  headers=HEADERS, timeout=None) as resp:
    resp.raise_for_status()
    event = None
    for line in resp.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            print(event, payload["market"], list(payload["quotes"]))
```

> Alternatif: `sseclient` (btubbs) paketi ile `SSEClient(url, headers={"X-API-Key": "..."})`
> başlık destekler. (`sseclient-py`/mpetazzoni farklı bir imza kullanır; yukarıdaki
> httpx örneği en taşınabilir yoldur.)

#### (c) Node — `eventsource` (başlık destekli)

```js
// npm i eventsource  (yerleşik/ tarayıcı EventSource'un aksine başlık verilebilir)
import { EventSource } from "eventsource";

const es = new EventSource("http://localhost:8000/stream?symbols=THYAO,GARAN", {
  fetch: (url, init) =>
    fetch(url, { ...init, headers: { ...init.headers, "X-API-Key": "<anahtar>" } }),
});

es.addEventListener("quotes", (e) => {
  const data = JSON.parse(e.data);
  console.log(data.market, Object.keys(data.quotes));
});
```

### 🔔 Webhook — Olay bazlı push

Fiyat bir koşulu tetiklediğinde servis, tanımladığınız URL'e JSON `POST` eder.
`WEBHOOKS_ENABLED=true` ve `WEBHOOKS_CONFIG` ile kural dosyası verilerek açılır.

**`webhooks.json` kural şeması:**

```json
{
  "rules": [
    {
      "id": "thyao-ust-350",
      "symbol": "THYAO",
      "condition": "above",
      "threshold": 350,
      "url": "https://ornek.com/webhook",
      "cooldown": 300
    }
  ]
}
```

| Alan | Anlamı |
|---|---|
| `id` | Kural kimliği (payload'da `rule_id` olarak döner) |
| `symbol` | İzlenecek BIST sembolü |
| `condition` | `above` (fiyat eşiğin üstüne çıkınca) · `below` (altına inince) · `pct_up` (yüzde değişim eşiği yukarı) · `pct_down` (yüzde değişim eşiği aşağı) |
| `threshold` | Eşik değeri (fiyat veya yüzde) |
| `url` | Hedef — **HTTPS zorunlu** (SSRF koruması) |
| `cooldown` | Aynı kural için tekrar tetiklenmeden önce beklenecek sn (varsayılan `300`) |

**Tetiklenince hedefe `POST` edilen gerçek payload:**

```json
{
  "rule_id": "thyao-ust-300",
  "symbol": "THYAO",
  "condition": "above",
  "threshold": 300.0,
  "price": 348.25,
  "change_percent": 6.01,
  "triggered_at": "2026-07-08T05:35:21.811634+00:00"
}
```

> **Güvenlik:** `url` **HTTPS** olmalıdır; opsiyonel `WEBHOOK_URL_ALLOWLIST` ile
> hedef hostname'leri kısıtlayın. İmza (HMAC) **yoktur**, düz JSON POST'tur —
> alıcıyı tahmin edilemez bir path/secret ile koruyun. Teslimat non-blocking'tir;
> hata olursa `webhook_max_retries` kez backoff ile yeniden denenir.

**Ortam değişkenleri:**

```
WEBHOOKS_ENABLED=true
WEBHOOKS_CONFIG=/app/webhooks.json
WEBHOOK_URL_ALLOWLIST=ornek.com     # opsiyonel; virgülle çoklu hostname
```

**Alıcı (receiver) örneği — Python (FastAPI):**

```python
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/webhook")
async def receive(req: Request):
    e = await req.json()
    print(e["rule_id"], e["symbol"], e["price"], e["condition"], e["threshold"])
    return {"ok": True}
```

**Alıcı örneği — Node (Express):**

```js
import express from "express";
const app = express();
app.use(express.json());
app.post("/webhook", (req, res) => {
  const e = req.body;
  console.log(e.rule_id, e.symbol, e.price, e.condition, e.threshold);
  res.json({ ok: true });
});
app.listen(9000);
```

### 🧩 İstemci kod örnekleri — "diğer projeye bağlanma"

#### Python (`requests` periyodik çekim + `httpx` SSE)

```python
import requests

BASE, HEADERS = "http://localhost:8000", {"X-API-Key": "<anahtar>"}

# Periyodik anlık fiyat
r = requests.get(f"{BASE}/quote/THYAO", headers=HEADERS, timeout=10)
r.raise_for_status()
q = r.json()
print(q["symbol"], q["price"], q["change_percent"])

# Toplu çekim
allq = requests.get(f"{BASE}/all?sort=change_percent&order=desc",
                    headers=HEADERS, timeout=15).json()
print(allq["count"], "hisse,", "bayat" if allq["is_stale"] else "taze")
```

Canlı akış için yukarıdaki [SSE — Python örneğine](#b-python--httpx-stream-x-api-key-ile) bakın.

#### Node / JS (`fetch` + `EventSource`)

```js
const BASE = "http://localhost:8000";
const H = { "X-API-Key": "<anahtar>" };

// Periyodik anlık fiyat
const res = await fetch(`${BASE}/quote/THYAO`, { headers: H });
if (!res.ok) throw new Error(`HTTP ${res.status}`);
const q = await res.json();
console.log(q.symbol, q.price, q.change_percent);
```

Canlı akış için [SSE — Node örneğine](#c-node--eventsource-başlık-destekli) bakın.

#### curl (hızlı test)

```bash
curl -H "X-API-Key: <anahtar>" http://localhost:8000/quote/THYAO
curl -H "X-API-Key: <anahtar>" "http://localhost:8000/quotes?symbols=THYAO,GARAN"
curl -N -H "X-API-Key: <anahtar>" "http://localhost:8000/stream?symbols=THYAO"   # SSE (-N: buffersız)
```

### 📖 Yanıt alan referansı

Quote nesnesindeki alanlar (`/quote`, `/quotes`, `/all`, `/stream` içinde aynıdır):

| Alan | Tip | Anlamı |
|---|---|---|
| `symbol` | string | BIST sembolü (örn. `THYAO`) |
| `price` | number | Son fiyat (**~15 dk gecikmeli**) |
| `previous_close` | number | Önceki seans kapanışı |
| `change` | number | Fiyat değişimi (mutlak, TRY) |
| `change_percent` | number | Yüzde değişim |
| `open` | number \| null | Açılış (kaynak sağlamazsa `null`) |
| `day_high` / `day_low` | number | Gün-içi en yüksek / en düşük |
| `volume` | number | İşlem hacmi (adet) |
| `currency` | string | Her zaman `"TRY"` |
| `market_state` | enum | `OPEN` \| `CLOSED` \| `UNKNOWN` — seans durumu |
| `source` | string | Fiyatı sağlayan kaynak (`yahoo_chart`, `isyatirim`, …) |
| `delayed` | bool | BIST için **her zaman `true`** (gerçek-zamanlı değil) |
| `updated_at` | ISO-8601 UTC | Servisin cache'e **yazım anı** (veri tazeliği bununla ölçülür) |
| `exchange_time` | ISO-8601 UTC \| null | Borsadaki **gerçek işlem zamanı** (kaynak sağlarsa). **Sadece** `data_age_seconds` (yaş) hesabında kullanılır — bayat-bar tespiti için kullanılmaz |
| `bar_time` | ISO-8601 UTC \| null | Fiyatın dayandığı bar'ın ait olduğu gün/an (gün granülerliği yeterli). Bayat-bar tespiti (`bist_stale_bar_skipped_total`) bunu kullanır — `exchange_time` yoksa (örn. TradingView bar-açılış damgası, İş Yatırım hiç) bile bu alandan çalışabilir |
| `data_age_seconds` | number \| null | `exchange_time` (varsa) veya `updated_at`'ten bu yana geçen süre (sn); **okuma anında** hesaplanır — `bar_time` yaş hesabına KARIŞMAZ (bar-açılış damgası "az önce çekildi" ile "bugün sabah açılış" arasını ayıramaz) |
| `stale` | bool | Seans **açıkken** `data_age_seconds`, `STALENESS_SECONDS` eşiğini aşarsa `true`. Kapalıyken her zaman `false` (kapanış fiyatı bayatlamaz) |

Zarf (envelope) düzeyi alanlar (`/all`, `/quotes`, `/stream`): `market`, `count`,
`last_update`, `is_stale` (veri bayat mı), `delayed`, ve `/quotes`'ta `missing`.

> **Gecikme & seans:** Veri **~15 dk gecikmelidir** (`delayed: true`). BIST seansı
> **10:00–18:15** (Europe/Istanbul, UTC+3). Piyasa kapalıyken (`market_state: "CLOSED"`)
> değerler **son kapanışı** yansıtır ve bu durumda **bayatlamaz** (`is_stale: false`) —
> yani kapalı piyasada eski görünen `exchange_time` normaldir.

### ⚠️ Hata ve limitler

| Kod | Anlam | Ne yapmalı |
|---|---|---|
| `400` | Geçersiz parametre (bilinmeyen `period`/`interval`/`sort` vb.) | İstek parametrelerini düzeltin |
| `401` | `X-API-Key` eksik/yanlış | Doğru anahtarı `X-API-Key` başlığıyla gönderin |
| `404` | Sembol/kaynak bulunamadı | Sembolü `/symbols` veya `/validate` ile doğrulayın (çoklu sorguda `/quotes` bunu `missing`'e koyar) |
| `429` | Rate limit aşıldı | `Retry-After` başlığı kadar bekleyin; limit `X-RateLimit-Limit`'te (varsayılan `120/minute`) |
| `503` | Servis hazır değil / fail-safe auth (anahtarsız `AUTH_REQUIRED`) | `/ready` ile hazırlığı yoklayıp tekrar deneyin |

**Hazırlık yoklama (orchestrator / health-check önerisi):**

```bash
# 200 → hazır; 503 → henüz değil (aynı gövde döner). Deploy/başlangıçta bunu bekleyin.
until curl -fsS http://localhost:8000/ready >/dev/null; do sleep 2; done
```

- **Liveness** için `/health`, **readiness** için `/ready` kullanın (ikisi de auth'suz).
- `429` alırsanız `Retry-After`'a uyun; sabit hızlı yoklama yerine exponential backoff önerilir.
- Servis geçici `503` verse bile REST/SSE istemcinizi otomatik yeniden denemeye ayarlayın.

## 🔐 Kimlik Doğrulama

```bash
# Anahtar üret
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
`.env` içinde:
```
API_KEYS=<anahtar>:mobil,<anahtar2>:web   # coklu, etiketli
AUTH_REQUIRED=true                         # anahtar yoksa 503 (fail-safe)
METRICS_PUBLIC=false                       # /metrics de auth ister
```
Üretimde plaintext yerine `API_KEYS_SHA256` ile hash saklayabilirsiniz.

## 📊 Gözlemlenebilirlik

- API metrikleri: `:8000/metrics` · Updater iş metrikleri: `:8001/metrics`
- cAdvisor (container bazlı bellek/CPU/OOM metrikleri): `:8081` (host port `8080` zaten
  `panel` servisi tarafından kullanıldığı için `8081:8080` ile eşlenir)
- Örnek Prometheus + Grafana yapılandırması: [`deploy/`](deploy/)
- Yapısal JSON log + her isteğe `X-Request-ID`

## 🤖 Telegram Bot

BIST fiyatlarını Telegram'dan sunan **opsiyonel** bot (`bot` servisi). REST API'nin
bir istemcisidir (ham `httpx`; `python-telegram-bot` kullanmaz). Varsayılan
**kapalı** — `TELEGRAM_ENABLED=false` iken veya token yokken temiz çıkar (crash yok).

### Kurulum

1. [@BotFather](https://t.me/BotFather) → `/newbot` ile token alın.
2. `.env` (git-ignored) dosyasına ekleyin:
   ```env
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=123456:AA...        # ASLA repoya commit etmeyin
   TELEGRAM_API_URL=http://api:8000       # compose ağı içinde
   # TELEGRAM_API_KEY=...                 # BIST API auth açıksa X-API-Key
   # TELEGRAM_ALLOWED_CHATS=123,456       # boş = herkes /start edebilir
   ```
3. `docker compose up -d bot`
4. Telegram'da bota **`/start`** gönderin (chat kaydı; kayıtlı chat'ler Redis'te tutulur).

### Komutlar

| Komut | İşlev |
|---|---|
| `/start` | Chat'i kaydeder + şık karşılama |
| `THYAO` · `/hisse THYAO` | Anlık hisse kartı (fiyat, değişim, gün içi) |
| `/durum` | Piyasa durumu, izlenen sayı, tazelik, kaynak sağlığı |
| `/yardim` | Komut listesi |
| `/stop` | Bildirimleri kapatır (kaydı siler) |

### Otomatik bildirimler

Piyasa **açılışında** (10:00) ve **kapanışında** (18:15) tüm kayıtlı chat'lere
_sessiz_ (bildirim sesi olmadan) mesaj gider: açılışta izlenen hisse sayısı;
kapanışta günün en çok yükselen/düşenleri. Mesajlar HTML biçimli, emoji ve
görsel öğelerle tasarlanmıştır (Armert × Bisteyes).

### Güvenlik

- Token yalnızca `.env`'den okunur; koda gömülmez, log'a sızmaz.
- `TELEGRAM_ALLOWED_CHATS` ile `/start`'ı belirli chat'lerle sınırlayabilirsiniz.
- Bot ağ hatasında çökmez (üstel backoff); bir chat'e teslimat hatası diğerlerini etkilemez.

## 🧪 Geliştirme

```bash
make install     # bağımlılıklar + pre-commit
make lint        # ruff
make typecheck   # mypy
make test        # pytest
make cov         # pytest + kapsam
```
Ayrıntılar için [CONTRIBUTING.md](CONTRIBUTING.md).

## ⚙️ Yapılandırma (öne çıkanlar)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `REDIS_URL` | *(boş)* | Boş = in-memory. Compose: `redis://redis:6379/0` |
| `PROVIDERS` | `yahoo_chart,isyatirim` | Kaynak fallback zinciri (`yahoo` teknik risk, `tradingview` **hukuki** gerekçeyle — ToS §3, bkz. yukarısı — varsayılandan çıkarıldı; ikisi de provider sınıfı silinmeden env ile geri eklenebilir) |
| `PROVIDER_MODE` | `gapfill` | `failover` \| `gapfill` \| `hybrid` |
| `PROVIDER_FETCH_TIMEOUT` | `45` | Tek `provider.fetch_quotes()` çağrısı için sert üst sınır (sn); aşılırsa sonraki kaynağa düşülür |
| `GUARD_COOLDOWN_FAIL_THRESHOLD` | `3` | Bir kaynağın ardışık kaç **TUR** (batch değil) boyunca tamamen bayat-bar/damgasız guard'ıyla düşerse provider-seviyesinde cooldown'a alınacağı |
| `GUARD_COOLDOWN_SECONDS` | `1800` | Cooldown süresi (sn) — bu sürede kaynak sorgulanmaz, sonra yeniden denenir |
| `GUARD_OPEN_GRACE_SECONDS` | `1200` | Seans açılışından sonraki bu kadar saniye içindeki guard-düşüşleri cooldown sayacına yazılmaz (20 dk = ~15 dk veri gecikmesi + tampon) |
| `GUARD_DROP_STREAK_MAX_AGE_SECONDS` | `900` | Son artıştan bu kadar saniye sonra hâlâ yeni bir tam-düşme olmadıysa sayaç geçersiz sayılır (yaşlanma) |
| `GUARD_FAIL_OPEN_MIN_SYMBOLS` | `20` | Bir batch'in TAMAMI guard'la düşerse fail-open'ın tetiklenmesi için gereken minimum sembol sayısı (kaynak sayısına değil batch büyüklüğüne bağlı eşik) |
| `UPDATE_INTERVAL` | `60` | Güncelleme aralığı (sn) |
| `STALENESS_SECONDS` | `300` | Bayatlık eşiği (`/ready`) |
| `RATE_LIMIT` | `120/minute` | IP başına limit |
| `MAX_SYMBOLS_PER_REQUEST` | `100` | `/quotes` ve `/validate` için tek istekteki sembol sayısı üst sınırı |
| `CORS_ORIGINS` | *(boş)* | Güvenli varsayılan: same-origin only. Panel/dashboard nginx reverse-proxy ile aynı-origin gittiği için etkilenmez; cross-origin bir tarayıcı istemciniz varsa açıkça set edin |

Tam liste: [`.env.example`](.env.example)

## 📈 Ölçekleme

- `api`'yi çok kopyaya ölçekle (stateless); `updater` **tek** olmalı.
- Redis Pub/Sub, SSE fan-out'u tüm kopyalara dağıtır.
- K8s: `/health` → liveness, `/ready` → readiness probe.

## 🤝 Katkı & Lisans

Katkılar için [CONTRIBUTING.md](CONTRIBUTING.md) · Güvenlik: [SECURITY.md](SECURITY.md)
· Davranış: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

[MIT Lisansı](LICENSE) © 2026 Armert Labs
