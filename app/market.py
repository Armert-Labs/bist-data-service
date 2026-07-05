"""BIST piyasa saatleri yardimcilari.

Turkiye 2016'dan beri kalici UTC+3 kullanir (yaz saati uygulanmaz), bu yuzden
tzdata bagimliligina gerek kalmadan sabit ofset kullaniyoruz.

Not (MVP kisiti): Resmi tatiller kontrol edilmez; yalnizca hafta ici + saat
araligina bakilir. Tatil takvimi gerekiyorsa buraya eklenmelidir.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from .config import settings

TR_TZ = timezone(timedelta(hours=settings.market_tz_offset_hours))


def now_tr() -> datetime:
    return datetime.now(TR_TZ)


def is_market_open(now: Optional[datetime] = None) -> bool:
    current = now or now_tr()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TR_TZ)
    current = current.astimezone(TR_TZ)

    # 5 = Cumartesi, 6 = Pazar
    if current.weekday() >= 5:
        return False

    open_t = time(settings.market_open_hour, settings.market_open_minute)
    close_t = time(settings.market_close_hour, settings.market_close_minute)
    return open_t <= current.time() <= close_t


def market_state(now: Optional[datetime] = None) -> str:
    return "OPEN" if is_market_open(now) else "CLOSED"
