"""API'nin dondurdugu veri semalari (Pydantic modelleri)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Quote(BaseModel):
    """Bir hissenin anlik (gecikmeli) fiyat goruntusu."""

    symbol: str = Field(..., description="BIST sembolu, orn. THYAO")
    price: Optional[float] = Field(None, description="Son islem fiyati")
    previous_close: Optional[float] = Field(None, description="Onceki kapanis")
    change: Optional[float] = Field(None, description="Fiyat degisimi (mutlak)")
    change_percent: Optional[float] = Field(None, description="Yuzde degisim")
    open: Optional[float] = Field(None, description="Gunluk acilis")
    day_high: Optional[float] = Field(None, description="Gun ici en yuksek")
    day_low: Optional[float] = Field(None, description="Gun ici en dusuk")
    volume: Optional[int] = Field(None, description="Islem hacmi (adet)")
    currency: str = Field("TRY", description="Para birimi")
    market_state: str = Field("UNKNOWN", description="OPEN / CLOSED / UNKNOWN")
    source: str = Field("yahoo", description="Veri kaynagi")
    delayed: bool = Field(True, description="Veri gecikmeli mi (BIST icin evet)")
    updated_at: Optional[datetime] = Field(None, description="Onbellege alinma zamani (UTC)")


class HistoryBar(BaseModel):
    """Gecmis OHLCV mumu."""

    time: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = None


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
    last_update: Optional[datetime] = None
    market_open: bool
    update_interval: float
