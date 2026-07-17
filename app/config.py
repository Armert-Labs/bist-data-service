"""Uygulama ayarlari (12-factor: ortam degiskenlerinden okunur).

Ek bagimlilik (pydantic-settings) gerektirmemesi icin sade os.environ kullanilir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit


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


def _build_redis_url(url: str, password: str) -> str:
    """HIGH-1 (review, PR#19): docker-compose.yml REDIS_PASSWORD'u artik
    REDIS_URL'e GOMMEDEN (kimlik-bilgisiz `redis://redis:6379/0`) ayri bir
    env olarak tasir; parola burada, TEK NOKTADA, percent-encode edilerek
    URL'e eklenir. `openssl rand -base64 32` uretilen parolalarin ~%50'si
    `/` (bazen `+`, `=`) icerir -- bu karakterler URL userinfo alaninda
    ayrilmis (reserved) oldugundan ham haliyle gomulurse `urlparse`/
    `redis.from_url` (ve limits/slowapi rate-limiter storage_uri'si, o da
    ayni `redis.from_url`'u cagirir) `ValueError: Port could not be cast`
    ile patlar -- redis sunucusu REDISCLI_AUTH kullandigi icin saglikli
    kalir ama api/updater/bot crash-loop'a girer (sessiz degil, ama
    kafa karistirici). quote(safe='') ANY parola degeri icin (yalniz `/`
    degil `@`, `:`, `#`, `?`, `%` de dahil) guvenlidir; redis-py `parse_url`
    zaten `unquote` uyguladigi icin sunucu tarafinda parola degismeden
    geri cikar (dogrulandi). url zaten kimlik bilgisi tasiyorsa (elle
    `REDIS_URL=redis://:pw@host:port/db` -- orn. compose-disi/harici
    yonetilen Redis) DOKUNULMAZ: REDIS_PASSWORD yalniz compose'un kendi
    ayirdigi senaryoda dolu olur."""
    if not url or not password:
        return url
    parsed = urlsplit(url)
    if parsed.password or parsed.username:
        return url
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    netloc = f":{quote(password, safe='')}@{netloc}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _enforce_watchdog_floor(cfg: Settings) -> None:
    """MEDIUM-1 (wedge-review, PR#20 ek): watchdog_timeout guvenli bir
    invariant korumali -- saglikli TEK bir turun en kotu olcekte alabilecegi
    sure (guard + cycle + guard) + UPDATE_INTERVAL'den KUCUK kalirsa, operator
    yalniz UPDATER_CYCLE_TIMEOUT'u yukseltip UPDATER_WATCHDOG_TIMEOUT'u
    unutursa SAGLIKLI surecte bile watchdog onu 'wedge' sanip surekli
    os._exit ile sureci oldurur (thrash). Config yanlissa fail-safe: taban
    deger otomatik uygulanir + WARNING loglanir -- crash-loop yerine erken/
    gorunur (log) bir duzeltme tercih edildi (bkz. _validate_redis_url'in
    RuntimeError'i: o guvenlik riski, bu yalniz operasyonel bir uyumsuzluk)."""
    buffer_seconds = 60.0
    floor = (
        cfg.update_interval
        + cfg.updater_cycle_timeout
        + 2 * cfg.updater_guard_timeout
        + buffer_seconds
    )
    if cfg.updater_watchdog_timeout < floor:
        import logging

        logging.getLogger(__name__).warning(
            "UPDATER_WATCHDOG_TIMEOUT=%.0fsn guvenli tabanin (%.0fsn = "
            "UPDATE_INTERVAL+UPDATER_CYCLE_TIMEOUT+2*UPDATER_GUARD_TIMEOUT+"
            "%.0fsn tampon) ALTINDA -- saglikli surecte os._exit thrash'ini "
            "onlemek icin tabana yukseltiliyor.",
            cfg.updater_watchdog_timeout,
            floor,
            buffer_seconds,
        )
        object.__setattr__(cfg, "updater_watchdog_timeout", floor)


def _validate_redis_url(url: str) -> None:
    """Fail-fast guard (HIGH-1 savunma-katmani): `_build_redis_url` normal
    compose akisinda URL'i zaten guvenli hale getirir, ama REDIS_URL'in
    ELLE (compose-disi, kimlik bilgisi ONCEDEN gomulu) verildigi durumda
    parola hala URL-ozel karakter icerebilir. Boyle bir URL sessizce
    RedisStore.connect()/telegram_bot/rate-limiter icinde patlamak yerine,
    servis ACILISTA NET bir hatayla durur -- crash-loop + belirsiz
    `ValueError: Port could not be cast` yerine actionable mesaj."""
    if not url:
        return
    try:
        parsed = urlsplit(url)
        _ = parsed.port  # tetikleyici: bozuk userinfo/port burada patlar
    except ValueError as exc:
        raise RuntimeError(
            "REDIS_URL ayristirilamiyor (gecersiz port/kimlik bilgisi). Parola "
            "URL-ozel karakter (/, +, =, @, : vb.) iceriyorsa REDIS_URL yerine "
            "REDIS_URL=redis://host:port/db (kimlik bilgisiz) + REDIS_PASSWORD="
            "PAROLA kullanin -- parola otomatik guvenli sekilde URL'e gomulur "
            "(bkz. .env.example, docker-compose.yml)."
        ) from exc


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
    # Oncelik sirasi. yahoo_chart=v8 chart (saf async httpx, crumb'siz, dayanikli),
    # isyatirim=Turkiye (yurtdisi IP'lerden erisilemeyebilir). yahoo=yfinance(batch)
    # varsayilan zincirden CIKARILDI: crumb/cookie auth istegi (curl_cffi) bazen
    # sonsuza kadar asilip thread havuzunu doldurabiliyor (bkz. PROVIDER_FETCH_TIMEOUT
    # + yahoo provider'daki izole executor). Provider sinifi silinmedi; env ile
    # geri eklenebilir (PROVIDERS=yahoo,yahoo_chart,...).
    # tradingview de PATRON KARARIYLA (hukuki) varsayilan zincirden CIKARILDI:
    # TradingView Kullanim Sartlari §3 veriyi "yalnizca ekranda-gosterim" ile
    # sinirlar, otomatik islem/algoritmik karar-verme/fiyat referanslama YASAKTIR
    # -- Faz-2 client-side stop-loss tam bu tanima girer. Provider sinifi
    # silinmedi (bkz. providers/tradingview.py ust dosya uyarisi); env ile geri
    # eklenebilir ANCAK YALNIZCA insan-okur dashboard/teshis amaciyla, bot karar
    # yoluna BAGLANMAMALIDIR.
    providers: list[str] = field(
        default_factory=lambda: _get_list("PROVIDERS", ["yahoo_chart", "isyatirim"])
    )
    # failover: ilk veri donduren kaynak yeter (verimli).
    # gapfill: her kaynak bir oncekinin eksiklerini tamamlar (kesintisizlik, onerilen).
    # hybrid: failover + eksikler icin gapfill devam eder.
    provider_mode: str = field(default_factory=lambda: _get_str("PROVIDER_MODE", "gapfill"))
    # Provider yanit kapsam esigi (%). Altinda kalan yanit basarisiz sayilir.
    provider_min_coverage_pct: float = field(
        default_factory=lambda: _get_float("PROVIDER_MIN_COVERAGE_PCT", 95.0)
    )
    # Tek bir provider.fetch_quotes() cagrisi icin sert ust sinir (sn). Bir kaynak
    # (orn. yfinance/curl_cffi auth istegi) sonsuza kadar asilirsa bu sinir
    # asildiginda cagri iptal edilir, breaker basarisizlik kaydeder ve sonraki
    # kaynaga dusulur. Disaridan (updater cycle butcesi) gelen iptal bundan
    # AYRIDIR ve yutulmaz (CancelledError yukari yayilir).
    provider_fetch_timeout: float = field(
        default_factory=lambda: _get_float("PROVIDER_FETCH_TIMEOUT", 45.0)
    )
    # Sembol bazli devre kesici
    symbol_circuit_fail_threshold: int = field(
        default_factory=lambda: _get_int("SYMBOL_CIRCUIT_FAIL_THRESHOLD", 3)
    )
    symbol_circuit_reset_seconds: float = field(
        default_factory=lambda: _get_float("SYMBOL_CIRCUIT_RESET_SECONDS", 300.0)
    )
    # MEDIUM-7: guard'in (bayat-bar/damgasiz) TAMAMEN dusurdugu bir kaynak, bu
    # symbol_circuit'ten MUAF oldugu icin (MEDIUM-3) baska hicbir frenle
    # karsilasmiyordu -- seans boyunca sonsuza kadar (her turda) sorulmaya
    # devam edebilirdi. N tur ust uste TAMAMEN guard'la duserse kaynak
    # provider-seviyesinde gecici olarak cooldown'a alinir.
    guard_cooldown_fail_threshold: int = field(
        default_factory=lambda: _get_int("GUARD_COOLDOWN_FAIL_THRESHOLD", 3)
    )
    guard_cooldown_seconds: float = field(
        default_factory=lambda: _get_float("GUARD_COOLDOWN_SECONDS", 1800.0)
    )
    # HIGH-1: guard-cooldown esigi (yukarida) artik TUR (updater cycle)
    # basina bir kez degerlendirilir (bkz. aggregator.begin_cycle/end_cycle) --
    # eskiden BATCH basina degerlendiriliyordu (bir tur ~13 batch cagrisi
    # uretiyor), bu da "3 tur" sigortasini saniyeler icinde patlatiyordu.
    # Acilis toleransi: seans acilisindan sonraki bu kadar saniye icinde
    # (market.seconds_since_open) guard-dususleri streak'e YAZILMAZ (guard
    # yine calisir, bayat veri gecmez -- yalniz cooldown'u TETIKLEMEZ).
    # Kaynaklar acilisin ilk saniyelerinde henuz dunku barlarini guncellemiyor
    # olabilir; bu yapisal bir gecikme, kalici bir ariza degil. Varsayilan
    # 1200sn (20 dk): veri ~15 dk gecikmeli (bkz. README/delayed=True) --
    # acilista yahoo_chart'in regularMarketTime'i bugune ait damga uretmesi
    # bu gecikme + tampon kadar surebilir (canlida dogrulanmadi -- comert
    # varsayilan tercih edildi; 300sn iken 10:05-10:07 arasi streak esigi
    # asip her islem gunu ~10:37'ye kadar kor kalmaya yol acabiliyordu).
    guard_open_grace_seconds: float = field(
        default_factory=lambda: _get_float("GUARD_OPEN_GRACE_SECONDS", 1200.0)
    )
    # MEDIUM-2: streak'in yaslanmasi -- son artistan bu kadar saniye sonra
    # hicbir yeni tam-dusme olmadiysa streak SIFIRLANIR. Aksi halde sabah
    # erken saatte birikmis bir streak, saatlerce durup ogleden sonraki TEK
    # kotu turla cooldown'a donusebilirdi (streak'in "ardisiklik" anlami
    # bozulur). 0 = kapali (asla yaslanma ile sifirlanmaz).
    guard_drop_streak_max_age_seconds: float = field(
        default_factory=lambda: _get_float("GUARD_DROP_STREAK_MAX_AGE_SECONDS", 900.0)
    )
    # HIGH-3 (review-3): esik artik BATCH degil TUR (cycle) bazinda
    # degerlendirilir -- eskiden batch-bazli degerlendirme, watchlist/batch_size
    # boluminden kalan KUCUK son batch'in (orn. 525 sembol / 40'lik batch =
    # son batch 5 sembol) hicbir zaman bu esigi asamamasi yuzunden fail-open'i
    # o kalinti semboller icin YAPISAL OLARAK olu birakiyordu (korunma
    # `len(watchlist) % BATCH_SIZE`'a bagliydi -- deterministik olmayan bir
    # emniyet). Artik tum TURDA (begin_cycle/end_cycle arasinda biriken TUM
    # batch'ler) HICBIR sembol taze quote almadiysa VE cikarilan guard-dusmus
    # aday sayisi bu esigi asarsa fail-open tetiklenir -- "kaynak sayisina"
    # DEGIL (tek intraday kaynakli dunyada asla saglanamaz) "batch buyuklugune"
    # de DEGIL, CYCLE'in TAMAMINA bagli bir esiktir. Yalniz updater'in
    # yapilandirilmis TUR dongusu (count_toward_cooldown=True + begin_cycle/
    # end_cycle aktif) bu degerlendirmeye girer -- on-demand istekler (HIGH-3b)
    # asla fail-open tetiklemez (cycle state'i hic mevcut degildir).
    guard_fail_open_min_symbols: int = field(
        default_factory=lambda: _get_int("GUARD_FAIL_OPEN_MIN_SYMBOLS", 20)
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
    # --- Sembol evreni (takip listesi) ---
    # Statik BIST_SYMBOLS listesine EK semboller (virgulle). Statik liste her zaman
    # taban kalir; bu yalnizca ekler (kayipsiz). Yeni kotasyonlari elle eklemek icin.
    extra_symbols: list[str] = field(default_factory=lambda: _get_list("EXTRA_SYMBOLS", []))
    # Updater dongu basinda TradingView'den TUM BIST evrenini periyodik cekip
    # takip listesini genisletir (2026+ yeni hisseler otomatik gorunur).
    symbol_universe_refresh_enabled: bool = field(
        default_factory=lambda: _get_bool("SYMBOL_UNIVERSE_REFRESH_ENABLED", True)
    )
    symbol_universe_refresh_hours: float = field(
        default_factory=lambda: _get_float("SYMBOL_UNIVERSE_REFRESH_HOURS", 24.0)
    )
    # Cekilen evren bu sayidan az ise guvenilmez sayilir ve YOK SAYILIR (guard):
    # bozuk/kismi bir enumerate mevcut listeyi daraltmasin.
    symbol_universe_min_count: int = field(
        default_factory=lambda: _get_int("SYMBOL_UNIVERSE_MIN_COUNT", 400)
    )
    # Evren cekimi basarisiz/guard-reddi olursa bir sonraki denemeye kadar bekleme
    # (sn). Basarisizlikta her turda (60 sn) endpoint dovulmesini onler.
    symbol_universe_retry_seconds: float = field(
        default_factory=lambda: _get_float("SYMBOL_UNIVERSE_RETRY_SECONDS", 900.0)
    )

    # /history onbellek TTL (sn)
    history_cache_ttl: float = field(default_factory=lambda: _get_float("HISTORY_CACHE_TTL", 600.0))
    # /validate + yazma-zamani capraz-dogrulama + drift monitoru icin referans
    # kaynaklar. Quote'un KENDI kaynagi _pick_reference tarafindan yapisal
    # olarak DISLANIR (bkz. pipeline.py H3) -- bu yuzden en az 2 kaynak
    # gerekir, aksi halde tek birincil kaynak (orn. yahoo_chart) seans
    # boyunca HICBIR referans bulamaz (review HIGH-2: eski varsayim
    # [yahoo_chart, isyatirim] ile isyatirim seans-ici bayat-bar guard'i
    # (H2) yuzunden de elenince dogrulama fiilen tamamen olu kaliyordu).
    # tradingview ONCEDEN buraya eklenmisti (HIGH-1'de exchange_time saglar
    # hale geldigi icin) ama PATRON KARARIYLA (hukuki, bkz. providers.py
    # yukarisi) varsayilan zincirden CIKARILDI. BILINEN VE KABUL EDILEN
    # SONUC: seans icinde bagimsiz referans KALMADI -- isyatirim H2 bayat-bar
    # guard'iyla elenir, tek aday yahoo_chart kendi kaynagi oldugu icin
    # dislanir; _pick_reference fail-quiet donuyor (bist_validate_no_reference_total
    # artar). Bu, Faz-2 lisansli realtime karar verilene kadar acik kalan
    # bilinçli bir tavizdir (bkz. README/CHANGELOG).
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
    # CRITICAL-1: coklu-kaynak uzlasisi (yukarisi) TEK intraday kaynakli
    # dunyada (bkz. PROVIDERS yorumu) YAPISAL OLARAK asla tetiklenemez --
    # `candidates` her zaman <2 kalir, escape olu kalirdi (bedelsiz/split
    # sonrasi sembol KALICI olarak sanity'de kilitlenirdi). Israr teyidi:
    # AYNI kaynak, AYNI (asagidaki tolerans icindeki) fiyati N ARDISIK TURDA
    # tekrarlarsa kabul edilir -- gecici tick hatasi israr etmez, kurumsal
    # islem (bedelsiz/split) eder. 0 = kapali.
    sanity_escape_persist_rounds: int = field(
        default_factory=lambda: _get_int("SANITY_ESCAPE_PERSIST_ROUNDS", 3)
    )
    # Israr teyidinde "ayni fiyat" sayilmasi icin ardisik turlar arasi izin
    # verilen maksimum sapma (%).
    sanity_escape_persist_tolerance_pct: float = field(
        default_factory=lambda: _get_float("SANITY_ESCAPE_PERSIST_TOLERANCE_PCT", 1.0)
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
    # Wedge fix (15-16 Tem nuksu): _maybe_refresh_universe()/_refresh_age_metric()
    # updater_cycle_timeout'un DISINDA cagrilir (bkz. updater._loop) -- guard'siz
    # bir Redis/HTTP cagrisi asilirsa TUM dongu suresiz kilitlenirdi, piyasa
    # acik olsa bile yeni tur baslamazdi. Her iki cagri da bu butce ile
    # ayri ayri sarilir; asimda log + o turun ilgili adimi atlanir.
    updater_guard_timeout: float = field(
        default_factory=lambda: _get_float("UPDATER_GUARD_TIMEOUT", 20.0)
    )
    # Savunma-katmani: ana dongu bu sureden uzun tur TAMAMLAMAZSA (yukaridaki
    # guard'lara ragmen beklenmeyen bir asilma) surec SERT olarak sonlandirilir
    # (bkz. updater.BackgroundUpdater._watchdog_loop) -- updater HTTP servisi
    # olmadigi icin Docker healthcheck'i yok; compose'daki 'restart: unless-
    # stopped' surecin kendisi olunce devreye girer. Tek bir turun en kotu
    # olcekte alabilecegi sure (guard+guard+cycle) + update_interval'in cok
    # uzerinde, comert bir tampon.
    updater_watchdog_timeout: float = field(
        default_factory=lambda: _get_float("UPDATER_WATCHDOG_TIMEOUT", 600.0)
    )
    updater_watchdog_check_interval: float = field(
        default_factory=lambda: _get_float("UPDATER_WATCHDOG_CHECK_INTERVAL", 30.0)
    )
    # /ready DEDEKTORUN KENDISI store cagrisi asilirsa (wedge) suresiz beklemesin
    # -- bu butce asilinca yapisal 503 doner (store cokmesiyle ayni davranis).
    ready_probe_timeout: float = field(
        default_factory=lambda: _get_float("READY_PROBE_TIMEOUT", 10.0)
    )

    # Bulunamayan (kaynaklarda olmayan) semboller icin negatif onbellek TTL'i (sn).
    # Ayni gecersiz sembole tekrarli isteklerin upstream'i dovmesini onler.
    negative_cache_ttl: float = field(
        default_factory=lambda: _get_float("NEGATIVE_CACHE_TTL", 60.0)
    )

    # --- Redis (bos ise in-memory store kullanilir) ---
    # HIGH-1 (PR#19 review): REDIS_URL, REDIS_PASSWORD varsa `_build_redis_url`
    # ile TEK NOKTADA guvenli (percent-encoded) sekilde birlestirilir -- bkz.
    # yukaridaki fonksiyon docstring'i. RedisStore/telegram_bot/rate-limiter
    # (deps.py storage_uri) HEPSI bu alani okur, degisiklik gerektirmezler.
    redis_url: str = field(
        default_factory=lambda: _build_redis_url(
            _get_str("REDIS_URL", ""), _get_str("REDIS_PASSWORD", "")
        )
    )
    # Ham parola (compose'un ayri gecirdigi env) -- yalniz `redis_url`
    # insasinda kullanilir, baska hicbir yerde okunmaz/loglanmaz.
    redis_password: str = field(default_factory=lambda: _get_str("REDIS_PASSWORD", ""))
    redis_prefix: str = field(default_factory=lambda: _get_str("REDIS_PREFIX", "bist"))
    # Wedge fix (kok neden): eskiden yalniz socket_connect_timeout (TCP el
    # sikisma) vardi -- baglanti KURULDUKTAN SONRA bir komutun soket okumasi
    # sessizce asilirsa hicbir sinir yoktu (redis-py suresiz beklerdi, exception
    # atmazdi). pubsub.listen() bundan ETKILENMEZ (redis-py blok=True'da
    # timeout=math.inf gonderip bu ayari BILEREK gecersiz kilar -- bos SSE
    # kanallari yanlislikla kopmaz, dogrulandi).
    redis_socket_timeout: float = field(
        default_factory=lambda: _get_float("REDIS_SOCKET_TIMEOUT", 10.0)
    )

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
    # Guvenli varsayilan: bos (same-origin only, cross-origin tarayici istegi
    # reddedilir). Dashboard/panel nginx reverse-proxy ile ayni-origin gittigi
    # icin (bkz. deploy/panel) bu varsayilan onu ETKILEMEZ. Cross-origin bir
    # tarayici istemciniz varsa CORS_ORIGINS ile acikca izin verin.
    cors_origins: list[str] = field(default_factory=lambda: _get_list("CORS_ORIGINS", []))
    rate_limit: str = field(default_factory=lambda: _get_str("RATE_LIMIT", "120/minute"))
    rate_limit_enabled: bool = field(default_factory=lambda: _get_bool("RATE_LIMIT_ENABLED", True))
    # /quotes ve /validate icin sembol listesi ust siniri: tek istekte asiri
    # sayida sembol (her biri cache'te yoksa upstream provider'lara tek tek
    # dusebilir) kaynaklari zorlayan bir DoS yuzeyi olusturmasin.
    max_symbols_per_request: int = field(
        default_factory=lambda: _get_int("MAX_SYMBOLS_PER_REQUEST", 100)
    )

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

    # --- Telegram bot (REST API istemcisi; ayri surec) ---
    # token bos/enabled=false ise bot main() temiz cikar (crash yok).
    telegram_enabled: bool = field(default_factory=lambda: _get_bool("TELEGRAM_ENABLED", False))
    telegram_bot_token: str = field(default_factory=lambda: _get_str("TELEGRAM_BOT_TOKEN", ""))
    telegram_api_url: str = field(
        default_factory=lambda: _get_str("TELEGRAM_API_URL", "http://api:8000")
    )
    # BIST API auth aciksa gonderilecek X-API-Key (bos ise baslik eklenmez).
    telegram_api_key: str = field(default_factory=lambda: _get_str("TELEGRAM_API_KEY", ""))
    telegram_poll_timeout: int = field(
        default_factory=lambda: _get_int("TELEGRAM_POLL_TIMEOUT", 30)
    )
    # Bos ise herkes /start edebilir; dolu ise yalniz bu chat id'ler.
    telegram_allowed_chats: list[str] = field(
        default_factory=lambda: _get_list("TELEGRAM_ALLOWED_CHATS", [])
    )
    telegram_market_poll_seconds: float = field(
        default_factory=lambda: _get_float("TELEGRAM_MARKET_POLL_SECONDS", 30.0)
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

    def __post_init__(self) -> None:
        # HIGH-1 savunma-katmani: `redis_url` `_build_redis_url` ile normal
        # compose akisinda zaten guvenli hale gelir; bu, YALNIZCA REDIS_URL'in
        # elle/compose-disi kimlik-bilgili verildigi (ve hala bozuk oldugu)
        # durumda acilista NET hatayla durur (bkz. _validate_redis_url).
        _validate_redis_url(self.redis_url)
        _enforce_watchdog_floor(self)

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
