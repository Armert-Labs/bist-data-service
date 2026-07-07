import httpx
import respx
from app import symbols as sym
from app.providers.tradingview import _SCANNER_URL


def test_normalize_strips_and_uppercases():
    assert sym.normalize(" thyao ") == "THYAO"
    assert sym.normalize("thyao.is") == "THYAO"
    assert sym.normalize("GARAN.IS") == "GARAN"


def test_valid_symbol():
    assert sym.is_valid_symbol("THYAO")
    assert sym.is_valid_symbol("GARAN.IS")
    assert not sym.is_valid_symbol("THYAO;DROP")
    assert not sym.is_valid_symbol("")
    assert not sym.is_valid_symbol("TOOLONGSYM")
    assert not sym.is_valid_symbol("a b")


def test_yahoo_conversion():
    assert sym.to_yahoo("THYAO") == "THYAO.IS"
    assert sym.to_yahoo("thyao") == "THYAO.IS"
    assert sym.from_yahoo("THYAO.IS") == "THYAO"


def test_watchlist_nonempty_and_sorted():
    wl = sym.default_watchlist()
    assert len(wl) > 100
    assert wl == sorted(wl)
    assert "THYAO" in wl and "GARAN" in wl


def test_default_watchlist_is_static_base_when_no_extra(override_settings):
    override_settings(extra_symbols=[])
    wl = sym.default_watchlist()
    assert set(wl) == set(sym.BIST_SYMBOLS)  # statik liste HER ZAMAN taban


def test_default_watchlist_unions_extra_symbols_lossless(override_settings):
    # EXTRA_SYMBOLS eklenir ama statik semboller ASLA kaybolmaz (kayipsizlik).
    override_settings(extra_symbols=["YYY1", "ZZZ2"])
    wl = sym.default_watchlist()
    assert set(sym.BIST_SYMBOLS).issubset(set(wl))
    assert "YYY1" in wl and "ZZZ2" in wl
    assert wl == sorted(wl)


def test_default_watchlist_ignores_invalid_extra(override_settings):
    override_settings(extra_symbols=["!!!", "a b", "TOOLONGSYM"])
    wl = sym.default_watchlist()
    assert set(wl) == set(sym.BIST_SYMBOLS)


def _universe_payload(symbols):
    return {"data": [{"s": f"BIST:{s}", "d": [s]} for s in symbols]}


@respx.mock
async def test_fetch_universe_accepts_large_list(override_settings):
    override_settings(symbol_universe_min_count=3)
    syms = ["THYAO", "GARAN", "AKBNK", "NEWCO", "FRSH1"]
    respx.post(_SCANNER_URL).mock(return_value=httpx.Response(200, json=_universe_payload(syms)))
    got = await sym.fetch_universe()
    assert set(got) == set(syms)
    assert got == sorted(got)
    # Gecerlilik filtresi: gecersiz bicimler elenir.


@respx.mock
async def test_fetch_universe_guard_rejects_small_list(override_settings):
    # Sonuc min_count altinda ise BOS don (guard: bozuk/kismi evren yutulmaz).
    override_settings(symbol_universe_min_count=400)
    respx.post(_SCANNER_URL).mock(
        return_value=httpx.Response(200, json=_universe_payload(["THYAO", "GARAN"]))
    )
    got = await sym.fetch_universe()
    assert got == []


@respx.mock
async def test_fetch_universe_filters_invalid_tickers(override_settings):
    override_settings(symbol_universe_min_count=2)
    payload = {
        "data": [
            {"s": "BIST:THYAO", "d": ["THYAO"]},
            {"s": "BIST:GARAN", "d": ["GARAN"]},
            {"s": "BIST:TOOLONGSYM", "d": ["x"]},  # gecersiz (uzun) -> elenir
            {"s": "NASDAQ:AAPL", "d": ["x"]},  # BIST disi ama bicim gecerli
            {"d": ["x"]},  # s yok -> atlanir
        ]
    }
    respx.post(_SCANNER_URL).mock(return_value=httpx.Response(200, json=payload))
    got = await sym.fetch_universe()
    assert "THYAO" in got and "GARAN" in got
    assert "TOOLONGSYM" not in got
    assert "AAPL" not in got  # BIST disi borsa sizmamali


@respx.mock
async def test_fetch_universe_http_error_returns_empty(override_settings):
    override_settings(symbol_universe_min_count=1)
    respx.post(_SCANNER_URL).mock(return_value=httpx.Response(500))
    got = await sym.fetch_universe()
    assert got == []
