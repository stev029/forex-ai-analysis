"""Cascade mapping + indicator sanity tests (no network, no LLM)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forex_agents.indicators import atr, rsi, snapshot, structure
from forex_agents.timeframes import cascade, normalize_tf


def _df(n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 1.1000 + np.cumsum(rng.normal(0, 0.0002, n))
    high = close + 0.0003
    low = close - 0.0003
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx
    )


@pytest.mark.parametrize(
    "base,trend,val,crit",
    [
        ("m1", "m15", "m5", "m1"),
        ("m5", "m30", "m15", "m5"),
        ("m15", "h1", "m30", "m15"),
        ("h1", "d1", "h4", "h1"),
        ("1m", "m15", "m5", "m1"),
    ],
)
def test_cascade(base, trend, val, crit):
    m = cascade(base)
    assert (m["trend"], m["validation"], m["critical"]) == (trend, val, crit)


def test_cascade_clamps_at_top():
    m = cascade("w1")
    assert m["critical"] == "w1"
    assert m["trend"] in ("w1", "d1")


def test_normalize_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_tf("m7")


def test_indicators_finite():
    df = _df()
    assert atr(df).iloc[-1] > 0
    r = rsi(df["close"]).iloc[-1]
    assert 0 <= r <= 100


def test_snapshot_shape():
    snap = snapshot(_df(), "m1")
    assert snap["timeframe"] == "m1"
    assert snap["atr_pips"] > 0
    assert snap["structure"]["structure"] in ("bullish", "bearish", "range")
    assert snap["range_low"] <= snap["last_price"] <= snap["range_high"]


def test_structure_detects_uptrend():
    n = 120
    close = np.linspace(1.10, 1.12, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": close, "high": close + 0.0002, "low": close - 0.0002, "close": close, "volume": 1.0},
        index=idx,
    )
    assert structure(df)["structure"] == "bullish"
