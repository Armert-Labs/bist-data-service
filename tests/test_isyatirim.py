"""Is Yatirim parse mantigi testleri (AGSIZ).

Gercek Is Yatirim `value[]` response formatiyla parse_quote'u dogrular.
Boylece kodun dogrulugu, ag erisiminden bagimsiz olarak kanitlanir.
"""

from app.providers.isyatirim import parse_quote

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
