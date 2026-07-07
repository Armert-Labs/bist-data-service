"""Piyasa saati testleri (deterministik; now parametresiyle)."""

from datetime import datetime

from app.market import TR_TZ, is_market_open, market_state


def _dt(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TR_TZ)


def test_open_weekday_midday():
    # 2026-07-06 Pazartesi 12:00
    assert is_market_open(_dt(2026, 7, 6, 12, 0)) is True
    assert market_state(_dt(2026, 7, 6, 12, 0)) == "OPEN"


def test_closed_on_weekend():
    # 2026-07-05 Pazar, 2026-07-04 Cumartesi
    assert is_market_open(_dt(2026, 7, 5, 12, 0)) is False
    assert is_market_open(_dt(2026, 7, 4, 12, 0)) is False
    assert market_state(_dt(2026, 7, 5, 12, 0)) == "CLOSED"


def test_closed_before_open_and_after_close():
    assert is_market_open(_dt(2026, 7, 6, 9, 30)) is False
    assert is_market_open(_dt(2026, 7, 6, 19, 0)) is False


def test_session_boundaries_inclusive():
    assert is_market_open(_dt(2026, 7, 6, 10, 0)) is True  # acilis
    assert is_market_open(_dt(2026, 7, 6, 18, 15)) is True  # kapanis siniri
    assert is_market_open(_dt(2026, 7, 6, 18, 16)) is False


def test_holiday_closes_market():
    # 2026-10-29 Persembe (Cumhuriyet Bayrami) — tatil listesi verilirse kapali.
    holiday = frozenset({"2026-10-29"})
    assert is_market_open(_dt(2026, 10, 29, 12, 0), holidays=holiday) is False
    assert is_market_open(_dt(2026, 10, 29, 12, 0), holidays=frozenset()) is True


def test_seconds_since_open():
    from app.market import seconds_since_open

    # Market acik: 12:00 -> acilistan (10:00) 7200 sn gecmis.
    assert seconds_since_open(_dt(2026, 7, 6, 12, 0)) == 7200.0
    # Market kapali (Pazar) -> None.
    assert seconds_since_open(_dt(2026, 7, 5, 12, 0)) is None


def test_default_holidays_close_market():
    # Varsayilan tatil listesi (config): Ramazan Bayrami 1. gun, Kurban 2. gun,
    # Cumhuriyet Bayrami 2026 — ucunde de piyasa kapali olmali.
    assert is_market_open(_dt(2026, 3, 20, 12, 0)) is False
    assert is_market_open(_dt(2026, 5, 28, 12, 0)) is False
    assert is_market_open(_dt(2026, 10, 29, 12, 0)) is False
