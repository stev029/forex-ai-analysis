"""Deterministic technical indicators. No LLM here - numbers must be reproducible."""
from __future__ import annotations

import pandas as pd

from .timeframes import PIP


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    # loss == 0 berarti tidak ada penurunan sama sekali -> RSI 100, bukan NaN.
    rs = gain / loss.where(loss > 0)
    out = 100 - 100 / (1 + rs)
    out = out.where(loss > 0, 100.0)
    return out.fillna(50.0).astype("float64")


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def swing_points(df: pd.DataFrame, left: int = 2, right: int = 2) -> tuple[list[float], list[float]]:
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    for i in range(left, len(df) - right):
        window_h = h[i - left : i + right + 1]
        window_l = l[i - left : i + right + 1]
        if h[i] == window_h.max():
            highs.append(float(h[i]))
        if l[i] == window_l.min():
            lows.append(float(l[i]))
    return highs, lows


def structure(df: pd.DataFrame, lookback: int = 60) -> dict:
    """Classify market structure: HH/HL (bull) vs LH/LL (bear) vs range.

    Swing pairs decide when available; otherwise fall back to net drift so a clean
    trend with few swing points is not mislabelled as a range.
    """
    tail = df.tail(lookback)
    highs, lows = swing_points(tail)
    out = {"swing_highs": highs[-4:], "swing_lows": lows[-4:], "structure": "range"}
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]
        hl = lows[-1] > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1] < lows[-2]
        if hh and hl:
            out["structure"] = "bullish"
        elif lh and ll:
            out["structure"] = "bearish"
        return out

    close = tail["close"]
    if len(close) >= 5:
        drift = float(close.iloc[-1] - close.iloc[0])
        span = float(tail["high"].max() - tail["low"].min())
        if span > 0 and abs(drift) / span > 0.5:
            out["structure"] = "bullish" if drift > 0 else "bearish"
    return out


def snapshot(df: pd.DataFrame, tf: str, bars: int = 120, pip: float = PIP) -> dict:
    """Compact numeric snapshot fed to the LLM instead of raw candles."""
    tail = df.tail(bars)
    close = tail["close"]
    e20, e50, e200 = ema(close, 20), ema(close, 50), ema(close, 200)
    atr_s = atr(tail)
    macd_line, macd_sig, hist = macd(close)
    price = float(close.iloc[-1])
    atr_v = float(atr_s.iloc[-1])

    return {
        "timeframe": tf,
        "last_price": round(price, 5),
        "last_bar_time": str(tail.index[-1]),
        "bars": int(len(tail)),
        "ema20": round(float(e20.iloc[-1]), 5),
        "ema50": round(float(e50.iloc[-1]), 5),
        "ema200": round(float(e200.iloc[-1]), 5),
        "ema_stack": "bull" if e20.iloc[-1] > e50.iloc[-1] > e200.iloc[-1] else
                     "bear" if e20.iloc[-1] < e50.iloc[-1] < e200.iloc[-1] else "mixed",
        "rsi14": round(float(rsi(close).iloc[-1]), 2),
        "macd_hist": round(float(hist.iloc[-1]), 6),
        "macd_hist_prev": round(float(hist.iloc[-2]), 6),
        "atr14": round(atr_v, 5),
        "atr_pips": round(atr_v / pip, 1) if pip else 0.0,
        "range_high": round(float(tail["high"].max()), 5),
        "range_low": round(float(tail["low"].min()), 5),
        "structure": structure(tail),
        "recent_highs": [round(v, 5) for v in swing_points(tail)[0][-4:]],
        "recent_lows": [round(v, 5) for v in swing_points(tail)[1][-4:]],
    }
