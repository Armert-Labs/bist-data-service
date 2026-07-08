"""Yahoo v8 chart provider testleri (respx ile agsiz)."""

import httpx
import respx
from app.providers.yahoo_chart import YahooChartProvider

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/THYAO.IS"


_MARKET_TIME_EPOCH = 1751884200


def _chart_payload(price, prev):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": price,
                        "chartPreviousClose": prev,
                        "regularMarketDayHigh": price + 2,
                        "regularMarketDayLow": price - 2,
                        "regularMarketVolume": 12345,
                        "regularMarketTime": _MARKET_TIME_EPOCH,
                        "currency": "TRY",
                    }
                }
            ]
        }
    }


@respx.mock
async def test_fetch_quotes_parses_meta():
    respx.get(url__startswith=_CHART_URL).mock(
        return_value=httpx.Response(200, json=_chart_payload(334.0, 330.0))
    )
    quotes = await YahooChartProvider().fetch_quotes(["THYAO"])
    q = quotes["THYAO"]
    assert q.price == 334.0
    assert q.previous_close == 330.0
    assert q.change == 4.0
    assert q.change_percent == 1.21
    assert q.volume == 12345
    assert q.source == "yahoo_chart"
    # Gercek borsa islem zamani tasinmali (istemci veri yasini olcebilsin)
    assert q.exchange_time is not None
    assert int(q.exchange_time.timestamp()) == _MARKET_TIME_EPOCH
    assert q.exchange_time.tzinfo is not None


@respx.mock
async def test_fetch_quotes_skips_on_error():
    respx.get(url__startswith=_CHART_URL).mock(return_value=httpx.Response(500))
    quotes = await YahooChartProvider().fetch_quotes(["THYAO"])
    assert quotes == {}


@respx.mock
async def test_fetch_quotes_empty_result():
    respx.get(url__startswith=_CHART_URL).mock(
        return_value=httpx.Response(200, json={"chart": {"result": []}})
    )
    quotes = await YahooChartProvider().fetch_quotes(["THYAO"])
    assert quotes == {}


@respx.mock
async def test_fetch_quotes_uses_range_1d():
    # meta.chartPreviousClose istenen range'den ONCEKI kapanistir; dogru gunluk
    # degisim icin range=1d olmali (gercek dunku kapanis). range=5d/1mo yanlis
    # (gunler/aylar oncesi) previous_close verir -> absurd change_percent.
    route = respx.get(url__startswith=_CHART_URL).mock(
        return_value=httpx.Response(200, json=_chart_payload(334.0, 330.0))
    )
    await YahooChartProvider().fetch_quotes(["THYAO"])
    request = route.calls.last.request
    assert request.url.params.get("range") == "1d"
    assert request.url.params.get("interval") == "1d"


@respx.mock
async def test_change_percent_hits_daily_limit():
    # Gercek ornek (BETAE): price=64.35, dunku kapanis=58.5 -> +%10.0 (BIST tavan).
    respx.get(url__startswith=_CHART_URL).mock(
        return_value=httpx.Response(200, json=_chart_payload(64.35, 58.5))
    )
    quotes = await YahooChartProvider().fetch_quotes(["THYAO"])
    q = quotes["THYAO"]
    assert q.previous_close == 58.5
    assert q.change == 5.85
    assert q.change_percent == 10.0
