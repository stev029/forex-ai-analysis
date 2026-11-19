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


def to_yf_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if "=" in s:
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
            raise RuntimeError(f"no data for {symbol} {tf}")

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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
