"""Telegram bot testleri (AGSIZ: respx/fakeredis/memory; canli network YOK).

Saf bicimlendirme fonksiyonlari deterministik dogrulanir; ChatRegistry memory
ve fakeredis modunda; TelegramClient respx ile (dogru URL + govde); komut
ayristirma ve piyasa gecis dedeksiyonu. Ayrica kodda gercek Telegram token'i
bulunmadigi guard'lanir.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
import respx
from app import telegram_bot as tb

# --------------------------------------------------------------------------- #
# sparkline
# --------------------------------------------------------------------------- #


def test_sparkline_empty():
    assert tb.sparkline([]) == ""
    assert tb.sparkline([None, None]) == ""


def test_sparkline_single_is_flat_mid():
    # Tek deger: min==max, orta blok.
    assert tb.sparkline([5]) == "▅"


def test_sparkline_flat_all_equal():
    assert tb.sparkline([3, 3, 3]) == "▅▅▅"


def test_sparkline_low_maps_low_high_maps_high():
    # En dusuk -> ilk blok, en yuksek -> son blok.
    assert tb.sparkline([1, 2]) == "▁█"


def test_sparkline_multiple_spread():
    s = tb.sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(s) == 8
    assert s[0] == "▁"
    assert s[-1] == "█"


def test_sparkline_skips_none():
    assert tb.sparkline([1, None, 2]) == "▁█"


# --------------------------------------------------------------------------- #
# day_range_bar
# --------------------------------------------------------------------------- #


def test_day_range_bar_price_at_low():
    bar = tb.day_range_bar(10.0, 10.0, 20.0)
    assert bar[0] == "▓"
    assert bar.count("▓") == 1
    assert "░" in bar


def test_day_range_bar_price_at_high():
    bar = tb.day_range_bar(10.0, 20.0, 20.0)
    assert set(bar) == {"▓"}


def test_day_range_bar_price_mid_has_both():
    bar = tb.day_range_bar(10.0, 15.0, 20.0)
    assert "▓" in bar and "░" in bar


def test_day_range_bar_degenerate_range():
    # Gecersiz aralik (high<=low veya None): duz bos bar, patlamaz.
    assert set(tb.day_range_bar(10.0, 10.0, 10.0)) == {"░"}
    assert set(tb.day_range_bar(None, 10.0, 20.0)) == {"░"}


# --------------------------------------------------------------------------- #
# fmt_volume
# --------------------------------------------------------------------------- #


def test_fmt_volume():
    assert tb.fmt_volume(None) == "-"
    assert tb.fmt_volume(500) == "500"
    assert tb.fmt_volume(1500) == "1,5 bin"
    assert tb.fmt_volume(44008702) == "44,0 mn"
    assert tb.fmt_volume(2_500_000_000) == "2,5 mr"


# --------------------------------------------------------------------------- #
# HTML kacis
# --------------------------------------------------------------------------- #


def test_html_escape():
    assert tb._esc("a<b>&c") == "a&lt;b&gt;&amp;c"


# --------------------------------------------------------------------------- #
# format_quote_card
# --------------------------------------------------------------------------- #

_QUOTE_UP = {
    "symbol": "THYAO",
    "price": 334.0,
    "previous_close": 333.25,
    "change": 0.75,
    "change_percent": 0.22,
    "open": 335.25,
    "day_high": 335.75,
    "day_low": 330.75,
    "volume": 44008702,
    "source": "yahoo",
    "market_state": "OPEN",
    "updated_at": "2026-07-06T09:30:00Z",
}


# Kapali piyasa saati (acilis 10:00'dan once) — kart alt-bilgisi rozeti deterministik olsun.
_NOW_CLOSED = datetime(2026, 7, 6, 8, 32, tzinfo=tb.TR_TZ)
# Acik piyasa saati (hafta ici, seans icinde).
_NOW_OPEN = datetime(2026, 7, 6, 11, 0, tzinfo=tb.TR_TZ)


def test_format_quote_card_up():
    card = tb.format_quote_card(
        _QUOTE_UP, [{"p": 330.0}, {"p": 331.5}, {"p": 334.0}], now=_NOW_CLOSED
    )
    assert "📊 <b>THYAO</b>" in card
    assert "🟢" in card and "▲" in card  # yukselis rozeti (emoji + ok)
    assert "+0,22%" in card
    assert "334,00" in card
    assert "<code>" in card  # Gun Ici degerleri code blogunda
    assert "44,0 mn" in card
    assert "Grafik" not in card  # grafik bolumu kaldirildi
    assert "▓" not in card and "░" not in card  # gun-araligi cubugu yok
    assert "━━━" in card  # premium ayirici cizgi
    assert "📅 06.07.2026 08:32" in card  # alt bilgi tarihi (Europe/Istanbul)
    assert "Armert × Bisteyes" in card  # marka satiri
    assert "🔴 Kapalı" in card  # 08:32 -> seans oncesi


def test_format_quote_card_open_badge():
    # Seans icinde (11:00) alt-bilgi rozeti acik (yesil) olmali.
    card = tb.format_quote_card(_QUOTE_UP, [], now=_NOW_OPEN)
    assert "🟢 Açık" in card


def test_format_quote_card_down():
    q = dict(_QUOTE_UP, change=-1.5, change_percent=-0.45, price=331.0)
    card = tb.format_quote_card(q, [], now=_NOW_CLOSED)
    assert "🔴" in card and "▼" in card
    assert "-0,45%" in card


def test_format_quote_card_flat():
    q = dict(_QUOTE_UP, change=0.0, change_percent=0.0)
    card = tb.format_quote_card(q, [], now=_NOW_CLOSED)
    assert "⚪" in card  # yatay


def test_format_quote_card_no_data():
    q = {"symbol": "ZZZZ", "price": None}
    card = tb.format_quote_card(q, None, now=_NOW_CLOSED)
    assert "<b>ZZZZ</b>" in card
    assert "bulunamadi" in card.lower()
    assert "Armert × Bisteyes" in card


# --------------------------------------------------------------------------- #
# format_market_open / close / status
# --------------------------------------------------------------------------- #


def test_format_market_open():
    msg = tb.format_market_open(507, now=_NOW_OPEN)
    assert "AÇILDI" in msg
    assert "507" in msg
    assert "🔔" in msg
    assert "10:00–18:15" in msg  # seans araligi ayarlardan
    assert "📅 06.07.2026 11:00" in msg
    assert "Armert × Bisteyes" in msg


_CLOSE_QUOTES = [
    {"symbol": "AAA", "change": 3, "change_percent": 3.0},
    {"symbol": "BBB", "change": 2, "change_percent": 2.0},
    {"symbol": "CCC", "change": 1, "change_percent": 1.5},
    {"symbol": "DDD", "change": -0.5, "change_percent": -0.5},
    {"symbol": "EEE", "change": -1, "change_percent": -1.2},
    {"symbol": "FFF", "change": -2, "change_percent": -2.5},
]


def test_format_market_close():
    msg = tb.format_market_close(_CLOSE_QUOTES, 616, now=_NOW_CLOSED)
    assert "KAPANDI" in msg
    assert "AAA" in msg and "FFF" in msg
    assert "+3,00%" in msg
    assert "-2,50%" in msg
    # Izlenen adet quotes_cached'ten (616), hareketli liste uzunlugundan degil.
    assert "616" in msg
    # En cok dusen (FFF) once listelenmeli.
    assert msg.index("FFF") < msg.index("DDD")
    # Agac karakterleri kullanilmali.
    assert "├" in msg and "└" in msg


def test_format_market_close_count_fallback():
    # watch_count verilmezse hareketli-liste uzunluguna duser (geriye uyum).
    msg = tb.format_market_close(_CLOSE_QUOTES)
    assert "<b>6</b>" in msg


def test_format_status():
    ready = {
        "market_open": True,
        "quotes_cached": 507,
        "fresh_pct": 98.5,
        "last_update_age_seconds": 12.3,
        "providers": {
            "yahoo": "closed",
            "yahoo_chart": "closed",
            "isyatirim": "open",
            "tradingview": "half_open",
        },
    }
    s = tb.format_status(ready, now=_NOW_CLOSED)
    assert "Açık" in s
    assert "507" in s
    assert "98,5" in s
    assert "3/4" in s  # aktif kaynak (closed+half_open)
    assert "Kaynaklar" in s  # sayi etiketi (ad DEGIL)


def test_format_status_fresh_whole_number():
    # Tam yuzde ondalik atar: %100 (%,100,0 degil).
    s = tb.format_status({"market_open": False, "quotes_cached": 5, "fresh_pct": 100.0})
    assert "%100" in s
    assert "100,0" not in s


def test_format_status_closed():
    s = tb.format_status(
        {"market_open": False, "quotes_cached": 0, "providers": {}}, now=_NOW_CLOSED
    )
    assert "Kapalı" in s


def test_format_welcome():
    msg = tb.format_welcome(616)
    assert "Hoş geldiniz" in msg
    assert "616" in msg
    assert "Armert × Bisteyes" in msg
    assert "├" in msg and "└" in msg  # agac karakterli komut listesi


def test_format_help():
    msg = tb.format_help()
    assert "YARDIM" in msg
    assert "/durum" in msg
    assert "/stop" in msg
    assert "━━━" in msg


# --------------------------------------------------------------------------- #
# Icerik kurallari: kaynak adi YOK, gecikme ibaresi YOK
# --------------------------------------------------------------------------- #


def test_no_source_name_or_delay_in_any_output():
    ready = {
        "market_open": True,
        "quotes_cached": 616,
        "fresh_pct": 98.5,
        "last_update_age_seconds": 4,
        "providers": {
            "yahoo": "closed",
            "yahoo_chart": "closed",
            "isyatirim": "open",
            "tradingview": "half_open",
        },
    }
    outputs = [
        tb.format_quote_card(_QUOTE_UP, [{"p": 330.0}, {"p": 334.0}], now=_NOW_OPEN),
        tb.format_quote_card({"symbol": "ZZZZ", "price": None}, now=_NOW_CLOSED),
        tb.format_market_open(616, now=_NOW_OPEN),
        tb.format_market_close(_CLOSE_QUOTES, 616, now=_NOW_CLOSED),
        tb.format_status(ready, now=_NOW_CLOSED),
        tb.format_welcome(616),
        tb.format_help(),
    ]
    for out in outputs:
        low = out.lower()
        # Kaynak ADLARI hicbir ciktida gecmemeli.
        for name in ("yahoo", "isyatirim", "tradingview", "yahoo_chart"):
            assert name not in low, f"kaynak adi sizdi: {name!r} -> {out!r}"
        # Eski 'Kaynak: <ad>' satiri kalmamali.
        assert "kaynak:" not in low
        # Gecikme ibaresi hicbir yerde gecmemeli.
        assert "gecikme" not in low
        assert "15 dk" not in low
        assert "dakika gecikmeli" not in low


# --------------------------------------------------------------------------- #
# parse_command
# --------------------------------------------------------------------------- #


def test_parse_command_hisse():
    assert tb.parse_command("/hisse THYAO") == ("/hisse", "THYAO")
    assert tb.parse_command("/h GARAN") == ("/h", "GARAN")


def test_parse_command_plain_symbol():
    assert tb.parse_command("THYAO") == ("/hisse", "THYAO")
    assert tb.parse_command("thyao") == ("/hisse", "THYAO")


def test_parse_command_start_stop():
    assert tb.parse_command("/start") == ("/start", "")
    assert tb.parse_command("/stop") == ("/stop", "")
    # Grup icinde @botname eki temizlenir.
    assert tb.parse_command("/start@bist_bot") == ("/start", "")


def test_parse_command_unknown():
    assert tb.parse_command("merhaba dunya") == ("", "merhaba dunya")
    assert tb.parse_command("") == ("", "")


def test_detect_transition():
    assert tb.detect_transition(False, True) == "open"
    assert tb.detect_transition(True, False) == "close"
    assert tb.detect_transition(True, True) is None
    assert tb.detect_transition(False, False) is None
    # Ilk tur (onceki durum bilinmiyor) -> mesaj yok.
    assert tb.detect_transition(None, True) is None


# --------------------------------------------------------------------------- #
# ChatRegistry (memory + fakeredis)
# --------------------------------------------------------------------------- #


async def test_chat_registry_memory():
    reg = tb.ChatRegistry()
    await reg.add(123)
    await reg.add(456)
    await reg.add(123)  # idempotent
    assert set(await reg.all()) == {"123", "456"}
    await reg.remove(123)
    assert set(await reg.all()) == {"456"}


async def test_chat_registry_fakeredis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reg = tb.ChatRegistry(redis=r, key="bist:telegram:chats")
    await reg.add(789)
    await reg.add(789)
    assert await reg.all() == ["789"]
    await reg.remove(789)
    assert await reg.all() == []
    await r.aclose()


# --------------------------------------------------------------------------- #
# TelegramClient (respx)
# --------------------------------------------------------------------------- #


@respx.mock
async def test_telegram_client_send_message():
    route = respx.post("https://api.telegram.org/botTESTTOKEN/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    client = tb.TelegramClient("TESTTOKEN")
    ok = await client.send_message(42, "<b>merhaba</b>")
    await client.aclose()
    assert ok is True
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == 42
    assert body["text"] == "<b>merhaba</b>"
    assert body["parse_mode"] == "HTML"


@respx.mock
async def test_telegram_client_send_message_error_no_raise():
    respx.post("https://api.telegram.org/botTESTTOKEN/sendMessage").mock(
        return_value=httpx.Response(500)
    )
    client = tb.TelegramClient("TESTTOKEN")
    ok = await client.send_message(42, "merhaba")  # patlamamali
    await client.aclose()
    assert ok is False


@respx.mock
async def test_get_updates_raises_on_error():
    # Hata (5xx) sessizce [] donmemeli — poll dongusu backoff yapabilsin diye
    # ayirt edilebilir bir istisna firlatmali.
    respx.post("https://api.telegram.org/botTESTTOKEN/getUpdates").mock(
        return_value=httpx.Response(500)
    )
    client = tb.TelegramClient("TESTTOKEN")
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_updates(offset=0)
    await client.aclose()


def test_poll_timeout_has_floor():
    # TELEGRAM_POLL_TIMEOUT=0 saglikli durumda bile busy-loop uretmesin.
    client = tb.TelegramClient("TESTTOKEN", poll_timeout=0)
    assert client._poll_timeout >= 1


async def test_poll_once_skips_update_without_id():
    tg, api, reg = _FakeTelegram(), _FakeApi(), tb.ChatRegistry()
    bot = tb.Bot(tg, api, reg)

    async def bad_updates(offset=None):
        return [{"message": {"chat": {"id": 5}, "text": "/start"}}]  # update_id YOK

    tg.get_updates = bad_updates
    before = bot._offset
    await bot.poll_once()  # KeyError ile patlamamali
    assert bot._offset == before  # id'siz update offset'i bozmamali


async def test_poll_loop_backs_off_on_error(monkeypatch):
    tg, api, reg = _FakeTelegram(), _FakeApi(), tb.ChatRegistry()
    bot = tb.Bot(tg, api, reg)

    async def boom():
        raise RuntimeError("ag hatasi")

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "poll_once", boom)
    monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await bot._poll_loop()
    assert sleeps == [1.0, 2.0, 4.0]  # ustel geri cekilme (busy-loop yok)


@respx.mock
async def test_telegram_client_get_updates():
    respx.post("https://api.telegram.org/botTESTTOKEN/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [{"update_id": 10, "message": {"chat": {"id": 5}, "text": "/start"}}],
            },
        )
    )
    client = tb.TelegramClient("TESTTOKEN")
    updates = await client.get_updates(offset=0)
    await client.aclose()
    assert len(updates) == 1
    assert updates[0]["update_id"] == 10


# --------------------------------------------------------------------------- #
# Bot dispatch (fake client'lar; agsiz)
# --------------------------------------------------------------------------- #


class _FakeTelegram:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, disable_notification=False):
        self.sent.append((chat_id, text))
        return True

    async def send_chat_action(self, chat_id, action="typing"):
        return None


class _FakeApi:
    def __init__(self, quote=None, intraday=None, ready=None):
        self._quote = quote
        self._intraday = intraday or []
        self._ready = ready or {"quotes_cached": 3, "market_open": True, "providers": {}}

    async def get_quote(self, symbol):
        return self._quote

    async def get_intraday(self, symbol):
        return self._intraday

    async def get_ready(self):
        return self._ready

    async def get_symbols_count(self):
        return 3

    async def get_all(self, sort="change_percent", order="desc"):
        return []


async def test_bot_start_registers_chat():
    tg, api, reg = _FakeTelegram(), _FakeApi(), tb.ChatRegistry()
    bot = tb.Bot(tg, api, reg)
    await bot.handle_message(999, "/start")
    assert "999" in set(await reg.all())
    assert tg.sent and tg.sent[-1][0] == 999


async def test_bot_stop_removes_chat():
    tg, api, reg = _FakeTelegram(), _FakeApi(), tb.ChatRegistry()
    await reg.add(999)
    bot = tb.Bot(tg, api, reg)
    await bot.handle_message(999, "/stop")
    assert "999" not in set(await reg.all())


async def test_bot_hisse_sends_card():
    tg = _FakeTelegram()
    api = _FakeApi(quote=_QUOTE_UP, intraday=[{"p": 330.0}, {"p": 334.0}])
    bot = tb.Bot(tg, api, tb.ChatRegistry())
    await bot.handle_message(1, "THYAO")
    assert tg.sent
    assert "THYAO" in tg.sent[-1][1]


async def test_bot_hisse_not_found():
    tg = _FakeTelegram()
    api = _FakeApi(quote=None)
    bot = tb.Bot(tg, api, tb.ChatRegistry())
    await bot.handle_message(1, "/hisse ZZZZ")
    assert tg.sent
    assert "bulunamadi" in tg.sent[-1][1].lower()


async def test_bot_unknown_command_guidance():
    tg = _FakeTelegram()
    bot = tb.Bot(tg, _FakeApi(), tb.ChatRegistry())
    await bot.handle_message(1, "merhaba dunya")
    assert tg.sent
    assert "/yardim" in tg.sent[-1][1]


async def test_bot_allowed_chats_blocks_stranger(override_settings):
    override_settings(telegram_allowed_chats=["111"])
    tg, reg = _FakeTelegram(), tb.ChatRegistry()
    bot = tb.Bot(tg, _FakeApi(), reg)
    await bot.handle_message(222, "/start")  # izinli degil
    assert "222" not in set(await reg.all())


# --------------------------------------------------------------------------- #
# Token gizliligi guard
# --------------------------------------------------------------------------- #


def test_no_hardcoded_telegram_token():
    """Kod dosyasinda gercek bir Telegram token'i (\\d+:AA...) bulunmamali."""
    src = Path(tb.__file__).read_text(encoding="utf-8")
    assert re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", src) is None
    test_src = Path(__file__).read_text(encoding="utf-8")
    assert re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", test_src) is None


def test_format_quote_card_change_none_omits_change():
    # Degisim hesaplanamadiginda (change None) '- (-%)' basilmamali.
    q = {"symbol": "YENI", "price": 12.5, "change": None, "change_percent": None}
    card = tb.format_quote_card(q, [], now=_NOW_CLOSED)
    assert "12,50 ₺" in card
    assert "(-%)" not in card
    assert "- (-" not in card
