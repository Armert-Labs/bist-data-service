from app import symbols as sym


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
