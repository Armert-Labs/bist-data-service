"""Is Yatirim parse mantigi testleri (AGSIZ).

Gercek Is Yatirim `value[]` response formatiyla parse_quote'u dogrular.
Boylece kodun dogrulugu, ag erisiminden bagimsiz olarak kanitlanir.
"""

from datetime import UTC, datetime

import httpx
import respx
from app.providers.isyatirim import IsYatirimProvider, parse_quote

_BASE = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/HisseTekil"

# Is Yatirim HisseTekil endpoint'inin gercek alan adlariyla ornek yanit.
SAMPLE_ROWS = [
    {
        "HGDG_TARIH": "01-07-2026",
        "HGDG_KAPANIS": 330.0,
        "HGDG_ACILIS": 328.0,
        "HGDG_MAX": 332.0,
        "HGDG_MIN": 327.0,
        "HGDG_HACIM": 1000000,
    },
    {
        "HGDG_TARIH": "02-07-2026",
        "HGDG_KAPANIS": 334.0,
        "HGDG_ACILIS": 331.0,
        "HGDG_MAX": 335.0,
        "HGDG_MIN": 330.0,
        "HGDG_HACIM": 1200000,
    },
]


def test_parse_quote_basic():
    q = parse_quote("THYAO", SAMPLE_ROWS)
    assert q is not None
    assert q.symbol == "THYAO"
    assert q.price == 334.0
    assert q.previous_close == 330.0
    assert q.change == 4.0
    assert q.change_percent == 1.21  # (334-330)/330*100
    assert q.day_high == 335.0
    assert q.day_low == 330.0
    assert q.volume == 1200000
    assert q.source == "isyatirim"
    assert q.delayed is True
    # H2: son bar'in tarihi (HGDG_TARIH) exchange_time'a tasinmali -- aksi halde
    # seans-ici bayat-bar guard'i (is_stale_bar) bu kaynak icin hicbir zaman
    # devreye giremez (exchange_time=None her zaman "taze" sayilirdi).
    assert q.exchange_time is not None
    assert q.exchange_time.astimezone(UTC).date().isoformat() == "2026-07-02"


def test_parse_quote_empty_returns_none():
    assert parse_quote("THYAO", []) is None
    assert parse_quote("THYAO", [{"HGDG_KAPANIS": None}]) is None


def test_parse_quote_single_row_has_no_previous():
    q = parse_quote("GARAN", [SAMPLE_ROWS[1]])
    assert q is not None
    assert q.price == 334.0
    assert q.previous_close is None
    assert q.change is None
    assert q.change_percent is None


def test_parse_quote_skips_null_close_rows():
    rows = [{"HGDG_KAPANIS": None}, SAMPLE_ROWS[0], SAMPLE_ROWS[1]]
    q = parse_quote("AKBNK", rows)
    assert q.price == 334.0
    assert q.previous_close == 330.0


def test_parse_quote_sorts_unordered_rows_by_date():
    """Is Yatirim value[] dizisini kronolojik sirali dondurmez (ayni istekte
    bile sira degisir). Parse en guncel gunluk cubugu TARIHE gore secmeli,
    dizideki son elemani degil. Gercek GARAN vakasi: dizide son eleman eski
    bir cubuk (29-06) oldugu icin yanlis 'son fiyat' ve /validate'te %3 sahte
    sapma doguyordu."""
    scrambled = [
        {"HGDG_TARIH": "02-07-2026", "HGDG_KAPANIS": 138.6},
        {"HGDG_TARIH": "07-07-2026", "HGDG_KAPANIS": 134.4},  # gercek en guncel
        {"HGDG_TARIH": "06-07-2026", "HGDG_KAPANIS": 133.7},
        {"HGDG_TARIH": "29-06-2026", "HGDG_KAPANIS": 137.3},  # dizide SON ama en eski
    ]
    q = parse_quote("GARAN", scrambled)
    assert q.price == 134.4  # 07-07 kapanisi (rows[-1]=29-06=137.3 DEGIL)
    assert q.previous_close == 133.7  # 06-07 kapanisi (tarihe gore bir onceki)
    assert q.exchange_time.astimezone(UTC).date().isoformat() == "2026-07-07"


def test_parse_quote_unparseable_date_not_picked_as_latest():
    """Tarihi bozuk/eksik satir en guncel cubuk olarak secilmemeli (en eskiye itilir)."""
    rows = [
        {"HGDG_TARIH": "06-07-2026", "HGDG_KAPANIS": 100.0},
        {"HGDG_TARIH": "BOZUK", "HGDG_KAPANIS": 999.0},
        {"HGDG_TARIH": "07-07-2026", "HGDG_KAPANIS": 110.0},
    ]
    q = parse_quote("XU", rows)
    assert q.price == 110.0  # 07-07, bozuk-tarihli 999.0 degil


def test_parse_quote_clamps_future_exchange_time_to_now():
    # MEDIUM-2: bar BUGUNE ait ve seans devam ediyorsa (henuz kapanis saati
    # gelmedi), market_close_time GELECEK bir damga uretir -- bu, okuma
    # aninda hesaplanan data_age_seconds'i negatif yapardi. exchange_time
    # "simdi"yi asamaz; en fazla "simdi" kadar guncel olabilir.
    rows = [{"HGDG_TARIH": "13-07-2026", "HGDG_KAPANIS": 100.0}]
    now = datetime(2026, 7, 13, 11, 0, tzinfo=UTC)  # TR 14:00 -- kapanistan (18:15) ONCE
    q = parse_quote("THYAO", rows, now=now)
    assert q is not None
    assert q.exchange_time == now


def test_parse_quote_does_not_clamp_past_exchange_time():
    rows = [{"HGDG_TARIH": "10-07-2026", "HGDG_KAPANIS": 100.0}]
    now = datetime(2026, 7, 13, 11, 0, tzinfo=UTC)
    q = parse_quote("THYAO", rows, now=now)
    assert q is not None
    assert q.exchange_time < now  # gercek kapanis zamani, klemplenmedi


@respx.mock
async def test_fetch_quotes_via_http():
    respx.get(url__startswith=_BASE).mock(
        return_value=httpx.Response(200, json={"ok": True, "value": SAMPLE_ROWS})
    )
    quotes = await IsYatirimProvider().fetch_quotes(["THYAO"])
    assert quotes["THYAO"].price == 334.0
    assert quotes["THYAO"].source == "isyatirim"


@respx.mock
async def test_fetch_history_via_http():
    respx.get(url__startswith=_BASE).mock(
        return_value=httpx.Response(200, json={"ok": True, "value": SAMPLE_ROWS})
    )
    res = await IsYatirimProvider().fetch_history("THYAO", "1mo", "1d")
    assert len(res.bars) == 2
    assert res.bars[-1].close == 334.0


@respx.mock
async def test_fetch_quotes_http_error_returns_empty():
    respx.get(url__startswith=_BASE).mock(return_value=httpx.Response(500))
    quotes = await IsYatirimProvider().fetch_quotes(["THYAO"])
    assert quotes == {}
