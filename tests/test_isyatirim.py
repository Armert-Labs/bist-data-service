"""Is Yatirim parse mantigi testleri (AGSIZ).

Gercek Is Yatirim `value[]` response formatiyla parse_quote'u dogrular.
Boylece kodun dogrulugu, ag erisiminden bagimsiz olarak kanitlanir.
"""

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
