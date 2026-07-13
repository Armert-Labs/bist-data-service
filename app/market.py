"""BIST piyasa saatleri yardimcilari.

Turkiye 2016'dan beri kalici UTC+3 kullanir (yaz saati uygulanmaz), bu yuzden
tzdata bagimliligina gerek kalmadan sabit ofset kullaniyoruz.

Resmi tatiller MARKET_HOLIDAYS ortam degiskeniyle verilir (virgulle ayrilmis
ISO tarihler, orn. "2026-10-29,2027-01-01"). Liste bos ise yalnizca hafta ici
+ saat araligi kontrol edilir.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

from .config import settings

TR_TZ = timezone(timedelta(hours=settings.market_tz_offset_hours))

# Ayarlar sureci boyunca sabittir; seti bir kez kur.
_HOLIDAYS: frozenset[str] = frozenset(settings.market_holidays)


def now_tr() -> datetime:
    return datetime.now(TR_TZ)


def _to_tr(now: datetime | None) -> datetime:
    current = now or now_tr()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TR_TZ)
    return current.astimezone(TR_TZ)


def is_market_open(now: datetime | None = None, holidays: frozenset[str] | None = None) -> bool:
    current = _to_tr(now)

    # 5 = Cumartesi, 6 = Pazar
    if current.weekday() >= 5:
        return False

    effective_holidays = _HOLIDAYS if holidays is None else holidays
    if current.date().isoformat() in effective_holidays:
        return False

    open_t = time(settings.market_open_hour, settings.market_open_minute)
    close_t = time(settings.market_close_hour, settings.market_close_minute)
    return open_t <= current.time() <= close_t


def seconds_since_open(now: datetime | None = None) -> float | None:
    """Market acik ise bugunku acilistan bu yana gecen saniye; kapali ise None.

    Bayatlik (staleness) hesabinda acilis sonrasi tolerans penceresi icin
    kullanilir: acilistan hemen sonra veri hala onceki seanstan olabilir ve
    guncelleyicinin ilk turunu tamamlamasi zaman alir.
    """
    current = _to_tr(now)
    if not is_market_open(current):
        return None
    open_dt = current.replace(
        hour=settings.market_open_hour,
        minute=settings.market_open_minute,
        second=0,
        microsecond=0,
    )
    return (current - open_dt).total_seconds()


def market_state(now: datetime | None = None) -> str:
    return "OPEN" if is_market_open(now) else "CLOSED"


def last_trading_day(now: datetime | None = None, holidays: frozenset[str] | None = None) -> date:
    """En son (bugun dahil) islem gunu -- hafta sonu/tatilse GERIYE dogru en
    yakin islem gunune yuvarlar.

    Kapali-piyasa guard kontrolu icin (bkz. is_stale_bar) "son mesru kapanis
    hangi gune ait olmali" sorusuna cevap verir.
    """
    d = _to_tr(now).date()
    effective_holidays = _HOLIDAYS if holidays is None else holidays
    while d.weekday() >= 5 or d.isoformat() in effective_holidays:
        d -= timedelta(days=1)
    return d


def is_stale_bar(exchange_time: datetime | None, now: datetime | None = None) -> bool:
    """Bir veri noktasi (exchange_time/bar_time) beklenen gune ait degilse True.

    Provider'lar (orn. Is Yatirim, TradingView) gunluk/bar bazli veri dondurur;
    seans icinde dunku/daha eski bir cubugu "canli fiyat" gibi sunmak yaniltici
    olur (bkz. H2). exchange_time yoksa bayat SAYILMAZ (baska bir mekanizma --
    orn. eksik-damga guard'i -- karar verir).

    Seans ACIKKEN beklenen gun BUGUNDUR. Seans KAPALIYKEN de bar yasi kontrol
    edilir (MEDIUM-6): beklenen gun SON ISLEM GUNUDUR (hafta sonu/tatil
    dahil) -- aksi halde donmus/eski bir kaynak (orn. 2 hafta oncesi bir bar)
    "kapanis fiyati" gibi gecip alarmsiz kalirdi.
    """
    if exchange_time is None:
        return False
    # LOW-b: Quote.exchange_time/bar_time sozlesmesi UTC'dir. Naive (tzinfo'suz)
    # bir deger gelirse -- beklenmedik ama savunma amacli -- datetime.astimezone()
    # SUNUCUNUN YEREL SISTEM saat dilimini varsayardi; bu, sunucu TZ'sine gore
    # yanlis gun karsilastirmasina yol acabilirdi. Naive girdi HER ZAMAN UTC
    # sayilir (sunucu TZ'sinden bagimsiz).
    if exchange_time.tzinfo is None:
        exchange_time = exchange_time.replace(tzinfo=UTC)
    bar_date = exchange_time.astimezone(TR_TZ).date()
    expected_day = _to_tr(now).date() if is_market_open(now) else last_trading_day(now)
    return bar_date < expected_day


def market_close_time(day: date) -> datetime:
    """Verilen gunun BIST kapanis anini UTC olarak doner.

    Gunluk EOD cubuk dondüren kaynaklarin (orn. Is Yatirim) tek kapanis
    fiyatini Quote.exchange_time'da ifade edebilmesi icin kullanilir.
    """
    closing = datetime.combine(day, time(settings.market_close_hour, settings.market_close_minute))
    return closing.replace(tzinfo=TR_TZ).astimezone(UTC)
