# Değişiklik Günlüğü

Bu projenin tüm önemli değişiklikleri bu dosyada belgelenir.

Biçim [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) temellidir ve
proje [Semantic Versioning](https://semver.org/lang/tr/) kullanır.

## [Yayınlanmamış]

### Eklendi
- `Quote.data_age_seconds` + `Quote.stale` — her okumada (exchange_time varsa
  ondan, yoksa updated_at'ten) hesaplanan per-sembol tazelik alanları
  (`/quote`, `/quotes`, `/all`, `/stream`'de; geriye uyumlu ek alanlar)
- `bist_quotes_by_source{source}` gauge — her güncelleme turunda kaynak başına
  yazılan quote sayısı (failover'ı sessizlikten çıkarır)
- `bist_stale_bar_skipped_total{provider}` counter — bayat-bar guard'ının
  (aşağıya bakın) devreye girdiği sayı
- `docker-compose.yml`'de updater metrik portu (`8001`) artık `ports` ile
  host'a publish ediliyor (önceden yalnızca `expose`; VM140 Prometheus'u
  compose ağı dışından scrape edemiyordu)
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
- **Veri-doğruluk hardening (13 Tem denetimi, H2/H3/M1):**
  - **Bayat-bar guard (H2):** Seans içi denetimde İş Yatırım'ın seans açıkken
    dünkü kapanışı `updated_at=şimdi` damgasıyla "canlı fiyat" gibi sunduğu
    tespit edildi (`exchange_time=None` olduğu için hiçbir mekanizma bunu
    yakalamıyordu). Fix: İş Yatırım artık son günlük çubuğun tarihini
    `exchange_time`'a taşıyor; `market.is_stale_bar()` yeni genel kuralı,
    seans açıkken bugüne ait olmayan bir `exchange_time`'a dayanan quote'u
    "hiç gelmemiş" sayıp aggregator'da bir sonraki kaynağa düşürüyor (kapalı
    seansta son kapanış zaten meşru veri, guard devreye girmiyor).
  - **Çapraz-doğrulama totolojisi (H3):** `cross_validate_quotes` referans
    seçerken artık quote'un KENDİ kaynağını yapısal olarak dışlıyor (config'e
    bağlı değil) — önceden `PROVIDERS[0] == VALIDATE_PROVIDERS[0]` olduğunda
    kaynak kendi kendini doğrulayıp sahte `consistent:true` üretebiliyordu.
    Bağımsız referans yoksa (hepsi kendi kaynağı/bayat/erişilemez) sessizce
    "doğrulanamadı" sayılır (fail-quiet, sahte red/alarm üretilmez); aynı
    kural drift monitörü ve H2 guard'ı için de geçerli.
  - **TradingView türetilmiş alanlar (M1):** `previous_close`/`change`/
    `change_percent` TradingView `/scan`'de evrensel olarak `null` geliyordu;
    kök neden `ch`/`chp`/`prev_close_price` kolon adlarının desteklenmemesi
    (varsayım — canlıda henüz doğrulanmadı, sonraki canlı denetimde teyit
    edilmeli). `change_abs`/`change` kolonlarına geçildi; `previous_close`
    artık aynı yanıttaki `change_abs`'den türetiliyor (`price - change_abs`),
    başka bir kaynağa gidilmiyor. `change_abs` de boşsa alan açıkça `None`
    bırakılır + loglanır.
- **Veri-doğruluk hardening review-fix turu (3 HIGH + 4 MEDIUM + 3 LOW):**
  - **HIGH-1 (kritik kör nokta):** Canlı olayda TradingView `exchange_time`
    üretmediği için H2 guard'dan tamamen MUAF kalıyordu; guard yahoo_chart'ı
    (bayat) düşürünce gapfill TradingView'e geçiyor ve **2 yıllık bayat bir
    fiyat `stale:false` ile servis ediliyordu**. Fix: TradingView `/scan`'in
    `time` kolonu (unix epoch, canlı doğrulandı) artık `exchange_time`'a
    taşınıyor. Ayrıca genel kural sıkılaştırıldı: seans içinde `exchange_time`
    hiç üretemeyen (damgasız) bir quote da artık düşürülür
    (`bist_missing_exchange_time_total{provider}`) — damgasız veri seans
    içinde güvenilmez sayılır. `tradingview.py`'deki yanlış "lp piyasa
    kapalıyken null olur" yorumu düzeltildi (canlı kanıt: seans açıkken de
    null geliyor, TV fiilen hep `close` döndürüyor).
  - **HIGH-2 (dogrulama seans içinde ölü):** Varsayılan `VALIDATE_PROVIDERS`
    (`yahoo_chart,isyatirim`) ile birincil `yahoo_chart`'tan gelince tek olası
    bağımsız referans `isyatirim` kalıyordu; o da H2 guard'ı yüzünden seans
    içinde elenince doğrulama **tamamen ölü** kalıyordu. Varsayıma
    `tradingview` eklendi (`yahoo_chart,tradingview,isyatirim`). Yeni
    `bist_validate_no_reference_total{reason="stale"|"no_data"}` sayacı
    "temiz" ile "hiç bakılamadı"yı ayırt eder.
  - **HIGH-3 + MEDIUM-1 (drift monitörü + `/validate` totolojik/çelişkili):**
    own-source dışlaması olmadan `run_drift_monitor` kendi kendini
    doğrulayıp `bist_validation_consistent` gauge'unu kalıcı `1`'de
    bırakabiliyordu; ayrıca `/validate` endpoint'i AYRI bir implementasyonla
    aynı gauge'lara çelişen değer yazıyordu. Üç yol (yazma-zamanı
    `cross_validate_quotes`, arka plan `run_drift_monitor`, insan-teşhis
    `/validate`) artık TEK bir çekirdeği (`gather_reference_quotes` +
    `compare_against_references` + `_pick_reference`, `pipeline.py`) paylaşır.
    `/validate` artık gauge YAZMAZ (yalnızca `run_drift_monitor` yazar);
    bağımsız referans yoksa gauge'lara hiç dokunulmaz (eski davranış: `0`
    yazardı, bu "gerçekten tutarsız" ile "hiç bakılamadı"yı ayırt edilemez
    kılıyordu). `/validate` yanıtındaki her referansa `self` bayrağı eklendi
    (şeffaflık; totolojik girdi görünür kılınır, resmi verdict'i etkilemez).
  - **MEDIUM-2:** İş Yatırım'ın kapanış-zamanı damgası (`market_close_time`)
    bugüne ait bir bar için seans devam ederken GELECEK bir zaman
    üretebiliyordu (`data_age_seconds` negatif görünürdü). Damga artık
    `min(kapanış, şimdi)` ile sınırlanır; `_with_live_state` ayrıca
    `data_age_seconds`'ı savunma amaçlı `0`'da klempler.
  - **MEDIUM-3:** Guard'ın düşürdüğü (bayat bar / damgasız) semboller artık
    `symbol_circuit`'e HATA olarak yazılmıyor — "bugün işlem görmedi" bir
    politika kararıdır, provider'ın veri veremediği anlamına gelmez; aksi
    halde sembol 3 turda 300sn'lik bir devre-dışına düşüp geç işlem gören
    hisseleri gereksiz yere atlıyordu.
  - **MEDIUM-4:** `docker-compose.yml`'de updater metrik portu artık
    `0.0.0.0` yerine VM130'un iç arayüzüne (`10.10.10.130:8001:8001`)
    bağlanıyor (Docker port publish, host firewall INPUT kurallarını
    atlayabildiğinden tüm arayüzlere açmak firewall-dışı bir yüzeydi).
  - **LOW:** `/all` ETag'i artık `data_age_seconds`/`stale` gibi okuma anında
    sürekli değişen alanlardan değil yalnızca "gerçek" veriden türetiliyor
    (aksi halde fiyat hiç değişmese bile her cache-yenilemesinde ETag
    değişip 304 yolu hiç tetiklenmezdi); `market.is_stale_bar()` naive
    (tzinfo'suz) `exchange_time`'ı artık sunucunun yerel saat diliminden
    bağımsız her zaman UTC sayıyor.

- **Veri-doğruluk hardening — çapraz-göz bulgusu turu (3 HIGH + 4 MEDIUM + 3 LOW):**
  - **HIGH-4 (`bar_time`/`exchange_time` ayrımı):** `exchange_time` iki farklı
    anlamda kullanılıyordu — "yaş hesabı için gerçek işlem anı" ve "bayat-bar
    guard'ı için barın ait olduğu gün" — bu ikisi TradingView için ÇELİŞİYORDU
    (`time` kolonu bar-açılış anıdır, sabah 10:00'da set olur ve gün boyu
    değişmez; bunu `exchange_time`'a koymak öğleden sonra her TV quote'unu
    yanlışlıkla `stale:true` gösterirdi). `Quote`'a ayrı bir `bar_time` alanı
    eklendi: `exchange_time` SADECE `data_age_seconds` hesabında kullanılır;
    `bar_time` bayat-bar guard'ında (`is_stale_bar`) kullanılır. TradingView ve
    İş Yatırım artık `exchange_time`'ı hiç doldurmaz (yalnızca `bar_time`);
    `yahoo` (yfinance) da DataFrame index'inden `bar_time` türetir;
    `yahoo_chart` için `regularMarketTime` ikisi için de aynı değerdir (bu
    kaynak gerçek işlem anını sağlar). **Kritik tamamlayıcı düzeltme:**
    `aggregator.py`'nin bayat-bar guard'ı (Block-1) ve damgasız-kaynak
    guard'ı (Block-2) ile `pipeline.py`'nin `_pick_reference`'ı bu ayrımdan
    SONRA da hâlâ yalnızca `exchange_time`'a bakıyordu — TradingView/İş
    Yatırım artık `exchange_time`'ı hiç doldurmadığı için Block-1 bu iki
    kaynak için kalıcı olarak devre dışı kalıyor, Block-2 ise TERSİNE aşırı
    agresifleşip bar_time taze olsa bile seans boyunca HER TURDA quote'larını
    düşürüyordu (feed seans içinde fiilen tek kaynağa — `yahoo_chart` —
    inerdi, bu da yeni eklenen MEDIUM-7 cooldown'unu kalıcı biçimde tetikler
    ve yapısal bir arızayı sonsuza kadar sürdürürdü). Üç konum da artık
    `bar_time or exchange_time` (guard) / her iki alanın da `None` olması
    (damgasız-kaynak guard'ı) sözleşmesini kullanır.
  - **MEDIUM-5 (referans yolu asimetrisi):** Aggregator seans içinde hiçbir
    zaman damgası (`bar_time` VE `exchange_time`) taşımayan bir quote'u
    güvenilmez sayıp düşürüyordu, ama doğrulama referans seçicisi
    (`_pick_reference`) aynı quote'u referans olarak KABUL edebiliyordu
    (asimetri — feed'in attığı veri arka kapıdan doğrulamaya geri girebilirdi).
    `_pick_reference` artık seans içinde damgasız adayları aynı şekilde
    reddeder (`bist_validate_no_reference_total{reason="no_timestamp"}`).
  - **MEDIUM-6 (kapalı-piyasa guard körlüğü):** Bayat-bar guard'ı yalnızca
    "seans açıkken bugüne ait değilse" kuralıyla çalışıyordu; piyasa kapalıyken
    devre dışıydı — donmuş/eski bir kaynak (örn. 2 hafta önceki bir bar) hiç
    yakalanmadan "kapanış fiyatı" gibi geçebilirdi. `market.last_trading_day()`
    eklendi; `is_stale_bar` artık kapalı piyasada da barın gününü SON İŞLEM
    GÜNÜYLE (hafta sonu/tatil dahil, geriye yuvarlanmış) karşılaştırır.
  - **MEDIUM-7 (guard-drop provider-seviyesi cooldown/backpressure):**
    Guard'ın düşürdüğü semboller `symbol_circuit`'e hata yazmadığı için
    (MEDIUM-3, bilinçli) yapısal olarak seans içinde hiç taze veri
    üretemeyen bir kaynağın üzerinde HİÇBİR fren kalmamıştı — her turda
    (sembol başına 1 istek) sonsuza kadar sorgulanmaya devam edebilirdi. Bir
    kaynak ardışık `GUARD_COOLDOWN_FAIL_THRESHOLD` (varsayılan 3) turda
    isteğinin TAMAMINI guard'a kaybederse `GUARD_COOLDOWN_SECONDS` (varsayılan
    1800sn) süreyle provider-seviyesinde cooldown'a alınır
    (`bist_provider_guard_cooldown{provider}` gauge); en az bir quote
    geçtiğinde ardışık sayaç sıfırlanır, cooldown bitince yeniden denenir.
  - **LOW-3 (`/validate` sayaç şişmesi):** `bist_validate_no_reference_total`
    `_pick_reference` içinde koşulsuz artıyordu; insan-teşhis (`/validate`,
    operatör poll'u) endpoint'i aynı çekirdeği kullandığı için her manuel
    sorgu, arka plan drift-monitörünün gerçek "referans bulunamadı" oranını
    bozuyordu. `_pick_reference`/`compare_against_references`'a
    `record_metrics` parametresi eklendi; `/validate` artık `False` geçer.

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
