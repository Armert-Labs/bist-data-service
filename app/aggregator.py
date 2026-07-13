"""Coklu kaynak orkestrasyonu: fallback + circuit breaker + sanity-check.

Strateji: Saglayicilar oncelik sirasiyla denenir. gapfill modunda her kaynak
eksik sembolleri tamamlar. Bir kaynak ard arda hata verirse circuit breaker onu
gecici devre disi birakir.

Sanity-check: onceki bilinen fiyata gore absurt sicramalar elenir.
Kapsam kontrolu: kismi/bos yanit basarisiz sayilir (circuit breaker tetiklenir).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from . import metrics
from .config import settings
from .market import is_market_open, is_stale_bar, seconds_since_open
from .models import HistoryResponse, Quote
from .providers.base import CircuitBreaker, Provider
from .providers.isyatirim import IsYatirimProvider
from .providers.tradingview import TradingViewProvider
from .providers.yahoo import YahooProvider
from .providers.yahoo_chart import YahooChartProvider
from .symbol_circuit import symbol_circuit

logger = logging.getLogger(__name__)

_FACTORY = {
    "yahoo": YahooProvider,
    "yahoo_chart": YahooChartProvider,
    "tradingview": TradingViewProvider,
    "isyatirim": IsYatirimProvider,
}


class Aggregator:
    def __init__(self) -> None:
        self._providers: list[tuple[Provider, CircuitBreaker]] = []
        # Sembol -> (ilk red, son red) monotonic. Kesintisiz red penceresini izler;
        # herhangi bir kabulde temizlenir, escape'ten uzun bosluk pencereyi sifirlar.
        self._sanity_reject_since: dict[str, tuple[float, float]] = {}
        # CRITICAL-1(b): sembol -> (ilk aykiri fiyat/tolerans-ankraji, ardisik
        # ISRAR sayaci, kaynak adi, son guncelleme ani/monotonic). Ayni kaynak
        # ayni fiyati N ardisik TURDA (fetch_quotes CAGRISI -- provider basina
        # degil, bkz. _update_persist_and_check) tekrarlarsa kabul edilir --
        # coklu-kaynak sartina (candidates>=2) BAGIMLI DEGILDIR, tek intraday
        # kaynakli dunyada (bkz. config.py PROVIDERS yorumu) escape'in TEK
        # canli yolu budur. Escape penceresinden (SANITY_REJECT_ESCAPE_SECONDS)
        # uzun bir bosluk (restart/kesinti) "ardisiklik"i bozar, sayac sifirlanir.
        self._sanity_persist: dict[str, tuple[float, int, str, float]] = {}
        # MEDIUM-7: provider adi -> ardisik TAM guard-dusme (TUR bazinda,
        # bkz. begin_cycle/end_cycle -- HIGH-1) sayisi / son artis ani
        # (monotonic, MEDIUM-2 yaslanma icin) / cooldown bitis ani.
        # symbol_circuit bu dususlerden MUAF oldugu icin (MEDIUM-3) baska
        # hicbir fren yoktu; bkz. asagidaki fetch_quotes/begin_cycle/end_cycle.
        self._guard_drop_streak: dict[str, int] = {}
        self._guard_drop_streak_updated_at: dict[str, float] = {}
        self._guard_cooldown_until: dict[str, float] = {}
        # HIGH-1: bir TUR (updater'in tek _update_once() cagrisi) birden fazla
        # batch'e (fetch_quotes cagrisina) bolunur. None = su an aktif bir tur
        # yok (begin_cycle cagrilmadi) -- bu durumda count_toward_cooldown=True
        # gecilse bile HICBIR SEY biriktirilmez (savunma amacli no-op).
        self._cycle_fully_dropped: dict[str, bool] | None = None
        # HIGH-2(b): cooldown'da olan bir provider'a TUR basina EN FAZLA 1
        # "prob" (half-open) denemesi -- circuit-breaker half-open deseniyle
        # ayni ruh. None = aktif tur yok.
        self._cooldown_probed_this_cycle: set[str] | None = None
        # HIGH-3 (review-3): fail-open artik BATCH degil TUR (cycle) bazinda
        # karar verilir (bkz. end_cycle). Bu turda HICBIR batch'te fresh
        # (guard'la dusmemis/kabul edilmis) quote alan sembol -- fail-open
        # yalniz bu KUME BOSSA degerlendirilir ("TUM watchlist sifir taze
        # quote uretti mi?"). guard_dropped_candidates ise TUM batch'lerden
        # (henuz fresh olmamis semboller icin) biriken son guard-dusmus
        # aday quote'lardir; end_cycle bunlari (MEDIUM-1: sanity'den GECIRIP)
        # stale=true ile geri dondurur. _cycle_previous, end_cycle'daki
        # sanity kontrolu icin biriken en guncel `previous` anlamli fiyatlardir.
        self._cycle_fresh_symbols: set[str] | None = None
        self._cycle_guard_dropped_candidates: dict[str, Quote] | None = None
        self._cycle_previous: dict[str, float] | None = None
        for name in settings.providers:
            factory = _FACTORY.get(name)
            if factory is None:
                logger.warning("Bilinmeyen provider atlandi: %s", name)
                continue
            self._providers.append((factory(), CircuitBreaker(name)))
            metrics.PROVIDER_UP.labels(provider=name).set(1)
        if not self._providers:
            raise RuntimeError("Hic gecerli provider yapilandirilmadi (PROVIDERS).")

    @property
    def provider_states(self) -> dict[str, str]:
        return {p.name: cb.state for p, cb in self._providers}

    def _coverage_ok(self, fetched: dict[str, Quote], ask: list[str]) -> bool:
        if not ask:
            return True
        if not fetched:
            return False
        hit = sum(1 for s in ask if s in fetched)
        ratio = hit / len(ask)
        return ratio >= (settings.provider_min_coverage_pct / 100.0)

    def _is_sane(self, symbol: str, quote: Quote, previous: dict[str, float] | None) -> bool:
        prev = (previous or {}).get(symbol)
        if prev is None or prev == 0 or quote.price is None:
            self._sanity_reject_since.pop(symbol, None)
            self._sanity_persist.pop(symbol, None)
            return True
        change_pct = abs((quote.price - prev) / prev * 100.0)
        if change_pct <= settings.sanity_max_change_percent:
            self._sanity_reject_since.pop(symbol, None)
            self._sanity_persist.pop(symbol, None)
            return True

        escape = settings.sanity_reject_escape_seconds
        if escape > 0:
            now = time.monotonic()
            entry = self._sanity_reject_since.get(symbol)
            # Escape'ten uzun fetch boslugu "kesintisiz red" sayilmaz: gece/kesinti
            # sonrasi ilk bozuk tick'in aninda kacis almasini onler.
            if entry is None or now - entry[1] > escape:
                self._sanity_reject_since[symbol] = (now, now)
            else:
                self._sanity_reject_since[symbol] = (entry[0], now)

        metrics.SANITY_REJECTS.inc()
        logger.warning(
            "Sanity reddi %s: %%%.1f degisim (%.4f -> %.4f)",
            symbol,
            change_pct,
            prev,
            quote.price,
        )
        return False

    def _update_persist_and_check(self, symbol: str, quote: Quote) -> bool:
        """CRITICAL-1(b): ayni kaynak SANITY_ESCAPE_PERSIST_ROUNDS ardisik
        TURDA (her `fetch_quotes()` cagrisinda -- provider basina degil,
        BURADAN tam olarak BIR KEZ cagrilir, bkz. `_try_escape`) AYNI
        (tolerans icindeki) aykiri fiyati tekrarladi mi? Gecici tick hatasi
        israr etmez; kurumsal islem (bedelsiz/split) eder. Escape
        penceresinden (`SANITY_REJECT_ESCAPE_SECONDS`) uzun bir fetch
        bosluğu (restart/kesinti) "ardisiklik"i bozar -- `_sanity_reject_since`
        ile ayni "kesintisiz" felsefesi (bkz. _is_sane)."""
        rounds = settings.sanity_escape_persist_rounds
        if rounds <= 0:
            self._sanity_persist.pop(symbol, None)
            return False
        if quote.price is None:
            return False
        now = time.monotonic()
        tol = settings.sanity_escape_persist_tolerance_pct
        gap_limit = settings.sanity_reject_escape_seconds
        entry = self._sanity_persist.get(symbol)
        same_run = (
            entry is not None
            and entry[2] == quote.source
            and entry[0] != 0
            and abs(quote.price - entry[0]) / abs(entry[0]) * 100.0 <= tol
            and (gap_limit <= 0 or now - entry[3] <= gap_limit)
        )
        count = entry[1] + 1 if same_run and entry is not None else 1
        self._sanity_persist[symbol] = (quote.price, count, quote.source, now)
        return count >= rounds

    def _accept_escape(self, symbol: str, quote: Quote, reason: str) -> None:
        metrics.SANITY_ESCAPES.labels(provider=quote.source, reason=reason).inc()
        logger.critical(
            "Sanity kacisi %s: '%s' ile kabul edildi (kaynak=%s, fiyat=%.4f)",
            symbol,
            reason,
            quote.source,
            quote.price,
        )
        self._sanity_reject_since.pop(symbol, None)
        self._sanity_persist.pop(symbol, None)

    def _try_escape(self, symbol: str, candidates: list[Quote]) -> Quote | None:
        """Kesintisiz sanity reddi altindaki bir sembolun kabul edilme yollari.

        (a) Israr teyidi (CRITICAL-1b): bkz. `_update_persist_and_check`. Tek
            intraday kaynakli dunyada (bkz. config.py PROVIDERS yorumu) bu
            dunyanin TEK canli escape yoludur.
        (b) Coklu-kaynak uzlasisi (Faz-2, birden fazla BAGIMSIZ kaynak varsa):
            kesintisiz red penceresi (SANITY_REJECT_ESCAPE_SECONDS) dolduysa
            VE en az iki kaynak yeni fiyatta uzlasiyorsa ilk adayi kabul eder.
            Tek kaynakli dunyada `candidates` hep <2 kalir, bu yol hicbir
            zaman tetiklenmez.

        HIGH-2 (review-4, GUVENLIK): eskiden ucuncu bir yol daha vardi --
        kaynagin KENDI `previous_close`'unun yeni `price` ile tutarli olmasina
        (VE bizim bildigimizden onemli olcude farkli olmasina) dayanan "ic
        tutarlilik" escape'i, TEK TURDA teyitsiz kabul ediyordu. Kanitlanmis
        acik: yanlis-birim/yanlis-enstruman (orn. kaynak THYAO icin USD
        satirini donerse: price=2.50, previous_close=2.40 -- ikisi BIRLIKTE
        kaydigi icin kendi icinde tutarli GORUNUR VE bizim TRY fiyatimizdan
        [orn. 100] "onemli olcude farkli" da GORUNUR) tam da guard'in var olus
        sebebi olan hata sinifini escape'in KENDI anahtarina cevirir -- tek
        bir bozuk turda (60 sn) client-side stop-loss'u besleyen feed'e
        sessizce sizar. Bu yol KALDIRILDI; israr teyidi (3 tur, ~3 dk gecikme)
        kabul edilen tek yoldur -- gecici/bozuk bir payload 3 ardisik turda
        israr etmez, gercek kurumsal islem (bedelsiz/split) eder.

        Bu fonksiyon sembol basina fetch_quotes() CAGRISI basina TAM OLARAK
        BIR KEZ cagrilir (rejected sozlugunde bir kez) -- (a)'nin ardisik-TUR
        sayaci bu yuzden PROVIDER-bazli degil CAGRI-bazli ilerler (bir batch
        icinde ayni sembol icin birden fazla kaynak denenmis olsa bile).

        Damga (`_sanity_reject_since`/`_sanity_persist`) kabul commit'e
        donusmezse (orn. cross-validate dusurdu) sifirlanmadan kalir.
        """
        if not candidates:
            return None
        base = candidates[0]
        if base.price is None:
            return None

        if self._update_persist_and_check(symbol, base):
            self._accept_escape(symbol, base, "persistence")
            return base

        escape = settings.sanity_reject_escape_seconds
        if escape <= 0 or len(candidates) < 2:
            return None
        entry = self._sanity_reject_since.get(symbol)
        now = time.monotonic()
        if entry is None or now - entry[0] < escape:
            return None
        max_pct = settings.cross_validate_max_pct
        agree = sum(
            1
            for other in candidates[1:]
            if other.price is not None
            and abs(other.price - base.price) / base.price * 100.0 <= max_pct
        )
        if agree < 1:
            return None
        logger.warning(
            "Sanity kacisi %s: %d kaynak yeni fiyatta uzlasti, %.0f sn kesintisiz "
            "red sonrasi kabul ediliyor (fiyat=%.4f)",
            symbol,
            agree + 1,
            now - entry[0],
            base.price,
        )
        metrics.SANITY_ESCAPES.labels(provider=base.source, reason="corroboration").inc()
        self._sanity_reject_since.pop(symbol, None)
        self._sanity_persist.pop(symbol, None)
        return base

    def get_provider(self, name: str) -> Provider | None:
        for provider, _ in self._providers:
            if provider.name == name:
                return provider
        return None

    def begin_cycle(self) -> None:
        """HIGH-1: updater'in tam-turu (tum batch'leri) BASLAMADAN ONCE cagirir.

        Guard-drop streak'i artik BATCH degil TUR bazinda degerlendirilir --
        bir tur tipik olarak ~13 `fetch_quotes()` (batch) cagrisi uretir;
        batch-bazli sayim "N tur ust uste" sigortasini saniyeler icinde
        patlatiyordu (asil niyet: N GUNCELLEME TURU, N batch degil). Tur
        boyunca biriken provider->"su ana kadar HEP tam mi dustu" bilgisini
        `end_cycle()` nihai olarak degerlendirir (streak +1 / sifirlama /
        cooldown). HIGH-3: ayni sekilde bu turda hic fresh quote alan sembol
        var mi + guard-dusmus adaylar da (fail-open TUR-bazli karari icin)
        burada birikmeye baslar.
        """
        self._cycle_fully_dropped = {}
        self._cooldown_probed_this_cycle = set()
        self._cycle_fresh_symbols = set()
        self._cycle_guard_dropped_candidates = {}
        self._cycle_previous = {}

    def end_cycle(self, now: datetime | None = None, *, aborted: bool = False) -> dict[str, Quote]:
        """Tur sonunda (tum batch'ler bitince) biriken durumu DEGERLENDIRIR.

        `begin_cycle()` cagrilmadiysa no-op (savunma amacli), bos dict doner.

        HIGH-3: fail-open karari artik BURADA, TUR duzeyinde verilir --
        eskiden HER BATCH kendi icinde (`len(symbols) >= esik`) karar
        veriyordu; bu, watchlist/BATCH_SIZE boluminden kalan kucuk son
        batch'i (orn. 525 sembol / 40'lik batch = kalan 5) sistemik bir
        kesinti sirasinda bile hicbir zaman esigi asamayacagi icin
        korumasiz birakiyordu (korunma batch boyutuna bagliydi, deterministik
        olmayan bir emniyet) VE `count_toward_cooldown`'a BAKMADIGI icin
        on-demand `/quotes` cagrilari da (>=esik cache-miss sembolle)
        tetikleyebiliyordu (bkz. review). Artik: bu TURDA (TUM batch'ler)
        HICBIR sembol taze quote almadiysa VE cikarilan guard-dusmus aday
        sayisi esigi asarsa, biriken adaylar (MEDIUM-1: sanity'den GECIRILEREK)
        stale=true ile dondurulur -- cagiran (updater) bunlari commit eder.
        On-demand cagrilar begin_cycle() hic cagirmadigi icin bu yola HICBIR
        ZAMAN giremez (cycle state'i mevcut degildir).

        `aborted` (MEDIUM-1/LOW-1, review-4): tur iptal/timeout (updater_
        cycle_timeout) veya erken durdurma (`BackgroundUpdater.stop()`) ile
        YARIM kaldiysa True gecilir -- boyle bir turda "hicbir sembol taze
        quote almadi" olcusu YALNIZCA kosulan az sayida batch'e dayanir,
        TUM watchlist icin sistemik kanit SAYILMAZ (bkz. review: 14 batch'ten
        2'si kosarsa trivially "hepsi guard'a dustu" gorunur). `aborted=True`
        iken fail-open VE provider guard-drop streak degerlendirmesi HIC
        YAPILMAZ -- ne metrik ne CRITICAL log ne commit; cagiran (updater)
        hicbir sey commit etmez, bir sonraki (tam) turda normal degerlendirme
        kaldigi yerden devam eder.

        Acilis toleransi (`GUARD_OPEN_GRACE_SECONDS`): HIGH-1 (review-4,
        REGRESYON): veri ~15 dk gecikmeli oldugu icin acilisin ilk ~15
        dakikasinda kaynagin regularMarketTime'i hala dunku/Cuma gunune ait
        olabilir -- guard TUM sembolleri duser, bu pencerede fail-open HER
        ISLEM GUNU rutin olarak tetiklenir (bu BEKLENEN bir durumdur, ariza
        degildir). Acilis toleransi penceresindeyken fail-open quote'lari
        YINE dondurulur (veri sürekliligi, `stale=true` ile) AMA
        `bist_guard_fail_open_total` sayaci artirilmaz ve CRITICAL log
        basilmaz -- aksi halde bu sayac/log her sabah rutin olarak ates
        alir, gercek bir takvim-hatasi (bilinmeyen resmi tatil) sinyalinden
        ayirt edilemez hale gelirdi (alarm yorgunlugu). Pencere disinda
        (eskisi gibi) sayac + CRITICAL log basilir. Provider guard-drop
        streak'i icin de ayni pencere kullanilir: tam guard-dususleri
        streak'e YAZILMAZ -- guard yine calisir (bayat/damgasiz veri gecmez),
        yalniz cooldown'u TETIKLEMEZ.

        LOW-2: bu turda fail-open tetiklendiyse TUM turun guard-drop
        degerlendirmesi (provider streak/cooldown) atlanir -- fail-open
        zaten sistemik bir isaret oldugundan, ayni turun provider'lara ait
        "tam dustu" bilgisi de suphelidir.
        """
        accumulated = self._cycle_fully_dropped
        fresh_symbols = self._cycle_fresh_symbols
        guard_candidates = self._cycle_guard_dropped_candidates
        cycle_previous = self._cycle_previous
        self._cycle_fully_dropped = None
        self._cooldown_probed_this_cycle = None
        self._cycle_fresh_symbols = None
        self._cycle_guard_dropped_candidates = None
        self._cycle_previous = None
        if accumulated is None:
            return {}
        if aborted:
            logger.debug(
                "Tur iptal/timeout veya erken durdurma ile yarim kaldi -- "
                "kismi veri sistemik kanit SAYILMAZ; fail-open/guard-drop "
                "streak degerlendirmesi bu tur icin ATLANDI."
            )
            return {}

        moment = now or datetime.now(UTC)
        since_open = seconds_since_open(moment)
        in_grace = since_open is not None and since_open < settings.guard_open_grace_seconds

        fail_open_result: dict[str, Quote] = {}
        if (
            not fresh_symbols
            and guard_candidates
            and len(guard_candidates) >= settings.guard_fail_open_min_symbols
        ):
            for s, q in guard_candidates.items():
                # MEDIUM-1: fail-open'in KENDISI sanity-check'i ATLAMAMALI --
                # aksi halde absurt bir fiyat + bayat damga birlikte commit
                # edilip `previous`'i kalici olarak zehirleyebilir (CRITICAL-1
                # ile birlesince zehir KALICI olurdu).
                if self._is_sane(s, q, cycle_previous):
                    q.stale = True
                    fail_open_result[s] = q
            if fail_open_result:
                if in_grace:
                    logger.info(
                        "%d sembolluk bir TURDA (esik %d) hicbir kaynak taze "
                        "veri uretemedi -- acilis-toleransi penceresinde "
                        "(< %.0f sn), bu BEKLENEN bir durumdur (veri ~15 dk "
                        "gecikmeli); veri sürekliligi icin fail-open ile "
                        "gecirildi ama sayac/CRITICAL log basilmadi.",
                        len(fail_open_result),
                        settings.guard_fail_open_min_symbols,
                        settings.guard_open_grace_seconds,
                    )
                else:
                    metrics.GUARD_FAIL_OPEN.inc()
                    logger.critical(
                        "%d sembolluk bir TURDA (esik %d) hicbir kaynak taze veri "
                        "uretemedi -- piyasa-acik varsayimi (tatil listesi/"
                        "last_trading_day?) hatali olabilir; guard bu TUR icin "
                        "FAIL-OPEN yapildi (veri geciyor, cooldown tetiklenmedi).",
                        len(fail_open_result),
                        settings.guard_fail_open_min_symbols,
                    )

        if fail_open_result:
            logger.debug(
                "Bu turda fail-open olustu -- guard-drop streak degerlendirmesi "
                "TUM tur icin atlandi (sistemik sinyal, provider'a atfedilmez)."
            )
            return fail_open_result

        for provider_name, fully_dropped in accumulated.items():
            self._apply_cycle_guard_result(provider_name, fully_dropped, in_grace)
        return fail_open_result

    def _apply_cycle_guard_result(
        self, provider_name: str, fully_dropped: bool, in_grace: bool
    ) -> None:
        now_m = time.monotonic()
        # MEDIUM-2: streak yaslanmasi -- son artistan uzun sure (varsayilan
        # 15 dk) hicbir yeni tam-dusme olmadiysa eski streak GECERSIZ sayilir.
        # Aksi halde sabah erken saatte birikmis bir streak saatlerce durup
        # ogleden sonraki TEK kotu turla cooldown'a donusebilirdi.
        last_seen = self._guard_drop_streak_updated_at.get(provider_name)
        max_age = settings.guard_drop_streak_max_age_seconds
        if last_seen is not None and max_age > 0 and (now_m - last_seen) > max_age:
            self._guard_drop_streak.pop(provider_name, None)
            self._guard_drop_streak_updated_at.pop(provider_name, None)

        if not fully_dropped:
            self._guard_drop_streak.pop(provider_name, None)
            self._guard_drop_streak_updated_at.pop(provider_name, None)
            return

        if in_grace:
            logger.debug(
                "%s acilis-toleransi penceresinde (< %.0f sn) tam guard-dustu -- "
                "cooldown sayacina yazilmadi",
                provider_name,
                settings.guard_open_grace_seconds,
            )
            return

        streak = self._guard_drop_streak.get(provider_name, 0) + 1
        self._guard_drop_streak[provider_name] = streak
        self._guard_drop_streak_updated_at[provider_name] = now_m
        if streak >= settings.guard_cooldown_fail_threshold:
            # HIGH-2(a): cooldown'un amaci "bozuk kaynagi dovme, DIGERLERI
            # servis etsin"dir -- yararlanacak baska bir intraday-capable
            # kaynak yoksa (bu provider'in kendisi intraday-capable VE
            # toplamda tek intraday-capable kaynaksa) bu provider'i susturmak
            # feed'i KENDI KENDINE keser (TV cikti + isyatirim EOD-only
            # dunyasinda yahoo_chart TEK kalir). Bu durumda cooldown
            # UYGULANMAZ -- guard yine bayat veriyi eler, ama provider her
            # tur yeniden denenir (streak izlenebilirlik icin birikmeye
            # devam eder).
            provider_obj = next((p for p, _ in self._providers if p.name == provider_name), None)
            is_this_intraday = bool(provider_obj and provider_obj.intraday_capable)
            intraday_capable_count = sum(1 for p, _ in self._providers if p.intraday_capable)
            if is_this_intraday and intraday_capable_count < 2:
                logger.warning(
                    "%s ardisik %d TUR TAMAMEN guard'la dustu ama tek intraday-capable "
                    "kaynak (mevcut: %d) -- cooldown UYGULANMADI (susturmak feed'i "
                    "kendi kendine keserdi); guard bayat veriyi elemeye devam eder.",
                    provider_name,
                    streak,
                    intraday_capable_count,
                )
                return
            self._guard_cooldown_until[provider_name] = now_m + settings.guard_cooldown_seconds
            # Cooldown aktive edilince sayac sifirlanir: aksi halde cooldown
            # bitiminde ilk kontrolde streak zaten esigin uzerinde kalir ve
            # provider hicbir gercek yeniden-deneme sansi olmadan ANINDA
            # tekrar cooldown'a girer (sonsuz dongu).
            self._guard_drop_streak.pop(provider_name, None)
            self._guard_drop_streak_updated_at.pop(provider_name, None)
            metrics.PROVIDER_GUARD_COOLDOWN.labels(provider=provider_name).set(1)
            logger.warning(
                "%s ardisik %d TUR boyunca TAMAMEN guard'la dustu -- %.0f sn cooldown'a alindi",
                provider_name,
                streak,
                settings.guard_cooldown_seconds,
            )

    async def fetch_quotes(
        self,
        symbols: list[str],
        previous: dict[str, float] | None = None,
        now: datetime | None = None,
        *,
        count_toward_cooldown: bool = False,
    ) -> dict[str, Quote]:
        """Semboller icin coklu-kaynak fiyat cekimi.

        `count_toward_cooldown` (HIGH-2): bu cagrinin guard-drop sonucu
        provider-seviyesi cooldown streak'ine VE HIGH-3 fail-open TUR
        muhasebesine (bkz. begin_cycle/end_cycle) YAZILSIN mi? Yalniz
        updater'in yapilandirilmis TUR dongusu (tum takip listesini kapsayan,
        temsili buyuklukte batch'ler) True gecer. On-demand / tek-sembol
        cagrilar (pipeline.fetch_quotes, drift monitorun eksik-sembol
        tamamlamasi) varsayilan False kalir -- aksi halde bugun islem
        gormemis TEK bir hissenin tekrarli sorgulanmasi, koca bir kaynagi TUM
        semboller icin cooldown'a sokabilir VEYA fail-open'i yanlislikla
        tetikleyebilirdi (HIGH-3b).
        """
        mode = settings.provider_mode.lower()
        gapfill = mode in ("gapfill", "hybrid")
        result: dict[str, Quote] = {}
        rejected: dict[str, list[Quote]] = {}
        remaining = list(symbols)
        min_cov = settings.provider_min_coverage_pct / 100.0
        moment = now or datetime.now(UTC)
        market_open_now = is_market_open(moment)
        # HIGH-3: guard'in dusurdugu (silinmeden ONCE saklanan) adaylar --
        # bu TURDA (begin_cycle/end_cycle) hicbir sembol fresh olmadiysa
        # end_cycle() bunlari geri alabilsin diye. Ilk-kazanir oncelikle
        # (ayni oncelik sirasi normal sonuclarla ayni).
        guard_dropped_candidates: dict[str, Quote] = {}
        # HIGH-1: bu BATCH'in (bu tek fetch_quotes cagrisinin) provider basina
        # sonucu -- cycle biriktiricisine ancak batch SONUNDA merge edilir.
        batch_fully_dropped: dict[str, bool] = {}

        for provider, breaker in self._providers:
            if not remaining:
                break

            # YAPISAL ONERI (review): seans acikken gun-ici bar VEREMEYEN
            # (EOD-only, orn. isyatirim) kaynak HIC SORGULANMAZ -- bu bir
            # ariza degil yapisal bir kisittir; sorgulamak guard'in zaten
            # her turda dusurecegi bosuna bir istek olurdu (+ gereksiz
            # cooldown baskisi).
            if market_open_now and not provider.intraday_capable:
                logger.debug("%s seans icinde intraday-capable degil, atlaniyor", provider.name)
                continue

            if not breaker.allow():
                logger.debug("%s devre disi (circuit=%s), atlaniyor", provider.name, breaker.state)
                continue

            cooldown_until = self._guard_cooldown_until.get(provider.name)
            is_cooldown_probe = False
            if cooldown_until is not None:
                if time.monotonic() >= cooldown_until:
                    del self._guard_cooldown_until[provider.name]
                    metrics.PROVIDER_GUARD_COOLDOWN.labels(provider=provider.name).set(0)
                else:
                    # HIGH-2(b): half-open -- cooldown suresi dolmadan, TUR
                    # basina EN FAZLA 1 "prob" denemesine izin ver (circuit
                    # breaker half-open deseniyle ayni ruh). Yalniz
                    # yapilandirilmis TUR (count_toward_cooldown) prob yapar;
                    # on-demand cagrilar cooldown'a mutlak saygi gosterir.
                    probed = self._cooldown_probed_this_cycle
                    if count_toward_cooldown and probed is not None and provider.name not in probed:
                        probed.add(provider.name)
                        is_cooldown_probe = True
                        logger.debug(
                            "%s cooldown'da ama bu tur icin 1 prob deneniyor (half-open)",
                            provider.name,
                        )
                    else:
                        logger.debug(
                            "%s guard-cooldown'da (bitis=%.0f), atlaniyor",
                            provider.name,
                            cooldown_until,
                        )
                        continue

            ask = remaining if gapfill else symbols
            ask = [s for s in ask if symbol_circuit.allow(provider.name, s)]
            if not ask:
                continue

            metrics.FETCH_REQUESTS.labels(provider=provider.name).inc()
            try:
                fetched = await asyncio.wait_for(
                    provider.fetch_quotes(ask), settings.provider_fetch_timeout
                )
            except TimeoutError:
                # NOT: disaridan (guncelleme turu butcesi) gelen bir iptal burada
                # YAKALANMAZ — o CancelledError'dur (BaseException), Exception'dan
                # TUREMEZ, bu try/except'i atlayip yukari yayilir. Bu blok yalnizca
                # BU cagrinin KENDI ic zaman asimini (provider_fetch_timeout) yakalar.
                breaker.record_failure()
                metrics.FETCH_ERRORS.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                for s in ask:
                    symbol_circuit.record_failure(provider.name, s)
                logger.warning(
                    "%s fetch zaman asimi (%.0f sn), sonraki kaynaga dusuluyor",
                    provider.name,
                    settings.provider_fetch_timeout,
                )
                continue
            except Exception as exc:
                breaker.record_failure()
                metrics.FETCH_ERRORS.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                for s in ask:
                    symbol_circuit.record_failure(provider.name, s)
                logger.warning("%s fetch hatasi, sonraki kaynaga dusuluyor: %s", provider.name, exc)
                continue

            # H2/HIGH-4: seans acikken bayat bar (dunku/eski veri noktasi)
            # dondüren sembolleri "hic gelmemis" say -- provider Quote
            # ÜRETMEMIS gibi davran, sonraki kaynaga (gapfill) dusulsun.
            # Kapsama/circuit muhasebesi (asagida) bu filtrelenmis fetched'e
            # gore hesaplanir. `bar_time` TERCIH EDILIR (guard'in asil
            # sozlesmesi -- bkz. models.py): isyatirim/tradingview artik
            # exchange_time'i HIC doldurmuyor (yalnizca bar_time); bu
            # kaynaklar icin exchange_time'a bakmaya devam etmek guard'i
            # kalici olarak devre disi birakirdi (HIC tetiklenmezdi, cunku
            # is_stale_bar(None,...)=False). yahoo_chart gibi ikisi de ayni
            # olan kaynaklarda fark etmez.
            guard_dropped: set[str] = set()
            for s in [
                s for s, q in fetched.items() if is_stale_bar(q.bar_time or q.exchange_time, moment)
            ]:
                guard_dropped.add(s)
                guard_dropped_candidates.setdefault(s, fetched[s])
                metrics.STALE_BAR_SKIPPED.labels(provider=provider.name).inc()
                # LOW: mesaj eskiden hep "seans acik" diyordu -- MEDIUM-6'dan beri
                # guard kapali piyasada da (son islem gunune gore) tetiklenebiliyor.
                logger.warning(
                    "%s bayat bar: sembol=%s bar_time=%s exchange_time=%s (simdi=%s, %s) "
                    "— quote atlandi",
                    provider.name,
                    s,
                    fetched[s].bar_time,
                    fetched[s].exchange_time,
                    moment.isoformat(),
                    "seans acik" if market_open_now else "seans kapali (son islem gunu kiyasi)",
                )
                del fetched[s]

            # HIGH-1/HIGH-4: seans acikken HICBIR zaman damgasi (ne bar_time
            # ne exchange_time) URETEMEYEN kaynak da ayni kuraldan muaf
            # olmamali -- canli olay: TradingView eskiden damgasiz oldugu
            # icin guard'dan tamamen kaciyor, 2 yillik bayat bir fiyati "taze"
            # diye servis ediyordu. Damgasiz veri seans icinde guvenilmez.
            # Koşul HER IKI alanin da None olmasini arar (yalniz exchange_time
            # kontrolu, artik exchange_time hic doldurmayan isyatirim/
            # tradingview icin bar_time taze olsa bile HER TURDA yanlislikla
            # tetiklenirdi -- feed seans icinde fiilen tek kaynaga inerdi).
            for s in [
                s
                for s, q in fetched.items()
                if market_open_now and q.exchange_time is None and q.bar_time is None
            ]:
                guard_dropped.add(s)
                guard_dropped_candidates.setdefault(s, fetched[s])
                metrics.MISSING_EXCHANGE_TIME.labels(provider=provider.name).inc()
                logger.warning(
                    "%s zaman damgasi yok (ne bar_time ne exchange_time): sembol=%s (seans acik) "
                    "— quote atlandi",
                    provider.name,
                    s,
                )
                del fetched[s]

            # MEDIUM-7/HIGH-1: guard_dropped'in ASK'in TAMAMINI yuttugu
            # (hicbir sey hayatta kalmadigi) BATCH -- provider bu sembolleri
            # "cevapladi" ama tumu politika geregi (bayat/damgasiz) elendi.
            # symbol_circuit bunu basarisizlik SAYMAZ (MEDIUM-3). Bu batch'in
            # sonucu (HENUZ cycle biriktiricisine YAZILMAZ -- ancak asagida
            # merge edilir; bkz. begin_cycle/end_cycle docstring'i).
            batch_fully_dropped[provider.name] = bool(guard_dropped) and not fetched

            if is_cooldown_probe and not batch_fully_dropped[provider.name]:
                # HIGH-2(b): prob BASARILI oldu -- cooldown turun sonunu
                # beklemeden ANINDA kalkar, feed derhal iyilesir.
                self._guard_cooldown_until.pop(provider.name, None)
                metrics.PROVIDER_GUARD_COOLDOWN.labels(provider=provider.name).set(0)
                logger.warning(
                    "%s cooldown probu BASARILI -- cooldown aninda kaldirildi", provider.name
                )

            if self._coverage_ok(fetched, ask):
                breaker.record_success()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1)
            elif gapfill:
                if batch_fully_dropped[provider.name]:
                    # HIGH-1(b): `ask`'in TAMAMI guard'la (bayat-bar/damgasiz)
                    # dustuyse bu "sembol yoklugu" degil GUARD'IN KENDISIDIR --
                    # record_success() YAZILMAZ (breaker/PROVIDER_UP durumu
                    # DEGISTIRILMEZ). Aksi halde kitlesel guard-dusmesi
                    # provider'i yanlislikla "saglikli" gosterirdi -- bu,
                    # store.fresh_ratio'nun `updated_at`'e (cache YAZIM ani,
                    # gercek veri yasi degil) bakmasiyla birlesince fail-open
                    # sirasinda HICBIR alarmin calmadigi review bulgusuydu.
                    logger.debug(
                        "%s: ask'in tamami guard'la dustu -- breaker durumu degistirilmedi",
                        provider.name,
                    )
                else:
                    # Gapfill'de fallback yalnizca onceki kaynaklarin bulamadigi (zor /
                    # illikit) sembolleri sorar; dusuk kapsama provider sagligini DEGIL
                    # sembol yoklugunu gosterir. Yanit geldi (exception yok) -> saglikli
                    # say, circuit'i actirma. Gercek coku exception yolunda yakalanir.
                    # Aksi halde illikit semboller fallback'leri bosuna devre disi birakip
                    # legitim eksik sembollerin kapsamasini dusururdu.
                    breaker.record_success()
                    metrics.PROVIDER_UP.labels(provider=provider.name).set(1)
            else:
                hit = sum(1 for s in ask if s in fetched)
                ratio = hit / len(ask) if ask else 0.0
                breaker.record_failure()
                metrics.FETCH_PARTIAL.labels(provider=provider.name).inc()
                metrics.PROVIDER_UP.labels(provider=provider.name).set(1 if breaker.healthy else 0)
                logger.warning(
                    "%s kismi yanit: %d/%d sembol (%%%.0f, esik %%%.0f)",
                    provider.name,
                    hit,
                    len(ask),
                    ratio * 100,
                    min_cov * 100,
                )

            for s in ask:
                if s in fetched:
                    symbol_circuit.record_success(provider.name, s)
                elif s in guard_dropped:
                    # MEDIUM-3: guard'in dusurdugu (bayat bar / damgasiz) sembol
                    # provider'in veri VEREMEDIGI anlamina gelmez -- "bugun
                    # islem gormedi/damga yok" bir politika karari. HATA
                    # yazilirsa 3 turda sembol_circuit acilir, gec islem goren
                    # bir hisse damgasi duzelince bile 300sn atlanmis olur.
                    continue
                else:
                    symbol_circuit.record_failure(provider.name, s)

            for s, q in fetched.items():
                if s in result:
                    continue
                if self._is_sane(s, q, previous):
                    result[s] = q
                else:
                    rejected.setdefault(s, []).append(q)
            remaining = [s for s in symbols if s not in result]

            if not gapfill and result:
                break

        for s, candidates in rejected.items():
            if s in result:
                continue
            escaped = self._try_escape(s, candidates)
            if escaped is not None:
                result[s] = escaped

        # HIGH-3: bu batch'in TUR biriktiricisine katkisi -- yalniz
        # count_toward_cooldown=True VE aktif bir cycle varsa (bkz.
        # begin_cycle/end_cycle). On-demand cagrilar (varsayilan False)
        # hicbir zaman buraya girmez -- fail-open TUR muhasebesi (HIGH-3b)
        # ve provider guard-drop streak'i yalniz updater'in yapilandirilmis
        # dongusunden beslenir.
        if count_toward_cooldown and self._cycle_fully_dropped is not None:
            cycle_fully_dropped = self._cycle_fully_dropped
            for provider_name, full_guard_drop in batch_fully_dropped.items():
                prev = cycle_fully_dropped.get(provider_name, True)
                cycle_fully_dropped[provider_name] = prev and full_guard_drop
            if self._cycle_fresh_symbols is not None:
                self._cycle_fresh_symbols.update(result.keys())
            if self._cycle_guard_dropped_candidates is not None:
                for s, q in guard_dropped_candidates.items():
                    if s not in result:
                        self._cycle_guard_dropped_candidates[s] = q
            if previous and self._cycle_previous is not None:
                self._cycle_previous.update(previous)

        return result

    async def fetch_history(self, symbol: str, period: str, interval: str) -> HistoryResponse:
        for provider, breaker in self._providers:
            if not provider.supports_history:
                continue
            if not breaker.allow():
                continue
            try:
                result = await asyncio.wait_for(
                    provider.fetch_history(symbol, period, interval),
                    settings.provider_fetch_timeout,
                )
                if result.bars:
                    breaker.record_success()
                    return result
                breaker.record_failure()
            except TimeoutError:
                # NOT: dis iptal (CancelledError) burada YAKALANMAZ — quote
                # yolundaki asyncio.wait_for ile ayni koruma (bkz. fetch_quotes).
                breaker.record_failure()
                logger.warning(
                    "%s history zaman asimi (%.0f sn), sonraki kaynaga dusuluyor",
                    provider.name,
                    settings.provider_fetch_timeout,
                )
                continue
            except Exception as exc:
                breaker.record_failure()
                logger.warning("%s history hatasi: %s", provider.name, exc)
                continue
        return HistoryResponse(symbol=symbol, period=period, interval=interval, bars=[])


aggregator = Aggregator()
