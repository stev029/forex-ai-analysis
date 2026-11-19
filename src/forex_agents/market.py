"""Market data provider. yfinance default; pluggable interface for broker feeds later."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

import pandas as pd

from .timeframes import YF_MAP, normalize_tf

log = logging.getLogger(__name__)

OHLCV_COLS = ["open", "high", "low", "close", "volume"]


class MarketProvider(Protocol):
    def fetch(self, symbol: str, timeframe: str, bars: int = 300) -> pd.DataFrame: ...


# yfinance has no FX-style tickers for metals/indices/crypto/oil - map to the futures/spot feeds.
SYMBOL_ALIASES: dict[str, str] = {
    "XAUUSD": "GC=F", "GOLD": "GC=F", "XAU": "GC=F",
    "XAGUSD": "SI=F", "SILVER": "SI=F", "XAG": "SI=F",
    "BTCUSD": "BTC-USD", "BTCUSDT": "BTC-USD", "BTC": "BTC-USD",
    "ETHUSD": "ETH-USD", "ETHUSDT": "ETH-USD", "ETH": "ETH-USD",
    "US30": "^DJI", "DJI": "^DJI", "DOW": "^DJI",
    "NAS100": "^NDX", "NDX": "^NDX", "USTEC": "^NDX",
    "SPX500": "^GSPC", "SPX": "^GSPC", "US500": "^GSPC",
    "USOIL": "CL=F", "WTI": "CL=F", "CRUDE": "CL=F",
    "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "DXY": "DX-Y.NYB", "USDX": "DX-Y.NYB",
}


def to_yf_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if s in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[s]
    if "=" in s or "-" in s or s.startswith("^"):
        return s
    if len(s) == 6 and s.isalpha():
        return f"{s}=X"
    return s


class YFinanceProvider:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    def fetch(self, symbol: str, timeframe: str, bars: int = 300) -> pd.DataFrame:
        import time

        import yfinance as yf

        tf = normalize_tf(timeframe)
        interval, period = YF_MAP[tf]
        key = (to_yf_symbol(symbol), tf)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < 30:
            return cached[1].tail(bars).copy()

        raw = yf.download(
            to_yf_symbol(symbol),
            interval=interval,
            period=period,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if raw is None or raw.empty:
            raise RuntimeError(
                f"no data for {symbol} {tf} (yfinance ticker {to_yf_symbol(symbol)}); "
                f"check the symbol with /symbols"
            )

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0] for c in raw.columns]
        df = raw.rename(columns=str.lower)[OHLCV_COLS].dropna()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()

        if tf == "h4":
            df = (
                df.resample("4h")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna()
            )
        df = df.tail(max(bars, 60))
        self._cache[key] = (now, df)
        return df.copy()


def get_provider(name: str) -> MarketProvider:
    name = (name or "yfinance").lower()
    if name == "yfinance":
        return YFinanceProvider()
    raise ValueError(f"unsupported MARKET_PROVIDER={name!r}")


def last_price(df: pd.DataFrame) -> float:
    return float(df["close"].iloc[-1])


# Pip size per instrument class. FX majors 0.0001, JPY crosses 0.01, metals 0.1,
# indices/crypto/oil 1.0. Wrong pip size makes ATR/stop comparisons meaningless.
_METALS = {"GC=F", "SI=F", "XAUUSD", "XAGUSD", "GOLD", "SILVER", "XAU", "XAG"}
_INDEX_LIKE = {"^DJI", "^NDX", "^GSPC", "DX-Y.NYB", "CL=F", "BZ=F",
               "BTC-USD", "ETH-USD", "US30", "NAS100", "SPX500", "USOIL", "DXY"}


def pip_size(symbol: str) -> float:
    s = symbol.strip().upper()
    ticker = to_yf_symbol(s)
    if s in _METALS or ticker in _METALS:
        return 0.1
    if s in _INDEX_LIKE or ticker in _INDEX_LIKE or ticker.startswith("^"):
        return 1.0
    if len(s) == 6 and s.isalpha() and s.endswith("JPY"):
        return 0.01
    if "=" in ticker and not ticker.endswith("=X"):
        return 1.0
    return 0.0001


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
