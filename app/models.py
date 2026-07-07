"""API'nin dondurdugu veri semalari (Pydantic modelleri)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Quote(BaseModel):
    """Bir hissenin anlik (gecikmeli) fiyat goruntusu."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "symbol": "THYAO",
                    "price": 334.0,
                    "previous_close": 333.25,
                    "change": 0.75,
                    "change_percent": 0.22,
                    "open": 335.25,
                    "day_high": 335.75,
                    "day_low": 330.75,
                    "volume": 44008702,
                    "currency": "TRY",
                    "market_state": "OPEN",
                    "source": "yahoo",
                    "delayed": True,
                    "updated_at": "2026-07-06T09:30:00Z",
                }
            ]
        }
    }

    symbol: str = Field(..., description="BIST sembolu, orn. THYAO")
    price: float | None = Field(None, description="Son islem fiyati")
    previous_close: float | None = Field(None, description="Onceki kapanis")
    change: float | None = Field(None, description="Fiyat degisimi (mutlak)")
    change_percent: float | None = Field(None, description="Yuzde degisim")
    open: float | None = Field(None, description="Gunluk acilis")
    day_high: float | None = Field(None, description="Gun ici en yuksek")
    day_low: float | None = Field(None, description="Gun ici en dusuk")
    volume: int | None = Field(None, description="Islem hacmi (adet)")
    currency: str = Field("TRY", description="Para birimi")
    market_state: str = Field("UNKNOWN", description="OPEN / CLOSED / UNKNOWN")
    source: str = Field("yahoo", description="Veri kaynagi")
    delayed: bool = Field(True, description="Veri gecikmeli mi (BIST icin evet)")
    updated_at: datetime | None = Field(None, description="Onbellege alinma zamani (UTC)")
    exchange_time: datetime | None = Field(
        None, description="Borsadaki gercek islem zamani (kaynak sagliyorsa; UTC)"
    )


class HistoryBar(BaseModel):
    """Gecmis OHLCV mumu."""

    time: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


class HistoryResponse(BaseModel):
    symbol: str
    period: str
    interval: str
    currency: str = "TRY"
    bars: list[HistoryBar] = []


class HealthResponse(BaseModel):
    status: str
    version: str
    symbols_tracked: int
    quotes_cached: int
    last_update: datetime | None = None
    market_open: bool
    update_interval: float
