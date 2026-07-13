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


def test_is_stale_bar_none_exchange_time_never_stale():
    from app.market import is_stale_bar

    # exchange_time saglamayan kaynak (orn. TradingView) icin bu kural
    # uygulanamaz; guard'siz gecer (baska bir mekanizma varsa o karar verir).
    assert is_stale_bar(None, _dt(2026, 7, 6, 12, 0)) is False


def test_is_stale_bar_yesterday_while_open_is_stale():
    from app.market import is_stale_bar

    # 2026-07-06 Pazartesi seans ici; bar 2026-07-03 Cuma kapanisina ait.
    yesterday_bar = _dt(2026, 7, 3, 18, 15)
    assert is_stale_bar(yesterday_bar, _dt(2026, 7, 6, 12, 0)) is True


def test_is_stale_bar_todays_bar_while_open_is_fresh():
    from app.market import is_stale_bar

    today_bar = _dt(2026, 7, 6, 10, 5)
    assert is_stale_bar(today_bar, _dt(2026, 7, 6, 12, 0)) is False


def test_is_stale_bar_old_bar_while_closed_is_legit():
    from app.market import is_stale_bar

    # Piyasa kapaliyken son kapanis mesru veridir; bayat SAYILMAZ.
    friday_close = _dt(2026, 7, 3, 18, 15)
    assert is_stale_bar(friday_close, _dt(2026, 7, 5, 12, 0)) is False  # Pazar, kapali


def test_is_stale_bar_accepts_utc_exchange_time():
    from datetime import UTC, datetime

    from app.market import is_stale_bar

    # exchange_time UTC'de tutulur (Quote modeli); TR gunune donusturme
    # dogru calismali (UTC 21:00 cuma = TR 00:00 cumartesi -> hala 03-07 gunu DEGIL,
    # asagida acik ornek: UTC 03-07 15:15 = TR 03-07 18:15, dunku TR kapanisi).
    yesterday_close_utc = datetime(2026, 7, 3, 15, 15, tzinfo=UTC)
    assert is_stale_bar(yesterday_close_utc, _dt(2026, 7, 6, 12, 0)) is True


def test_is_stale_bar_naive_exchange_time_independent_of_system_tz():
    # LOW-b: exchange_time tzinfo tasimiyorsa (beklenmedik/savunma amacli durum)
    # naive datetime.astimezone() Python'da SUNUCUNUN YEREL SISTEM saatini
    # varsayar -- ayni naive deger, sunucu TZ'sine gore FARKLI bir gune
    # yuvarlanabilir. Quote modelinin sozlesmesi UTC'dir; naive girdi HER
    # ZAMAN UTC sayilmali, yani sonuc sistem TZ'sinden BAGIMSIZ olmali. Bunu
    # dogrulamak icin sistem TZ'sini iki UC noktaya (New York / Kiritimati,
    # ~18 saat fark) gecici olarak degistirip sonucun degismedigini kontrol
    # ederiz (ambient +03 gelistirme ortaminda tesadufen "doğru" gorunmesin).
    import os
    import time as time_mod
    from datetime import datetime

    from app.market import is_stale_bar

    # Gece yarisina yakin bir naive deger + ertesi gun (Sali) "simdi": iki asiri
    # TZ yorumu (New York / Kiritimati) FARKLI gunlere yuvarlanacak sekilde
    # secildi (biri "dun", digeri "bugun" -> stale/degil FARKLI cikar) --
    # bug varsa test bunu yakalar, fix'liyken ikisi de UTC varsayimina gore
    # AYNI (dogru) sonucu vermeli.
    naive = datetime(2026, 7, 6, 23, 30)  # tzinfo YOK
    now = _dt(2026, 7, 7, 12, 0)
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time_mod.tzset()
        result_ny = is_stale_bar(naive, now)

        os.environ["TZ"] = "Pacific/Kiritimati"
        time_mod.tzset()
        result_kiritimati = is_stale_bar(naive, now)
    finally:
        if original_tz is not None:
            os.environ["TZ"] = original_tz
        else:
            os.environ.pop("TZ", None)
        time_mod.tzset()
    assert result_ny == result_kiritimati
