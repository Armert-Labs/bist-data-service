"""BIST icin sik gorsel Telegram bot (REST API istemcisi; ayri surec).

Calistirma:  python -m app.telegram_bot

Bot python-telegram-bot KULLANMAZ; ham httpx ile Telegram Bot API'sine konusur
(getUpdates long-poll + sendMessage). Fiyat verisini mevcut BIST REST API'sinden
(http://api:8000) ceker; piyasa takvimi icin app.market'i kullanir.

Guvenlik: token yalnizca ortam degiskeninden (TELEGRAM_BOT_TOKEN) okunur, koda
gomulmez. telegram_enabled=false veya token bos ise main() log basip temiz cikar.

Bicimlendirme fonksiyonlari (sparkline, day_range_bar, fmt_volume, format_*) SAF
ve deterministiktir (agsiz test edilir). Ag yapan parcalar (TelegramClient,
BistApiClient) hata firlatmaz: bir chat/istek basarisiz olsa digerleri etkilenmez.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from .config import settings
from .market import TR_TZ, is_market_open

logger = logging.getLogger("bist-telegram-bot")

# --------------------------------------------------------------------------- #
# Saf bicimlendirme yardimcilari (agsiz, deterministik)
# --------------------------------------------------------------------------- #
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_MID_BLOCK = _SPARK_BLOCKS[len(_SPARK_BLOCKS) // 2]
_BAR_WIDTH = 12

# Premium kart dili: kalin ayirici cizgi ve sabit marka alt-bilgisi.
_DIV = "━━━━━━━━━━━━━━━━━━"
_BRAND = "#️⃣ Armert × Bisteyes"


def _esc(value: Any) -> str:
    """Telegram HTML parse_mode icin ozel karakter kacisi (& < >)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tr1(value: float) -> str:
    """Tek ondalikli Turk stili sayi (nokta -> virgul)."""
    return f"{value:.1f}".replace(".", ",")


def _fmt_price(value: float | None) -> str:
    """Iki ondalikli Turk stili fiyat: 1234.5 -> '1.234,50'."""
    if value is None:
        return "-"
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_signed(value: float | None) -> str:
    """Isaretli Turk stili sayi: pozitife '+' onek, negatif kendi '-' isareti."""
    if value is None:
        return "-"
    body = _fmt_price(abs(value))
    if value > 0:
        return f"+{body}"
    if value < 0:
        return f"-{body}"
    return body


def sparkline(values: list[float | None]) -> str:
    """Deger listesini unicode sparkline'a cevirir (▁▂▃▄▅▆▇█).

    Bos -> "". Tek deger veya min==max -> duz orta blok. Aksi halde her deger
    araligin icinde en yakin bloga oturtulur.
    """
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return _MID_BLOCK * len(nums)
    span = hi - lo
    last = len(_SPARK_BLOCKS) - 1
    out = []
    for v in nums:
        idx = int((v - lo) / span * last + 0.5)
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def day_range_bar(low: float | None, price: float | None, high: float | None) -> str:
    """Gun araligi (low..high) icinde fiyatin yerini gosteren ▓/░ bar.

    Fiyat konumuna kadar dolu (▓), sonrasi bos (░). Gecersiz aralik -> tum bos.
    """
    if low is None or price is None or high is None or high <= low:
        return "░" * _BAR_WIDTH
    ratio = max(0.0, min(1.0, (price - low) / (high - low)))
    pos = int(ratio * (_BAR_WIDTH - 1) + 0.5)
    return "▓" * (pos + 1) + "░" * (_BAR_WIDTH - pos - 1)


def fmt_volume(n: float | None) -> str:
    """Hacmi kisaltir: bin / mn / mr (Turk stili tek ondalik)."""
    if n is None:
        return "-"
    n = float(n)
    a = abs(n)
    if a < 1_000:
        return str(int(n))
    if a < 1_000_000:
        return f"{_tr1(n / 1_000)} bin"
    if a < 1_000_000_000:
        return f"{_tr1(n / 1_000_000)} mn"
    return f"{_tr1(n / 1_000_000_000)} mr"


def _fmt_pct(value: float | None) -> str:
    """Turk stili yuzde govdesi (tam sayida ondalik atilir): 100.0->'100', 98.5->'98,5'."""
    if value is None:
        return "-"
    body = f"{value:.1f}"
    if body.endswith(".0"):
        body = body[:-2]
    return body.replace(".", ",")


def _trend_parts(change: float | None) -> tuple[str, str]:
    """Degisim yonu -> (renk emojisi, ok). Yatay/eksik veri icin ok bos."""
    if change is None or change == 0:
        return ("⚪", "")
    if change > 0:
        return ("🟢", "▲")
    return ("🔴", "▼")


def _now_tr(now: datetime | None) -> datetime:
    """now'i Europe/Istanbul (kalici UTC+3) saatine cevirir; None -> su an."""
    current = now or datetime.now(TR_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TR_TZ)
    return current.astimezone(TR_TZ)


def _fmt_dt(now: datetime | None) -> str:
    """Alt bilgi tarihi: gg.aa.yyyy SS:DD (Europe/Istanbul)."""
    return _now_tr(now).strftime("%d.%m.%Y %H:%M")


def _session_range() -> str:
    """Seans araligi metni ayarlardan (orn. '10:00-18:15'; ciktida en-dash)."""
    o = f"{settings.market_open_hour:02d}:{settings.market_open_minute:02d}"
    c = f"{settings.market_close_hour:02d}:{settings.market_close_minute:02d}"
    return f"{o}–{c}"


def _market_badge(now: datetime | None) -> tuple[str, str]:
    """Piyasa durum rozeti: acikken yesil+'Acik', kapaliyken kirmizi+'Kapali'."""
    return ("🟢", "Açık") if is_market_open(now) else ("🔴", "Kapalı")


def _footer(now: datetime | None, extra: str | None = None) -> list[str]:
    """Ortak alt-bilgi: tarih (+opsiyonel rozet) ve marka satiri."""
    date_line = f"📅 {_fmt_dt(now)}"
    if extra:
        date_line = f"{date_line}   ·   {extra}"
    return [date_line, _BRAND]


def _no_data_card(symbol: str, now: datetime | None) -> str:
    """Veri yoksa nazik 'sembol bulunamadi' karti (HTML)."""
    lines = [
        f"📊 <b>{symbol}</b>",
        _DIV,
        "😔 <b>Sembol bulunamadi</b>",
        "Sembolü kontrol edip tekrar deneyin.",
        _DIV,
        *_footer(now),
    ]
    return "\n".join(lines)


def format_quote_card(
    quote: dict, intraday_points: list[dict] | None = None, now: datetime | None = None
) -> str:
    """Bir hisse icin premium, gorsel anlik kart (HTML)."""
    symbol = _esc(quote.get("symbol", "?"))
    price = quote.get("price")
    if price is None:
        return _no_data_card(symbol, now)

    change = quote.get("change")
    change_pct = quote.get("change_percent")
    low = quote.get("day_low")
    high = quote.get("day_high")

    emoji, arrow = _trend_parts(change)
    head = f"{emoji} {arrow} " if arrow else f"{emoji} "
    badge_emoji, badge_label = _market_badge(now)

    # Degisim hesaplanamamissa (prev None/0) fiyat satirinda "- (-%)" basmayalim.
    price_line = f"💰 <b>{_fmt_price(price)} ₺</b>"
    if change is not None:
        price_line += f"   {head}{_fmt_signed(change)} ({_fmt_signed(change_pct)}%)"

    lines = [
        f"📊 <b>{symbol}</b>",
        _DIV,
        price_line,
        _DIV,
        "📈 <b>Gün İçi</b>",
        f"├ Açılış   <code>{_fmt_price(quote.get('open'))}</code>",
        f"├ Yüksek   <code>{_fmt_price(high)}</code>",
        f"├ Düşük    <code>{_fmt_price(low)}</code>",
        f"└ Hacim    <code>{fmt_volume(quote.get('volume'))}</code>",
        _DIV,
    ]
    lines += _footer(now, f"{badge_emoji} {badge_label}")
    return "\n".join(lines)


def format_market_open(watch_count: int, now: datetime | None = None) -> str:
    """Piyasa acilis otomatik mesaji (sessiz yayin)."""
    lines = [
        "🔔 <b>PİYASA AÇILDI</b>",
        _DIV,
        f"🟢 Seans başladı  ·  {_session_range()}",
        f"📡 <b>{watch_count}</b> hisse izleniyor",
        _DIV,
        "Anlık veri için sembol yazın → <code>THYAO</code>",
        _DIV,
        *_footer(now),
    ]
    return "\n".join(lines)


def _mover_lines(quotes: list[dict]) -> list[str]:
    """Yukselen/dusen listesini agac karakterli (├/└) satirlara cevirir."""
    out: list[str] = []
    last = len(quotes) - 1
    for i, q in enumerate(quotes):
        branch = "└" if i == last else "├"
        pct = q.get("change_percent")
        emoji = "🟢" if (pct or 0) > 0 else ("🔴" if (pct or 0) < 0 else "⚪")
        out.append(f"{branch} {emoji} {_esc(q.get('symbol', '?'))}   {_fmt_signed(pct)}%")
    return out


def format_market_close(
    all_quotes: list[dict], watch_count: int | None = None, now: datetime | None = None
) -> str:
    """Piyasa kapanis ozeti: ilk 3 yukselen + son 3 dusen (izlenen sayi quotes_cached)."""
    quotes = [q for q in all_quotes if q.get("change_percent") is not None]
    quotes.sort(key=lambda q: q["change_percent"], reverse=True)
    gainers = quotes[:3]
    # En cok dusenler: son 3, en negatif once; yukselenlerle cakismasin.
    losers = [q for q in quotes[-3:] if q not in gainers][::-1]
    count = watch_count if watch_count is not None else len(quotes)

    lines = [
        "🔕 <b>PİYASA KAPANDI</b>",
        _DIV,
        f"📡 <b>{count}</b> hisse  ·  seans sonu",
        _DIV,
    ]
    if gainers:
        lines.append("📈 <b>Günün Yükselenleri</b>")
        lines += _mover_lines(gainers)
    if losers:
        if gainers:
            lines.append("")
        lines.append("📉 <b>Günün Düşenleri</b>")
        lines += _mover_lines(losers)
    lines.append(_DIV)
    lines += _footer(now)
    return "\n".join(lines)


def format_status(ready: dict, now: datetime | None = None) -> str:
    """/durum karti: piyasa, izlenen adet, veri tazeligi, aktif kaynak SAYISI.

    Kaynak ADLARI hicbir yerde gecmez; yalnizca saglikli/toplam sayisi verilir.
    """
    open_ = ready.get("market_open")
    badge_emoji = "🟢" if open_ else "🔴"
    badge_label = "Açık" if open_ else "Kapalı"
    cached = ready.get("quotes_cached", 0) or 0
    fresh = ready.get("fresh_pct")
    age = ready.get("last_update_age_seconds")
    providers = ready.get("providers") or {}
    # closed/half_open = deneme yapabilir (saglikli); open = devre kesik.
    healthy = sum(1 for st in providers.values() if st in ("closed", "half_open"))

    lines = [
        "📟 <b>SİSTEM DURUMU</b>",
        _DIV,
        f"{badge_emoji} Piyasa    <b>{badge_label}</b>",
        f"📡 İzlenen   <b>{cached}</b> hisse",
    ]
    if fresh is not None:
        fresh_line = f"⏱️ Tazelik   <b>%{_fmt_pct(fresh)}</b>"
        if age is not None:
            fresh_line += f"  (son {round(age)} sn)"
        lines.append(fresh_line)
    if providers:
        lines.append(f"🔌 Kaynaklar <b>{healthy}/{len(providers)}</b> aktif")
    lines.append(_DIV)
    lines += _footer(now)
    return "\n".join(lines)


def format_welcome(watch_count: int, now: datetime | None = None) -> str:
    """/start karsilama karti (sik cerceveli)."""
    lines = [
        "👋 <b>Hoş geldiniz</b>",
        _DIV,
        "Armert × Bisteyes BIST veri botu.",
        f"📡 <b>{watch_count}</b> hisse anlık izleniyor.",
        _DIV,
        "<b>Komutlar</b>",
        "├ Sembol yazın (<code>THYAO</code>) → anlık kart",
        "├ /durum → sistem durumu",
        "├ /yardim → yardım",
        "└ /stop → bildirimleri kapat",
        _DIV,
        "🔔 Açılış/kapanış otomatik bildirilir.",
        _BRAND,
    ]
    return "\n".join(lines)


def format_help(now: datetime | None = None) -> str:
    """/yardim karti (sik cerceveli komut listesi)."""
    lines = [
        "🤖 <b>YARDIM</b>",
        _DIV,
        "<b>Komutlar</b>",
        "├ <code>THYAO</code> → anlık hisse kartı",
        "├ <code>/hisse THYAO</code> → anlık kart",
        "├ /durum → sistem durumu",
        "├ /start → bildirimleri aç",
        "└ /stop → bildirimleri kapat",
        _DIV,
        "🔔 Açılış/kapanış otomatik bildirilir.",
        _BRAND,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Komut ayristirma (saf)
# --------------------------------------------------------------------------- #
_SYMBOL_RE = re.compile(r"[A-Za-z0-9]{2,6}$")


def parse_command(text: str | None) -> tuple[str, str]:
    """Metni (komut, arguman) ciftine cevirir.

    "/hisse THYAO" -> ("/hisse","THYAO");  "/h GARAN" -> ("/h","GARAN")
    duz "THYAO" (2-6 harf/rakam) -> ("/hisse","THYAO")
    "/start@bot" -> ("/start","");  bilinmeyen/duz metin -> ("", metin)
    """
    text = (text or "").strip()
    if not text:
        return ("", "")
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@", 1)[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        return (cmd, arg)
    token = text.split()[0]
    if _SYMBOL_RE.fullmatch(token):
        return ("/hisse", token.upper())
    return ("", text)


def detect_transition(prev_open: bool | None, now_open: bool) -> str | None:
    """Piyasa durum gecisi: 'open' (kapali->acik), 'close' (acik->kapali), None.

    prev_open None (ilk tur) veya durum degismediyse None -> spam yok.
    """
    if prev_open is None or prev_open == now_open:
        return None
    return "open" if now_open else "close"


# --------------------------------------------------------------------------- #
# Chat kayit defteri (Redis set veya in-memory fallback)
# --------------------------------------------------------------------------- #
class ChatRegistry:
    """Kayitli chat id'leri tutar. Redis verilirse set olarak, aksi halde bellek."""

    def __init__(self, redis: Any = None, key: str | None = None) -> None:
        self._redis = redis
        self._key = key or f"{settings.redis_prefix}:telegram:chats"
        self._mem: set[str] = set()

    async def add(self, chat_id: int | str) -> None:
        cid = str(chat_id)
        if self._redis is not None:
            await self._redis.sadd(self._key, cid)
        else:
            self._mem.add(cid)

    async def remove(self, chat_id: int | str) -> None:
        cid = str(chat_id)
        if self._redis is not None:
            await self._redis.srem(self._key, cid)
        else:
            self._mem.discard(cid)

    async def all(self) -> list[str]:
        if self._redis is not None:
            members = await self._redis.smembers(self._key)
            return sorted(str(m) for m in members)
        return sorted(self._mem)


# --------------------------------------------------------------------------- #
# Telegram Bot API istemcisi (ham httpx; hata firlatmaz)
# --------------------------------------------------------------------------- #
class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        client: httpx.AsyncClient | None = None,
        poll_timeout: int = 30,
    ) -> None:
        self._base = f"{base_url}/bot{token}"
        # Taban 1 sn: TELEGRAM_POLL_TIMEOUT=0 saglikli durumda bile busy-loop
        # (getUpdates aninda doner) uretmesin.
        self._poll_timeout = max(1, poll_timeout)
        # getUpdates long-poll icin okuma zaman asimi poll_timeout'un uzerinde olmali.
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 15)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(
        self, chat_id: int | str, text: str, disable_notification: bool = False
    ) -> bool:
        """Mesaj gonderir (HTML). Basarisizlikta log + False (patlamaz)."""
        try:
            resp = await self._client.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": disable_notification,
                },
            )
            if resp.status_code != 200:
                logger.warning("sendMessage %s -> %s", chat_id, resp.status_code)
                return False
            return bool(resp.json().get("ok"))
        except Exception as exc:  # ag hatasi bir chat'i etkilemesin
            logger.warning("sendMessage hata (%s): %s", chat_id, type(exc).__name__)
            return False

    async def send_chat_action(self, chat_id: int | str, action: str = "typing") -> None:
        with contextlib.suppress(Exception):
            await self._client.post(
                f"{self._base}/sendChatAction", json={"chat_id": chat_id, "action": action}
            )

    async def get_updates(self, offset: int | None = None) -> list[dict]:
        """Long-poll getUpdates. Hata durumunda ISTISNA firlatir (bos liste ile
        karistirilmasin); cagiran (_poll_loop) backoff uygular."""
        payload: dict[str, Any] = {"timeout": self._poll_timeout}
        if offset is not None:
            payload["offset"] = offset
        resp = await self._client.post(f"{self._base}/getUpdates", json=payload)
        resp.raise_for_status()
        return list(resp.json().get("result", []))


# --------------------------------------------------------------------------- #
# BIST REST API istemcisi (bot bir istemcidir; hata firlatmaz)
# --------------------------------------------------------------------------- #
class BistApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=15, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            resp = await self._client.get(f"{self._base}{path}", params=params)
            if resp.status_code == 200:
                return resp.json()
            # /ready basarisizken (503) yapisal govde 'detail' altinda gelir.
            if resp.status_code == 503:
                body = resp.json()
                return body.get("detail", body) if isinstance(body, dict) else None
            return None
        except Exception as exc:
            logger.debug("API GET %s hata: %s", path, type(exc).__name__)
            return None

    async def get_ready(self) -> dict:
        data = await self._get("/ready")
        return data if isinstance(data, dict) else {}

    async def get_symbols_count(self) -> int:
        # "Izlenen hisse" = canli verisi olan sembol sayisi (quotes_cached);
        # dinamik evren genislemesini yansitir. /symbols yalnizca statik taban
        # listesini (daha kucuk) doner, o yuzden ikincil fallback'tir.
        ready = await self.get_ready()
        cached = int(ready.get("quotes_cached", 0) or 0)
        if cached:
            return cached
        data = await self._get("/symbols")
        if isinstance(data, dict) and isinstance(data.get("count"), int):
            return data["count"]
        return 0

    async def get_quote(self, symbol: str) -> dict | None:
        data = await self._get(f"/quote/{symbol}")
        return data if isinstance(data, dict) else None

    async def get_intraday(self, symbol: str) -> list[dict]:
        data = await self._get(f"/intraday/{symbol}")
        if isinstance(data, dict):
            return list(data.get("points", []))
        return []

    async def get_all(self, sort: str = "change_percent", order: str = "desc") -> list[dict]:
        data = await self._get("/all", params={"sort": sort, "order": order})
        if isinstance(data, dict):
            return list(data.get("quotes", []))
        return []


# --------------------------------------------------------------------------- #
# Bot: komut yonlendirme + piyasa gecis yayini
# --------------------------------------------------------------------------- #
class Bot:
    def __init__(self, tg: Any, api: Any, registry: ChatRegistry) -> None:
        self._tg = tg
        self._api = api
        self._registry = registry
        self._offset: int | None = None
        self._prev_open: bool | None = None

    def _is_allowed(self, chat_id: int | str) -> bool:
        allowed = settings.telegram_allowed_chats
        return not allowed or str(chat_id) in allowed

    async def handle_message(self, chat_id: int, text: str) -> None:
        cmd, arg = parse_command(text)

        # Guvenlik: TUM komutlar yalniz izinli chat'lerden (admin grubu/kisi) islenir.
        # Bos allowlist = herkes. Yoksa rastgele biri /hisse ile veri cekemez.
        if not self._is_allowed(chat_id):
            await self._tg.send_message(chat_id, "⛔ Bu bot yalnizca izinli kullanicilar icindir.")
            return

        if cmd == "/start":
            await self._registry.add(chat_id)
            count = await self._api.get_symbols_count()
            await self._tg.send_message(chat_id, format_welcome(count))
            return

        if cmd == "/stop":
            await self._registry.remove(chat_id)
            await self._tg.send_message(chat_id, "👋 Bildirimler kapatildi. Gorusmek uzere!")
            return

        if cmd in ("/yardim", "/help"):
            await self._tg.send_message(chat_id, format_help())
            return

        if cmd == "/durum":
            ready = await self._api.get_ready()
            await self._tg.send_message(chat_id, format_status(ready))
            return

        if cmd in ("/hisse", "/h"):
            symbol = arg.strip().upper().split()[0] if arg.strip() else ""
            if not symbol:
                await self._tg.send_message(
                    chat_id, "Kullanim: <code>/hisse THYAO</code> ya da duz <code>THYAO</code>"
                )
                return
            await self._send_quote_card(chat_id, symbol)
            return

        # Bilinmeyen komut / duz metin -> kisa yonlendirme.
        await self._tg.send_message(
            chat_id,
            "Anlamadim. 🤔 Sembol yazin (orn. <code>THYAO</code>) ya da <code>/yardim</code>.",
        )

    async def _send_quote_card(self, chat_id: int, symbol: str) -> None:
        await self._tg.send_chat_action(chat_id, "typing")
        quote = await self._api.get_quote(symbol)
        if not quote or quote.get("price") is None:
            await self._tg.send_message(
                chat_id, format_quote_card({"symbol": symbol, "price": None})
            )
            return
        intraday = await self._api.get_intraday(symbol)
        await self._tg.send_message(chat_id, format_quote_card(quote, intraday))

    async def poll_once(self) -> None:
        updates = await self._tg.get_updates(self._offset)
        for upd in updates:
            uid = upd.get("update_id")
            if uid is None:  # bozuk update offset'i bozmasin/patlatmasin
                continue
            self._offset = uid + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text")
            if chat_id is None or not text:
                continue
            try:
                await self.handle_message(chat_id, text)
            except Exception:
                logger.exception("Mesaj isleme hatasi (chat=%s)", chat_id)

    async def market_tick(self, now: Any = None) -> str | None:
        """Piyasa durumunu kontrol eder; gecis varsa TUM chat'lere sessiz yayin."""
        now_open = is_market_open(now)
        transition = detect_transition(self._prev_open, now_open)
        self._prev_open = now_open
        if transition == "open":
            count = await self._api.get_symbols_count()
            await self._broadcast(format_market_open(count))
        elif transition == "close":
            quotes = await self._api.get_all(sort="change_percent", order="desc")
            count = await self._api.get_symbols_count()
            await self._broadcast(format_market_close(quotes, count))
        return transition

    async def _broadcast(self, text: str) -> None:
        # Acilis/kapanis "sessiz bir sekilde" (disable_notification=True).
        for chat_id in await self._registry.all():
            await self._tg.send_message(chat_id, text, disable_notification=True)

    async def run(self) -> None:
        # Ilk turda mevcut durumu "onceki" kabul et (baslar baslamaz spam atma).
        self._prev_open = is_market_open()
        await asyncio.gather(self._poll_loop(), self._market_loop())

    async def _poll_loop(self) -> None:
        # Backoff: getUpdates hata verirse (ag/Telegram 5xx/429) busy-loop yerine
        # ustel geri cekilme; basaride sifirlanir. Beklenmedik hata sureci oldurmez.
        backoff = 1.0
        while True:
            try:
                await self.poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Poll dongusu hatasi; %.0f sn sonra tekrar", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _market_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.telegram_market_poll_seconds)
            try:
                await self.market_tick()
            except Exception:
                logger.exception("Piyasa gecis kontrolu hatasi")


# --------------------------------------------------------------------------- #
# Giris noktasi
# --------------------------------------------------------------------------- #
async def _run() -> None:
    redis = None
    if settings.redis_enabled:
        try:
            import redis.asyncio as aioredis

            redis = aioredis.from_url(settings.redis_url, decode_responses=True)
            await redis.ping()
        except Exception as exc:
            logger.warning(
                "Redis'e baglanilamadi (%s); bellek moduna dusuluyor.", type(exc).__name__
            )
            redis = None

    registry = ChatRegistry(redis=redis)
    tg = TelegramClient(settings.telegram_bot_token, poll_timeout=settings.telegram_poll_timeout)
    api = BistApiClient(settings.telegram_api_url, settings.telegram_api_key)
    bot = Bot(tg, api, registry)
    logger.info("Telegram bot basladi (API=%s).", settings.telegram_api_url)
    try:
        await bot.run()
    finally:
        await tg.aclose()
        await api.aclose()
        if redis is not None:
            await redis.aclose()


def main() -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    # httpx/httpcore DEBUG'da istek URL'ini basar; getUpdates/sendMessage URL'i
    # token icerdiginden (/bot<TOKEN>/...) log'a sizmasin.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not settings.telegram_enabled or not settings.telegram_bot_token:
        logger.info(
            "Telegram bot devre disi (TELEGRAM_ENABLED=%s, token %s). Temiz cikiliyor.",
            settings.telegram_enabled,
            "var" if settings.telegram_bot_token else "yok",
        )
        return
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Telegram bot durduruldu.")


if __name__ == "__main__":
    main()
