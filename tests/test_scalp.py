"""Mode scalping: deteksi area m15/m30, gerbang area ideal, entry m1/m5."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forex_agents import scalp
from forex_agents.modes import MODE_CASCADE, MODE_SCALP, describe, normalize_mode


def _frame(prices: list[float], freq: str = "1min", start: str = "2026-11-21") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    close = np.asarray(prices, dtype="float64")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0002,
            "low": close - 0.0002,
            "close": close,
            "volume": np.ones(len(close)),
        },
        index=idx,
    )


def _range_frame(n: int = 120, low: float = 1.1000, high: float = 1.1040) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    mid = (low + high) / 2
    close = mid + (high - low) / 2 * np.sin(np.linspace(0, 6 * np.pi, n))
    close = close + rng.normal(0, 0.00005, n)
    idx = pd.date_range("2026-11-01", periods=n, freq="15min", tz="UTC")
    s = close
    return pd.DataFrame(
        {
            "open": s,
            "high": s + 0.0002,
            "low": s - 0.0002,
            "close": s,
            "volume": np.ones(n),
        },
        index=idx,
    )


def _no_wick(n: int = 20, base: float = 1.1000) -> pd.DataFrame:
    """Candle tanpa wick sama sekali: high == max(o,c), low == min(o,c)."""
    idx = pd.date_range("2026-11-21", periods=n, freq="1min", tz="UTC")
    close = np.full(n, base, dtype="float64")
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "volume": np.ones(n)},
        index=idx,
    )


def _reject_low(n: int = 60, base: float = 1.1000) -> pd.DataFrame:
    """Candle terakhir punya lower wick panjang, close di atas -> penolakan bawah."""
    prices = [base] * n
    idx = pd.date_range("2026-11-21", periods=n, freq="1min", tz="UTC")
    close = np.asarray(prices, dtype="float64")
    df = pd.DataFrame(
        {"open": close, "high": close + 0.0001, "low": close - 0.0001,
         "close": close, "volume": np.ones(n)},
        index=idx,
    )
    df.iloc[-1, df.columns.get_loc("low")] = base - 0.0010
    df.iloc[-1, df.columns.get_loc("high")] = base + 0.0001
    df.iloc[-1, df.columns.get_loc("open")] = base - 0.0002
    df.iloc[-1, df.columns.get_loc("close")] = base
    return df


# --------------------------------------------------------------- mode switch
def test_normalize_mode_aliases():
    assert normalize_mode(None) == MODE_CASCADE
    assert normalize_mode("") == MODE_CASCADE
    assert normalize_mode("scalping") == MODE_SCALP
    assert normalize_mode("SCALP") == MODE_SCALP
    assert normalize_mode("normal") == MODE_CASCADE
    assert normalize_mode("standar") == MODE_CASCADE


def test_normalize_mode_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_mode("swing")


def test_describe_mentions_timeframes():
    assert "m15/m30" in describe(MODE_SCALP)
    assert "m1/m5" in describe(MODE_SCALP)
    assert "cascade" in describe(MODE_CASCADE)


def test_scalp_tfs_grouping():
    groups = scalp.scalp_tfs()
    assert groups["context"] == ("h1", "h4", "d1", "w1")
    assert groups["area"] == ("m15", "m30")
    assert groups["entry"] == ("m1", "m5")
    assert set(scalp.all_tfs()) == {"m1", "m5", "m15", "m30", "h1", "h4", "d1", "w1"}


# ------------------------------------------------------------------- candles
def test_candle_stats_fields():
    stats = scalp.candle_stats(_frame([1.1, 1.2]), bars=2)
    assert len(stats) == 2
    assert set(stats[-1]) == {
        "time", "open", "high", "low", "close", "body",
        "upper_wick", "lower_wick", "close_pos",
    }


def test_wick_rejection_detects_lower_wick():
    out = scalp.wick_rejection(_reject_low())
    assert out["last"] == "lower"
    assert out["bullish"] >= 1


def test_wick_rejection_none_on_flat():
    out = scalp.wick_rejection(_no_wick())
    assert out["last"] == "none"


def test_push_direction_and_consecutive():
    up = scalp.push(_frame([1.0, 1.1, 1.2, 1.3, 1.4]))
    assert up["direction"] == "up"
    assert up["consecutive"] >= 2
    down = scalp.push(_frame([1.4, 1.3, 1.2, 1.1]))
    assert down["direction"] == "down"


def test_exhaustion_score_bounds():
    out = scalp.exhaustion(_frame([1.0 + 0.001 * i for i in range(40)]))
    assert 0 <= out["score"] <= 3
    assert out["score"] >= 2  # RSI ekstrem + candle searah panjang
    assert out["rsi14"] >= 70


# --------------------------------------------------------------------- areas
def test_potential_area_finds_zones():
    df = _range_frame()
    area = scalp.potential_area(df, df, price=float(df["close"].iloc[-1]))
    assert area["zones"]
    assert area["tolerance"] > 0
    assert area["side"] in ("buy", "sell", "none")


def test_potential_area_inside_range():
    df = _range_frame()
    zone = scalp.potential_area(df, df, price=float(df["close"].iloc[-1]))
    support = zone["support"]
    assert support is not None
    price = float(support["high"])
    area = scalp.potential_area(df, df, price=price)
    assert area["inside"] is True
    assert area["active"] is not None
    assert area["touches"] >= 1


def test_ideal_area_requires_all_checks():
    df = _range_frame()
    price = float(df["close"].iloc[-1])
    area = scalp.potential_area(df, df, price=price)
    ideal = scalp.ideal_area(area, _reject_low(), price)
    assert set(ideal["checks"]) == {
        "harga_di_area", "area_teruji", "wick_penolakan",
        "wick_searah_area", "kelelahan",
    }
    assert ideal["ok"] is all(ideal["checks"].values())
    assert ideal["missing"] == [k for k, v in ideal["checks"].items() if not v]


def test_ideal_area_false_when_wick_missing():
    df = _range_frame()
    zone = scalp.potential_area(df, df, price=float(df["close"].iloc[-1]))
    price = float(zone["support"]["high"])
    area = scalp.potential_area(df, df, price=price)
    area["side"] = "buy"
    ideal = scalp.ideal_area(area, _no_wick(), price)
    assert ideal["ok"] is False
    assert "wick_penolakan" in ideal["missing"]


# --------------------------------------------------------------------- plans
def test_plan_buy_levels_ordered():
    df = _reject_low()
    price = float(df["close"].iloc[-1])
    area = {
        "active": {"low": price - 0.0005, "high": price + 0.0005},
        "side": "buy",
        "resistance": {"low": price + 0.0040, "high": price + 0.0045},
        "support": None,
    }
    out = scalp.plan(price, area, df, pip=0.0001)
    assert out["side"] == "buy"
    assert out["stop_loss"] < out["entry"] < out["take_profit"]
    assert out["rr"] > 0


def test_plan_sell_levels_ordered():
    df = _frame([1.1] * 20)
    price = 1.1
    area = {
        "active": {"low": price - 0.0005, "high": price + 0.0005},
        "side": "sell",
        "support": {"low": price - 0.0045, "high": price - 0.0040},
        "resistance": None,
    }
    out = scalp.plan(price, area, df, pip=0.0001)
    assert out["side"] == "sell"
    assert out["take_profit"] < out["entry"] < out["stop_loss"]
    assert out["rr"] > 0


def test_plan_none_side_returns_empty():
    out = scalp.plan(1.1, {"side": "none"}, _frame([1.1] * 10), pip=0.0001)
    assert out["entry"] is None
    assert out["rr"] is None


# ---------------------------------------------------------------------- read
def test_read_bundles_everything():
    area_df = _range_frame()
    entry_df = _reject_low()
    price = float(entry_df["close"].iloc[-1])
    out = scalp.read(price, area_df, area_df, entry_df, pip=0.0001)
    data = out.as_dict()
    assert set(data) == {"price", "area", "ideal", "plan", "push"}
    assert data["price"] == price
    assert "checks" in data["ideal"]


def test_read_tolerates_missing_confirm_frame():
    area_df = _range_frame()
    entry_df = _reject_low()
    price = float(entry_df["close"].iloc[-1])
    out = scalp.read(price, area_df, None, entry_df, pip=0.0001)
    assert out.area["zones"]
    assert out.area["touches"] >= 1


def test_read_falls_back_to_confirm_when_area_missing():
    df = _range_frame()
    price = float(df["close"].iloc[-1])
    out = scalp.read(price, None, df, None, pip=0.0001)
    assert out.area["zones"]


def test_read_raises_without_area_data():
    with pytest.raises(ValueError):
        scalp.read(1.1, None, None, None, pip=0.0001)


# ------------------------------------------------- migrasi tabel chat_settings
def test_schema_migrates_legacy_chat_settings():
    """Tabel chat_settings lama (balance/risk/timeframe) harus dapat kolom mode."""
    from forex_agents.memory import SCHEMA_SQL

    assert "ALTER TABLE chat_settings ADD COLUMN IF NOT EXISTS mode" in SCHEMA_SQL
    assert "UPDATE chat_settings SET mode = 'cascade' WHERE mode IS NULL" in SCHEMA_SQL
    assert "ALTER COLUMN mode SET DEFAULT 'cascade'" in SCHEMA_SQL
