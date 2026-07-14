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

### Hukuki / Uyum
- **TradingView varsayılan zincirden çıkarıldı (patron kararı, ToS §3):**
  TradingView Kullanım Şartları veriyi yalnızca ekranda-gösterim (display-only)
  ile sınırlar; otomatik işlem, algoritmik karar-verme, fiyat referanslama,
  order verification, risk-yönetim programları kullanımını ismen yasaklar.
  Abonelik satın almak bunu çözmez (satılan şey display-use lisansıdır).
  BistEye'in Faz-2'deki client-side stop-loss'u bu tanımın tam ortasına
  düşüyor; servis henüz bu veriyi tüketmese de kaynak şimdiden temizlendi.
  - `PROVIDERS` ve `VALIDATE_PROVIDERS` varsayılanlarından `tradingview`
    çıkarıldı: `["yahoo_chart", "isyatirim"]`. Provider sınıfı **silinmedi**
    (`app/providers/tradingview.py`), env ile geri eklenebilir ama YALNIZCA
    insan-okur dashboard/teşhis amacıyla — modül docstring'inde, README'de ve
    `.env.example`'da net uyarı var: çıktısı bot karar-yoluna
    bağlanmamalıdır.
  - **Bilinen ve kabul edilen sonuçlar** (Faz-2 lisanslı realtime kaynak
    kararına bağlı açık madde — bkz. README "Bilinen açık maddeler"):
    seans içinde fiilen tek kaynak `yahoo_chart` kalıyor (İş Yatırım EOD-guard'ı
    yüzünden elenir; `yahoo_chart` düşerse feed durur, `/ready` not-ready
    döner — donma değil, gözlemlenebilir bir alarm); çapraz doğrulama seans
    içinde bağımsız referans bulamadığı için fail-quiet dönüyor
    (`bist_validate_no_reference_total{reason="stale"}` artar) — bu, HIGH-2
    fix'inin kazanımının bilinçli olarak geri alınması demektir.
  - Not: Yahoo (`yahoo_chart`) da benzer bir ToS gri alanında; bu ayrı bir
    karar (lisans süreci netleşene kadar servis "dashboard-only" konumda).

### Düzeltildi (devam) — cooldown/last_trading_day yeniden tasarımı (1 CRITICAL + 3 HIGH + 3 MEDIUM + LOW'lar)
- **CRITICAL-1 (`last_trading_day` pre-open'da bugünü döndürüyordu):**
  `market.last_trading_day()` hafta içi sabah (açılış öncesi, örn. 09:00)
  yanlışlıkla "bugün"ü dönüyordu — ama bugünün henüz hiçbir barı yoktu (seans
  başlamadı). Sonuç: `is_stale_bar`'ın kapalı-piyasa dalı tüm kaynakların
  dünkü/Cuma barlarını "beklenenden eski" sanıp elerdi; updater'ın warm-up
  turu (piyasa kapalıyken de çalışır) ve on-demand cache-miss istekleri TÜM
  kaynakları guard'a düşürüp seans açılışının ilk ~30 dakikasında feed'i
  karartabilirdi (uçtan uca simülasyonla kanıtlandı). Fix: bugünün kapanışı
  henüz gelmediyse (`now.time() < close_t`) önce bir gün geriye inilir, sonra
  hafta sonu/tatil yuvarlaması yapılır.
- **HIGH-1 (cooldown sayacı BATCH değil TUR bazında):** bir güncelleme turu
  ~13 `fetch_quotes()` (batch) çağrısı üretiyordu; batch-bazlı sayımda "N tur
  üst üste" sigortası saniyeler içinde patlıyordu. `Aggregator.begin_cycle()`/
  `end_cycle()` eklendi — bir TUR'un tüm batch'leri artık TEK bir "ardışık
  tam-düşme" sayılır (updater `_update_once()` bunları sarmalar). Açılış
  toleransı da eklendi: `GUARD_OPEN_GRACE_SECONDS` (varsayılan 300sn) içindeki
  düşüşler sayaca yazılmaz (kaynaklar açılışın ilk saniyelerinde henüz dünkü
  barlarını güncellemiyor olabilir).
- **HIGH-2 (on-demand istek cooldown tetikliyordu):** tek-sembol on-demand
  sorgular (`/quote/{symbol}` cache-miss) bir sembol guard'a düşünce koca bir
  kaynağı TÜM semboller için cooldown'a sokabiliyordu. `fetch_quotes()`'a
  `count_toward_cooldown: bool = False` (keyword-only) parametresi eklendi;
  yalnızca updater'ın yapılandırılmış döngüsü `True` geçer. Bu, aynı zamanda
  **MEDIUM-3**'ü de çözer: Redis'siz (tek-instance) dağıtımda bile on-demand
  yol asla cooldown state'ini YAZMAZ (yalnızca mevcut bir cooldown'a saygı
  gösterir) — `bist_provider_guard_cooldown` gauge'u yalnızca updater'ın
  döngüsünden beslenir.
- **HIGH-3 (eksik tatil listesi → tüm gün kitlesel düşüş + cooldown):**
  `MARKET_HOLIDAYS` listesinde olmayan resmi bir tatilde tüm kaynaklar aynı
  anda "bayat" görünüp tüm gün düşer ve cooldown'a girerdi. Fail-open emniyet
  supabı eklendi: **birden fazla bağımsız kaynak** aynı turda **tüm
  sembolleri** guard'la düşürürse (tek kaynak değil — bu ayrım yapılamaz,
  güvenli varsayılan cooldown yoludur) guard o tur için devre dışı bırakılır,
  elde bulunan veri `stale=true` ile geçirilir, hiçbir kaynak cooldown'a
  girmez, `bist_guard_fail_open_total` sayacı artar + CRITICAL log basılır.
- **MEDIUM-1 (test sırası bağımlılığı):** `bist_provider_guard_cooldown`
  gauge'u process-genelinde paylaşıldığı için bir testin bıraktığı durum
  izolasyonda (`pytest -k`) sonraki teste sızıyordu. `conftest.py`'de autouse
  fixture eklendi (her testten önce/sonra sıfırlar).
- **MEDIUM-2 (streak'te zaman aşımı yoktu):** sabah erken saatte birikmiş bir
  sayaç saatlerce durup öğleden sonraki TEK kötü turla cooldown'a
  dönüşebiliyordu. `GUARD_DROP_STREAK_MAX_AGE_SECONDS` (varsayılan 900sn)
  eklendi — son artıştan bu süre sonra sayaç geçersiz sayılır.
- **LOW'lar:** guard log'u piyasa kapalıyken de "seans açık" diyordu (artık
  duruma göre değişir); `market_close_time()` docstring'i güncellendi
  (`exchange_time` değil `bar_time` — HIGH-4 ile tutarlı); `is_stale_bar`'ın
  yanıltıcı `exchange_time` parametre adı `bar_or_exchange_time` oldu.
- **Yapısal iyileştirme:** `Provider.intraday_capable` (varsayılan `True`)
  eklendi; İş Yatırım (`IsYatirimProvider`, yalnızca günlük EOD verir)
  `False` yapıldı — seans **açıkken** bu kaynak artık **hiç sorgulanmaz**
  (guard zaten her turda düşüreceği için sorgulamak yapısal olarak bosuna bir
  istekti). Bu, cooldown mekanizmasının ihtiyacını da azaltır.

### Düzeltildi (devam 2) — cooldown/fail-open tek-intraday-kaynak dünyasına göre yeniden tasarım (3 HIGH + 3 MEDIUM + 2 LOW)
- **Kök neden (üçünün de ortak nedeni):** TradingView çıkarıldı + İş Yatırım
  `intraday_capable=False` olduğu için seans içinde **TEK intraday kaynak**
  (`yahoo_chart`) kaldı. Cooldown ve fail-open bir önceki turda "birden fazla
  kaynak var" dünyası için tasarlanmıştı; o dünya artık yok.
- **HIGH-1 (fail-open seans içinde yapısal olarak tetiklenemiyordu):** eski
  koşul (`>=2 kaynak sorgulandı`) seans içinde asla sağlanamazdı (sözlüğe
  yalnız `yahoo_chart` girer) — bilinmeyen bir tatilde tüm gün karanlık +
  `bist_guard_fail_open_total` hiç artmazdı (alarm bile yok). Fail-open eşiği
  artık **kaynak sayısına değil batch büyüklüğüne** bağlı:
  `len(symbols) >= GUARD_FAIL_OPEN_MIN_SYMBOLS` (varsayılan 20). Tek kaynakta
  da çalışır, on-demand tek-sembol yolunda (`len(symbols)=1`) tetiklenmez.
- **HIGH-2 (cooldown tek intraday kaynağı susturup feed'i kendi kendine
  kesiyordu):** cooldown'un amacı "bozuk kaynağı dövme, diğerleri servis
  etsin"di — diğerleri yoksa bu artık kendi kendine sabotajdı. İki katman
  eklendi: **(a)** cooldown yalnız `>=2` intraday-capable kaynak varsa fiilen
  uygulanır (yoksa guard yine bayat veriyi eler, sayaç izlenebilirlik için
  birikir, ama provider her tur yeniden denenir); **(b)** cooldown
  uygulandığında bile **half-open**: tur başına 1 "prob" denemesi yapılır,
  başarılı olursa cooldown **anında** kalkar (tam süreyi beklemez).
- **HIGH-3 (`GUARD_OPEN_GRACE_SECONDS=300` < ~15 dk besleme gecikmesi):**
  veri ~15 dk gecikmeli; açılışta kaynağın bugüne ait damga üretmesi bu
  gecikme + tampon kadar sürebilir — 300sn'lik grace, açılıştan ~10 dk sonra
  biterek her işlem gününün ilk yarım saatini köreltiyordu. Varsayılan
  **1200sn (20 dk)** yapıldı; HIGH-2(a) fix'i bunu zaten ölümcül olmaktan
  çıkarır ama iki koruma birlikte durur.
- **MEDIUM-1 (fail-open'ın `stale=True` işareti tüketiciye ulaşmıyordu):**
  `main.py::_with_live_state` bu bayrağı `model_copy` ile koşulsuz eziyordu
  (üstelik yalnız `market_state=="OPEN"` iken `True` olabiliyordu) —
  README/CHANGELOG'un "veri stale işaretiyle geçirilir" vaadi tutulmuyordu.
  Artık `stale = q.stale or (yaş-tabanlı hesap)` — provider/aggregator'ın
  koyduğu bayrak korunur.
- **MEDIUM-2 (`end_cycle()` iptal-güvenli değildi):** `updater_cycle_timeout`
  aşımında `CancelledError` enjekte edilir; `try/finally` olmadan
  `end_cycle()` hiç çalışmaz, biriken guard-drop bilgisi sessizce kaybolurdu.
  `updater._update_once()`'a `try/finally` eklendi.
- **MEDIUM-3 (testler üretim varsayılan zincirini hiç koşmuyordu):** bu
  yüzden HIGH-1 kaçmıştı. Gerçek `Aggregator()` + gerçek `IsYatirimProvider`
  (gerçek `intraday_capable=False`) + `PROVIDERS=[yahoo_chart, isyatirim]` +
  seans açık ile uçtan uca entegrasyon testi eklendi.
- **LOW-1:** fail-open'ın erken `return result`'ı aynı batch'teki İLGİSİZ
  (guard'la değil sanity ile reddedilen) sembollerin kaçış (escape) fırsatını
  sessizce atlıyordu (fail-open eşiği gevşeyince canlı bir bug olurdu) —
  artık escape bloğu fail-open sonrası da normal çalışır.
- **LOW-2:** bir turda herhangi bir batch fail-open'a girerse, aynı turdaki
  BAŞKA batch'lerin "tam düştü" bilgisi de birleştirilmiyordu (karma turda
  streak yine birikebiliyordu) — artık fail-open'ın olduğu tur, TÜM
  provider'lar için veto edilir (streak'e hiçbir şey yazılmaz).

### Düzeltildi (devam 3) — birleşik iki-review turu: 1 CRITICAL + 3 HIGH + 3 MEDIUM + LOW
- **CRITICAL-1 (sanity-escape tek-kaynak dünyasında yapısal olarak ölüydü):**
  `_try_escape` `len(candidates) >= 2` şartına bağlıydı — seans içinde TEK
  intraday kaynak sorgulandığı için bu şart asla sağlanamaz, escape ölü
  kalırdı. Bedelsiz/split sonrası bir sembol **kalıcı olarak** sanity'de
  kilitlenirdi (reddedilen quote store'a hiç yazılmadığı için restart bile
  kurtarmazdı). İki bağımsız kaçış yolu eklendi (bkz. README "Sanity-check
  kaçış yolları"): **(a)** kaynağın kendi `previous_close`'u yeni fiyatla VE
  bizim bildiğimiz önceki fiyattan farklılığıyla tutarlıysa (`reason=
  consistency`) tek turda kabul; **(b)** aynı kaynak aynı aykırı fiyatı
  `SANITY_ESCAPE_PERSIST_ROUNDS` (vars. 3) ardışık TURDA tekrarlarsa
  (`reason=persistence`) kabul. Eski çoklu-kaynak uzlaşısı (`reason=
  corroboration`) Faz-2 için korunur. `bist_sanity_escapes_total` artık
  `{provider,reason}` etiketli + escape kabulünde CRITICAL log.
- **HIGH-1 (fail-open bayatlık dedektörünü körleştiriyordu):** `store.
  fresh_ratio()`/`is_stale()` yalnız `updated_at`'e (cache YAZIM anı, GERÇEK
  veri yaşı değil) bakıyordu — fail-open `stale=True` koysa bile `/ready`
  "sağlıklı" görünüyor, `BistDataStale`/`BistApiDegraded` gibi hiçbir alarm
  çalmıyordu. `stale=True` quote artık TAZE SAYILMIYOR. Ayrıca: bir provider'ın
  `ask`'inin TAMAMI guard'la (bayat-bar/damgasız) düşerse gapfill dalında artık
  `breaker.record_success()` YAZILMIYOR (breaker/`bist_provider_up` durumu
  değiştirilmiyor) — kitlesel guard-düşmesi provider'ı yanlışlıkla "sağlıklı"
  göstermesin diye. Aynı körlüğün kardeşi: `store.oldest_update_age()` (ve
  `bist_oldest_quote_age_seconds` metriği, `/ready`'nin
  `oldest_quote_age_seconds` alanı) de yalnız `updated_at`'e bakıyordu —
  `stale=True` quote'larda artık yaş GERÇEK veri zamanından (`exchange_time or
  bar_time or updated_at` önceliğiyle) hesaplanıyor; fail-open sırasında
  saatler önceki bir bar artık saniyelere küçültülmüyor.
- **HIGH-2 (okuma yolu `bar_time`'a hiç bakmıyordu):** guard yalnız FETCH
  anında çalışıyordu; `main.py::_with_live_state` okuma anında yalnız
  `exchange_time`/`updated_at` yaşına bakıyordu — açılışın ilk dakikalarında
  (warm-up piyasa kapalıyken doğru geçen, ama okuma piyasa açılınca yapılan)
  bir Cuma kapanışı "taze" servis edilebilirdi. Artık okuma da `bar_time`'ı
  (varsa, yoksa `exchange_time`'ı) `is_stale_bar` ile yeniden kontrol eder.
- **HIGH-3 (fail-open BATCH değil TUR düzeyinde değerlendirilmeliydi):** eski
  tasarım `watchlist/BATCH_SIZE` bölümünden kalan küçük son batch'i (örn. 525
  sembol / 40'lık batch = kalan 5) sistemik bir kesinti sırasında bile hiçbir
  zaman eşiği aşamayacağı için korumasız bırakıyordu (korunma
  `len(watchlist) % BATCH_SIZE`'a bağlıydı — deterministik olmayan bir
  emniyet); ayrıca `count_toward_cooldown`'a bakmadığı için on-demand
  `/quotes?symbols=` istekleri de (≥eşik cache-miss sembolle) tetikleyebiliyor,
  `bist_guard_fail_open_total` sayacını kirletebiliyordu. Karar artık
  `end_cycle()`'da, TUR düzeyinde verilir: "bu turda TÜM watchlist sıfır taze
  quote üretti mi?" — evetse, TURUN TOPLAMINDA biriken guard-düşmüş adaylar
  (küçük kalıntı batch dahil) `stale=true` ile kurtarılır. Yalnızca updater'ın
  yapılandırılmış döngüsü (`begin_cycle`/`end_cycle`) bu değerlendirmeye girer
  — on-demand yol yapısal olarak asla giremez.
- **MEDIUM-1 (fail-open sanity-check'i atlıyordu):** guard'ın düşürdüğü aday
  doğrudan commit ediliyordu — absürt bir fiyat + bayat damga birlikte
  `previous`'i kalıcı olarak zehirleyebilirdi (CRITICAL-1 ile birleşince zehir
  KALICI olurdu). Fail-open adayları artık `end_cycle()`'da sanity-check'ten
  de geçirilir.
- **MEDIUM-2 (cAdvisor `0.0.0.0:8081`'de):** 8001 için daha önce düzeltilen
  aynı public-expose açığı (üstelik `privileged: true` + `/:/rootfs:ro` +
  auth'suz) — `docker-compose.yml` artık yalnız VM130'un internal IP'sine
  (`10.10.10.130:8081:8080`) bağlanıyor.
- **MEDIUM-3 (TradingView hâlâ sembol evrenini belirliyor — kabul edilen
  kalıntı risk):** `SYMBOL_UNIVERSE_REFRESH_ENABLED` **PATRON KARARIYLA
  KALDI** (ücretsiz, çalışıyor) — kod değişikliği yok, yalnızca README/
  `.env.example`'a açıkça belgelendi: fiyat zinciri TradingView'den tamamen
  arındırıldı, sembol evreni (dizin) hâlâ TradingView'den gelir.
- **LOW (test yanılsaması):** eski escape testleri (`_two_agreeing_providers`)
  yalnızca artık üretimde var olmayan iki-intraday-kaynak dünyasını
  kapsıyordu; gerçek üretim zinciriyle (tek `yahoo_chart`, gerçek
  `IsYatirimProvider`) escape + fail-open + `/ready`/`store.is_stale()`/metrik
  entegrasyon testleri eklendi.

### Düzeltildi (devam 4) — açılış regresyonu + escape güvenlik açığı (2 HIGH + 1 MEDIUM + LOW'lar)
- **HIGH-1 (REGRESYON — her seans açılışında `/ready` 503 + günlük critical
  alarm):** veri ~15 dk (900 sn) gecikmeli olduğu için açılışın ilk ~15
  dakikasında kaynağın `regularMarketTime`'ı hâlâ dünkü güne ait olabilir;
  guard TÜM sembolleri düşürür, TUR-düzeyi fail-open devreye girer, hepsi
  `stale=true` commit edilir. `store.is_stale()`'in KENDİ açılış toleransı
  (`STALENESS_SECONDS`, vars. 300 sn) bu veri gecikmesinden (900 sn) kısa
  olduğu için bu, **HER İŞLEM GÜNÜ** açılıştan ~5-16 dk sonra `/ready` 503 +
  critical Telegram alarmı üretiyordu (rutin bir durum için) — alarm
  yorgunluğu, gerçek fail-open (bilinmeyen tatil) rutin gürültüden ayırt
  edilemez hâle geliyordu. İki fix birlikte: **(a)** `store.is_stale()`'in
  açılış toleransı artık `GUARD_OPEN_GRACE_SECONDS` (vars. 1200 sn) ile
  hizalı — açılışın ilk 20 dk'sında `/ready` 200 döner (servis hazır, veri
  henüz gecikmeli, BEKLENEN); **(b)** `end_cycle()`'da açılış toleransı
  penceresindeyken fail-open quote'ları yine döndürülür (veri sürekliliği,
  `stale=true`) ama `bist_guard_fail_open_total` sayacı artırılmaz ve
  CRITICAL log basılmaz (takvim-hatası alarmının sinyal değeri korunur;
  rutin açılış gürültü üretmez). Pencere dışında eskisi gibi (sayaç +
  CRITICAL).
- **HIGH-2 (GÜVENLİK — sanity-escape'in `previous_close` iç-tutarlılık yolu
  tek turda, teyitsiz deliniyordu):** kanıtlanmış açık — kaynak THYAO için
  yanlışlıkla bir USD satırı dönerse (`price=2.50, previous_close=2.40`) bu
  iki alan BİRLİKTE kaydığı için hem kendi içinde tutarlı GÖRÜNÜYOR hem bizim
  TRY fiyatımızdan "önemli ölçüde farklı" GÖRÜNÜYORDU — guard'ın var oluş
  sebebi olan hata sınıfının (yanlış-birim/yanlış-enstrüman) tam kendisi
  escape'i açıyordu; tek bir bozuk turda (60 sn) client-side stop-loss'u
  besleyen feed'e sessizce sızabilirdi. Bu yol **kaldırıldı** — yalnız ısrar
  teyidi (3 ardışık tur, ~3 dk) kaldı: gerçek bir kurumsal işlem gün
  öncesinden bilinir (3 dk gecikme maddi değil), geçici/hatalı bir payload
  ise 3 dk boyunca aynı değeri tekrarlamaz.
- **MEDIUM-1 + LOW-1 (iptal/kısmi turda fail-open metrik/log/commit
  tutarsızlığı):** `end_cycle()` `try/finally` içinde çalıştığı için
  `updater_cycle_timeout` aşımında (`CancelledError`) metrik+CRITICAL log
  basılıyor ama `CancelledError` hemen sonra yukarı yayıldığı için commit
  bloğuna hiç ulaşılmıyordu (log "kurtarıldı" derken veri store'a hiç
  yazılmıyordu). Ayrıca kısmi bir turda (örn. 14 batch'ten yalnız 2'si
  koşabildiyse) "hiçbir sembol taze değil" ölçüsü trivially doğru olup
  fail-open'i yanlış değerlendirebiliyordu; aynı sorun kapanma sırasındaki
  erken durdurmada da (`BackgroundUpdater.stop()`) vardı. `end_cycle(aborted=
  bool)` parametresi eklendi: iptal/timeout veya erken durdurma ile YARIM
  kalan bir turda fail-open (VE provider guard-drop streak) HİÇ
  değerlendirilmez — ne metrik ne log ne commit; bir sonraki (tam) tur
  normal değerlendirmeye kaldığı yerden devam eder.

337 → 343 test (yeni: açılış-penceresi + aborted-cycle + USD-satırı senaryosu
uçtan uca kilit testleri), ruff + ruff format + mypy temiz.

### Düzeltildi (devam 5) — wedge/donmuş updater tespiti + doküman kısıtları (1 MEDIUM + LOW'lar)
- **MEDIUM (servisin bilinen ANA arızasının dedektörü açılışta kördü):**
  açılış toleransı penceresinde (`GUARD_OPEN_GRACE_SECONDS`) `store.
  is_stale()` koşulsuz `False` döndüğü için "veri gecikmeli ama boru hattı
  sağlıklı" ile "boru hattı **ÖLÜ**" (donmuş/wedge updater — servisin bilinen
  ana arızası, `py-spy` watcher tam bunun için kuruldu) ayırt edilemiyordu;
  ikisi de bu pencerede `fresh_ratio=0` üretiyordu. Sonuç: updater Cuma'dan
  beri ölüyse bile açılıştan itibaren **20 dakika boyunca** `/ready` yanlış
  `200` dönüyordu (tespit gecikmesi 5 dk'dan 20 dk'ya çıkmıştı). Fix:
  `/ready` artık ayrıca `last_update_age_seconds`'a bakar — **market
  açıkken** bu değer `2 × UPDATE_INTERVAL`'i (vars. 120 sn) aşarsa (sağlıklı
  bir updater her `UPDATE_INTERVAL`'de store'a yazar) boru hattı
  gerçekten ölmüştür ve `ready=false` **anında** döner, açılış toleransı
  penceresinin dolmasını beklemez. Market kapalıyken bu kontrol devre
  dışıdır (updater bilerek çalışmaz — `UPDATE_WHEN_CLOSED=false` varsayılan
  — eski veri bir arıza değildir; bu kontrol olmasaydı her akşam/hafta sonu
  yanlışlıkla 503 üretirdi).
- **LOW (dokümantasyon):** `GUARD_OPEN_GRACE_SECONDS` (vars. 1200 sn) ile
  belgelenen veri gecikmesi (~900 sn) arasında yalnız ~300 sn marj olduğu
  `.env.example` + README'ye açıkça yazıldı — kaynak gecikmesi bir gün 20
  dk'yı aşarsa bu değerin orantılı büyütülmesi gerektiği (aksi hâlde günlük
  yanlış CRITICAL + 503 geri döner) artık kısıt olarak belgeleniyor.
- **LOW (kabul edilmiş takas, dokümantasyon):** ısrar teyidinin (3 ardışık
  tur) mutlak bir garanti olmadığı — kaynak bir ticker'ı **kalıcı olarak**
  yanlış bir satıra eşlerse (örn. sürekli USD satırı) bu hatalı fiyat da 3
  tur ısrar edip escape ile kabul edilir; tek kaynaklı bir dünyada
  kalıcı-yanlış-eşleme ile gerçek kurumsal işlem formen ayırt edilemez —
  README'ye bilinçli kabul edilmiş bir risk olarak belgelendi.

345 test yeşil (bir önceki turda 343), ruff + ruff format + mypy temiz.

### Düzeltildi (devam 6) — kayıp prod compose overlay'i tek kaynağa konsolide edildi
- **`cadvisor.mem_limit`** `128m` → `256m` (v0.54.1'in gerçek RSS'i VM130'da
  ~125MB, önceki tavana yapışıktı; kernel cgroup OOM'u ~8 saatte bir sessizce
  öldürüp restart ediyordu — Docker `OOMKilled=false` yanlış-negatif verdiği
  için görünmüyordu).
- **Zaman bombası bulgusu:** VM130'da (`/opt/bist-canli-api`) repo'da
  **izlenmeyen** bir `docker-compose.prod.yml` overlay'i tüm prod
  bellek/CPU limitlerini, log rotation'ı ve panel'in `80:80` publish'ini
  taşıyordu. 8 Tem'de `redis` bu overlay verilmeden recreate edildi ve `320m`
  cgroup tavanını kaybederek sınırsız çalışır hale geldi — overlay'e
  bağımlılığın somut başarısızlık modu.
- **Fix (bu PR):** tüm limitler `docker-compose.yml`'e taşındı, overlay
  kaldırıldı (kod dışı, VM130-lokal dosya — deploy adımı ayrı iş paketi):
  `redis` mem 320m; `api` cpu 1.0/mem 512m; `updater` cpu 1.5/mem 1024m;
  `bot` cpu 0.5/mem 256m; `panel` mem 64m + ek `80:80` port (mevcut `8080:80`
  ile birlikte — compose liste alanları `-f` dosyaları arasında birleşir,
  ikisi de VM130'da fiilen aktifti); tüm servislerde log rotation
  (`json-file`, `max-size: 10m`, `max-file: 3`). Stil: `deploy.resources.limits`
  değil üst-seviye `mem_limit`/`cpus` (cadvisor'ün zaten kullandığı stille
  tutarlı — Swarm'a hiç girilmiyor).
- **Bulgu (PM kararına bırakıldı, bu PR'da bilinçli konsolide edildi):** `bot`
  servisinin `restart` politikası overlay'de `unless-stopped` idi; taban
  tasarım `on-failure` idi (`TELEGRAM_ENABLED=false` iken exit(0)'da durup
  restart döngüsü oluşturmasın diye). **VM130 için** davranış değişmedi —
  canlı teşhis: `TELEGRAM_ENABLED=true`, bot `RestartCount=0`, restart-loop
  yok. **Ama base dosya artık tek kaynak** olduğundan bu, `TELEGRAM_ENABLED=
  false` olan her ortamı (dev, CI, taze klon) etkiler: bot exit(0) yapar ve
  `unless-stopped` bunu backoff'lu restart-loop'a sokar. Compose-seviyesinde
  `profiles: [telegram]` **kullanılmadı** — bu, `docker compose up -d`'nin
  bot'u sessizce atlamasına yol açar (bayrak unutulursa servis prod'dan
  kaybolur), tam bu PR'ın yok ettiği hata sınıfı. Kalıcı çözüm uygulama
  katmanında (devre dışıyken exit yerine idle) — ayrı bir iş.
- `.gitignore`'a `.api-keys` eklendi (yalnız `.env` kapsanıyordu — bir
  `git add -A` secret'ı repo'ya sokabilirdi).

345 test yeşil (değişmedi — bu iş yalnız compose/CI-dışı dosyalara dokundu),
ruff + mypy temiz.

### Düzeltildi (devam 7) — review turu (1 MEDIUM + 1 LOW, deploy öncesi)
- **MEDIUM-1:** `cadvisor` servisinde log rotation eksikti (`docker compose
  config` 6 servisten yalnız 5'inde `json-file` gösteriyordu) — devam 6'daki
  "tüm servislerde log rotation" iddiasıyla çelişiyordu. Üstelik rotation'sız
  kalan tek container, restart-loop geçmişi olan (~8 saatte bir kernel cgroup
  OOM) cadvisor'du. Diğer 5 servisle aynı `json-file` / `max-size: 10m` /
  `max-file: 3` eklendi.
- **LOW-2:** `.gitignore`'da `.env` yalnız tam-eşleşmeydi — `.env.prod`,
  `.env.local`, `.env.bak` kapsanmıyordu. `.env*` + `!.env.example` istisnasına
  genişletildi (`.api-keys` için yazılan aynı gerekçe: `git add -A` secret
  sızdırabilir).
- **Kabul edilen risk (MEDIUM-3, bu PR'da düzeltilmedi):** VM130'daki limit
  toplamı (redis 320 + api 512 + updater 1024 + bot 256 + panel 64 + cadvisor
  256 = 2432 MiB) gerçek VM RAM'inin (2907 MB, bugün canlı doğrulandı —
  dokümantasyondaki "6GB" yanlış) ~%84'ü. Limitler cap'tir, reservation değil;
  fiili toplam kullanım ~460 MB olduğundan bugün risk yok, tek-servis
  sızıntısı hâlâ kendi cgroup'unda yakalanır (diğer servisleri boğmaz). RAM
  büyütme veya limit küçültme ayrı bir iş.

345 test yeşil (değişmedi), ruff + mypy temiz.

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
