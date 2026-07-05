"""BIST piyasa saatleri yardimcilari.

Turkiye 2016'dan beri kalici UTC+3 kullanir (yaz saati uygulanmaz), bu yuzden
tzdata bagimliligina gerek kalmadan sabit ofset kullaniyoruz.

Resmi tatiller MARKET_HOLIDAYS ortam degiskeniyle verilir (virgulle ayrilmis
ISO tarihler, orn. "2026-10-29,2027-01-01"). Liste bos ise yalnizca hafta ici
+ saat araligi kontrol edilir.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

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
