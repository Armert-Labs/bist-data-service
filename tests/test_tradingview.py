"""TradingView scanner provider testleri (respx ile agsiz).

Parse mantigi saf fonksiyonla (parse_quote) ag erisiminden bagimsiz dogrulanir;
HTTP yolu respx ile mock'lanir. Canli network YOK.
"""

import json

import httpx
import pytest
import respx
from app.providers.tradingview import (
    _COLUMNS,
    _SCANNER_URL,
    TradingViewProvider,
    parse_quote,
)

# Kolon sirasi: ["lp","change_abs","change","volume","open","high","low","close","time"]
# (prev_close_price kolonu /scan'de her zaman null geliyordu -- M1; previous_close
# artik ayni yanittaki change_abs'den turetiliyor: price - change_abs. `time`
# review'da (HIGH-1) bar'in gercek zamanini verdigi canli olarak dogrulandi.)
_THYAO_EPOCH = 1783922400  # 13 Tem 2026 (review kanit ornegi)
_THYAO_ROW = {
    "s": "BIST:THYAO",
    "d": [334.0, 0.75, 0.22, 44008702, 335.25, 335.75, 330.75, 334.0, _THYAO_EPOCH],
}
_GARAN_ROW = {
    "s": "BIST:GARAN",
    "d": [128.5, -1.5, -1.15, 9000000, 130.0, 130.5, 128.0, 128.0, _THYAO_EPOCH],
}


def _scan_payload(*rows):
    return {"totalCount": len(rows), "data": list(rows)}


def test_parse_quote_falls_back_to_close_when_lp_null():
    # HIGH-1 review kaniti: lp bu /scan uc noktasinda SEANS ACIKKEN DE null
    # geliyor (yalniz "piyasa kapali" degil) -- TV fiilen hep close alanini
    # dolduruyor, price bu yuzden ona dusuyor. change_abs/change de bu
    # durumda hep null geliyor -> previous_close turetilemez.
    row = {"s": "BIST:THYAO", "d": [None, None, None, 60645389, 346, 355.5, 345.25, 347.0]}
    q = parse_quote(row)
    assert q is not None
    assert q.price == 347.0  # close'a dustu
    assert q.volume == 60645389
    assert q.day_high == 355.5
    assert q.previous_close is None  # change_abs yok -> turetilemedi, ACIKCA None


def test_parse_quote_zero_volume_preserved():
    # Hacim 0 (islem gormemis) 'bilinmiyor' (None) ile karistirilmamali.
    row = {"s": "BIST:ZERO", "d": [10.0, 0.0, 0.0, 0, 10.0, 10.0, 10.0, 10.0]}
    q = parse_quote(row)
    assert q is not None
    assert q.volume == 0


def test_parse_quote_basic():
    q = parse_quote(_THYAO_ROW)
    assert q is not None
    assert q.symbol == "THYAO"
    assert q.price == 334.0
    assert q.change == 0.75
    assert q.change_percent == 0.22
    assert q.volume == 44008702
    assert q.open == 335.25
    assert q.day_high == 335.75
    assert q.day_low == 330.75
    # previous_close artik prev_close_price kolonundan DEGIL, ayni yanittaki
    # change_abs'den turetiliyor: price - change_abs = 334.0 - 0.75 = 333.25.
    assert q.previous_close == 333.25
    assert q.source == "tradingview"
    assert q.delayed is True
    assert q.updated_at is not None
    # HIGH-1: `time` kolonu bar'in gercek zamanini verir; guard (is_stale_bar)
    # bu kaynak icin de calisabilsin diye exchange_time'a tasinmali.
    assert q.exchange_time is not None
    assert int(q.exchange_time.timestamp()) == _THYAO_EPOCH


def test_parse_quote_missing_time_leaves_exchange_time_none():
    # `time` saglanmiyorsa exchange_time acikca None kalir (baska bir alandan
    # tahmin edilmez).
    row = {"s": "BIST:THYAO", "d": [334.0, 0.75, 0.22, 1, 2, 3, 4, 334.0]}
    q = parse_quote(row)
    assert q is not None
    assert q.exchange_time is None


def test_parse_quote_derives_previous_close_from_change_abs():
    # M1 fix: previous_close artik price - change_abs olarak hesaplanir
    # (prev_close_price kolonu TradingView /scan'de calismiyordu).
    row = {"s": "BIST:GARAN", "d": [128.5, -1.5, -1.15, 9000000, 130.0, 130.5, 128.0, 128.0]}
    q = parse_quote(row)
    assert q is not None
    assert q.previous_close == 130.0  # 128.5 - (-1.5)


def test_parse_quote_change_abs_null_leaves_previous_close_none(caplog):
    # change_abs saglanmiyorsa previous_close BASKA BIR KAYNAKTAN turetilmez;
    # acikca None birakilir + loglanir (sessiz eksiklik yerine gorunur eksiklik).
    row = {"s": "BIST:THYAO", "d": [334.0, None, None, 1, 2, 3, 4, 334.0]}
    with caplog.at_level("DEBUG", logger="app.providers.tradingview"):
        q = parse_quote(row)
    assert q is not None
    assert q.previous_close is None
    assert q.change is None
    assert q.change_percent is None
    assert any("previous_close" in r.message for r in caplog.records)


def test_parse_quote_strips_bist_prefix():
    q = parse_quote({"s": "BIST:GARAN", "d": [50.0, 0, 0, 1, 50, 51, 49, 50]})
    assert q is not None
    assert q.symbol == "GARAN"


def test_parse_quote_missing_fields_returns_none():
    assert parse_quote({}) is None
    assert parse_quote({"s": "BIST:THYAO"}) is None  # d yok
    assert parse_quote({"d": [1, 2, 3]}) is None  # s yok


def test_parse_quote_null_price_returns_none():
    # lp VE close ikisi de null -> fiyat hic elde edilemez.
    row = {"s": "BIST:THYAO", "d": [None, 0.75, 0.22, 1, 2, 3, 4, None]}
    assert parse_quote(row) is None


def test_parse_quote_invalid_symbol_returns_none():
    # Gecersiz sembol bicimi (nokta/uzun) elenir.
    row = {"s": "BIST:TOOLONGSYM", "d": [10.0, 0, 0, 1, 2, 3, 4, 5]}
    assert parse_quote(row) is None


@respx.mock
async def test_fetch_quotes_single_symbol():
    route = respx.post(_SCANNER_URL).mock(
        return_value=httpx.Response(200, json=_scan_payload(_THYAO_ROW))
    )
    quotes = await TradingViewProvider().fetch_quotes(["THYAO"])
    assert route.called
    assert set(quotes) == {"THYAO"}
    assert quotes["THYAO"].price == 334.0
    assert quotes["THYAO"].source == "tradingview"


@respx.mock
async def test_fetch_quotes_multi_symbol_single_post():
    route = respx.post(_SCANNER_URL).mock(
        return_value=httpx.Response(200, json=_scan_payload(_THYAO_ROW, _GARAN_ROW))
    )
    quotes = await TradingViewProvider().fetch_quotes(["THYAO", "GARAN"])
    # Coklu sembol TEK POST'ta gonderilir (batch verimlilik).
    assert route.call_count == 1
    assert set(quotes) == {"THYAO", "GARAN"}
    assert quotes["GARAN"].change_percent == -1.15
    # Govde gercekten BIST:XXX ticker'lari + beklenen kolonlari icermeli.
    sent = route.calls[0].request
    body = json.loads(sent.content)
    assert body["symbols"]["tickers"] == ["BIST:THYAO", "BIST:GARAN"]
    assert body["columns"] == _COLUMNS


@respx.mock
async def test_fetch_quotes_http_500_raises():
    # Contract: hata FIRLAT (aggregator circuit breaker'i tetikler).
    respx.post(_SCANNER_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await TradingViewProvider().fetch_quotes(["THYAO"])


@respx.mock
async def test_fetch_quotes_empty_data_returns_empty():
    respx.post(_SCANNER_URL).mock(
        return_value=httpx.Response(200, json={"totalCount": 0, "data": []})
    )
    quotes = await TradingViewProvider().fetch_quotes(["THYAO"])
    assert quotes == {}


@respx.mock
async def test_fetch_quotes_invalid_json_raises():
    respx.post(_SCANNER_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(json.JSONDecodeError):
        await TradingViewProvider().fetch_quotes(["THYAO"])


async def test_fetch_quotes_invalid_symbol_filtered_no_request():
    # Gecersiz sembol hic POST atmadan elenir (respx route yok => istek olsa patlar).
    quotes = await TradingViewProvider().fetch_quotes(["!!!", "a b"])
    assert quotes == {}


async def test_fetch_history_returns_empty_bars():
    # Scanner /scan zaman serisi vermez; history bos doner (durust davranis).
    res = await TradingViewProvider().fetch_history("THYAO", "1mo", "1d")
    assert res.symbol == "THYAO"
    assert res.bars == []
