"""Fiyat dogruluk kontrolu (CLI).

Bizim API'nin dondurdugu fiyatlari (birincil kaynak: Yahoo) BAGIMSIZ ikinci bir
kaynakla (Is Yatirim) karsilastirir. Iki bagimsiz kaynagin uyusmasi, fiyatin
dogrulugu icin guclu bir gostergedir.

Kullanim:
  python scripts/check_prices.py                 # varsayilan likit hisseler
  python scripts/check_prices.py THYAO GARAN     # secili hisseler
  API_URL=http://localhost:8000 python scripts/check_prices.py
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_URL = os.environ.get("API_URL", "http://localhost:8000")
# Bagimsiz referans kaynak. yahoo_chart bu ortamda calisir; isyatirim yalnizca
# Turkiye'den erisilebilir.  REF_PROVIDER=isyatirim ile degistirilebilir.
REF_PROVIDER = os.environ.get("REF_PROVIDER", "yahoo_chart")

_PROVIDER_CLASS = {
    "yahoo_chart": ("app.providers.yahoo_chart", "YahooChartProvider"),
    "isyatirim": ("app.providers.isyatirim", "IsYatirimProvider"),
}


def _make_reference_provider():
    module_path, class_name = _PROVIDER_CLASS.get(REF_PROVIDER, _PROVIDER_CLASS["yahoo_chart"])
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


DEFAULT_SYMBOLS = [
    "THYAO",
    "GARAN",
    "AKBNK",
    "ASELS",
    "SISE",
    "KCHOL",
    "TUPRS",
    "BIMAS",
    "EREGL",
    "FROTO",
]

# Esik degerleri (%)
OK_THRESHOLD = 1.0  # < %1 fark: mukemmel uyum
WARN_THRESHOLD = 5.0  # < %5: kabul edilebilir; >= %5: incelenmeli


async def fetch_ours(symbols: list[str]) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{API_URL}/quotes", params={"symbols": ",".join(symbols)})
        resp.raise_for_status()
        return resp.json().get("quotes", {})


async def main(symbols: list[str]) -> int:
    print(f"API: {API_URL}  |  Referans: {REF_PROVIDER}  |  Kontrol: {len(symbols)} sembol\n")

    try:
        ours = await fetch_ours(symbols)
    except Exception as exc:
        print(f"HATA: Bizim API'ye erisilemedi: {exc}")
        return 2

    isy = await _make_reference_provider().fetch_quotes(symbols)

    header = f"{'SEMBOL':<8}{'BIZIM':>14}{'REFERANS':>14}{'FARK%':>10}  DURUM"
    print(header)
    print("-" * len(header))

    max_dev = 0.0
    compared = 0
    missing = 0

    for sym in symbols:
        o = ours.get(sym)
        i = isy.get(sym)
        op = o.get("price") if o else None
        ip = i.price if i else None

        if op is not None and ip not in (None, 0):
            dev = abs(op - ip) / ip * 100.0
            max_dev = max(max_dev, dev)
            compared += 1
            status = (
                "OK" if dev < OK_THRESHOLD else ("~ UYARI" if dev < WARN_THRESHOLD else "!! SAPMA")
            )
            print(f"{sym:<8}{op:>14.4f}{ip:>14.4f}{dev:>9.2f}%  {status}")
        else:
            missing += 1
            print(f"{sym:<8}{op!s:>14}{ip!s:>14}{'-':>10}  EKSIK (bir kaynakta yok)")

    print("-" * len(header))
    print(
        f"Karsilastirilan: {compared}/{len(symbols)}  |  Eksik: {missing}  |  Maks sapma: %{max_dev:.2f}"
    )

    if compared == 0:
        print("SONUC: KARSILASTIRILAMADI (kaynaklardan biri veri vermedi)")
        return 2
    if max_dev < WARN_THRESHOLD:
        print("SONUC: DOGRULANDI - iki bagimsiz kaynak uyumlu.")
        return 0
    print("SONUC: INCELEME GEREKLI - kaynaklar arasi sapma yuksek.")
    return 1


if __name__ == "__main__":
    args = [a.upper() for a in sys.argv[1:]] or DEFAULT_SYMBOLS
    raise SystemExit(asyncio.run(main(args)))
