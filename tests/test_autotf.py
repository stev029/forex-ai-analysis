"""Auto-TF: skor tiap timeframe, pilih yang ideal, saran multi-timeframe."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forex_agents import autotf
from forex_agents.scheduler import auto_interval, targets
from forex_agents.timeframes import AUTO, cascade, normalize_tf


def _frame(prices: list[float], freq: str = "1min", start: str = "2026-11-21") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    close = np.asarray(prices, dtype="float64")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0004,
            "low": close - 0.0004,
            "close": close,
            "volume": np.ones(len(close)),
        },
        index=idx,
    )


def _trending(n: int = 120, start: float = 1.1000, step: float = 0.0002,
              freq: str = "1min", start_ts: str = "2026-11-21") -> pd.DataFrame:
    prices = [start + step * i for i in range(n)]
    df = _frame(prices, freq=freq, start=start_ts)
    # ema stack butuh tren monoton; beri sedikit variasi agar tidak konstan
    return df


def _flat(n: int = 120, level: float = 1.1000, freq: str = "1min") -> pd.DataFrame:
    return _frame([level] * n, freq=freq)


# ---------------------------------------------------------------- auto marker
def test_normalize_tf_accepts_auto():
    assert normalize_tf("auto") == AUTO
    assert normalize_tf("otomatis") == AUTO
    assert normalize_tf("AUTO-TF") == AUTO


def test_cascade_rejects_auto():
    with pytest.raises(ValueError):
        cascade("auto")


def test_is_auto_variants():
    assert autotf.is_auto("auto")
    assert autotf.is_auto(" Otomatis ")
    assert not autotf.is_auto("m1")
    assert not autotf.is_auto(None)


# -------------------------------------------------------------------- scoring
def test_score_tf_returns_full_shape():
    df = _trending()
    out = autotf.score_tf(df, "m15", price=float(df["close"].iloc[-1]), pip=0.0001)
    assert set(out) >= {"tf", "score", "ideal", "strong", "parts", "checks",
                        "atr_pips", "rsi14", "lag_s", "reasons"}
    assert 0 <= out["score"] <= 100
    assert out["tf"] == "m15"


def test_score_tf_rewards_clear_trend():
    df = _trending()
    out = autotf.score_tf(df, "m15", price=float(df["close"].iloc[-1]), pip=0.0001)
    assert out["checks"]["tren_jelas"] is True
    assert out["parts"]["trend"] == autotf.W_TREND


def test_score_tf_penalises_flat_market():
    df = _flat()
    out = autotf.score_tf(df, "m1", price=1.1, pip=0.0001)
    assert out["score"] < autotf.IDEAL_SCORE
    assert out["checks"]["tren_jelas"] is False


def test_score_tf_flags_stale_bar():
    df = _trending()
    out = autotf.score_tf(
        df, "m1", price=float(df["close"].iloc[-1]), pip=0.0001,
        now=pd.Timestamp("2026-11-21 06:00", tz="UTC").to_pydatetime(),
    )
    assert out["checks"]["bar_segar"] is False
    assert out["parts"]["fresh"] == 0.0


def test_score_tf_parts_sum_to_score():
    df = _trending()
    out = autotf.score_tf(df, "m15", price=float(df["close"].iloc[-1]), pip=0.0001)
    assert out["score"] == pytest.approx(round(sum(out["parts"].values())))


# --------------------------------------------------------------------- choose
def test_choose_returns_primary_and_cascade():
    frames = {"m1": _trending(freq="1min"), "m15": _trending(freq="15min")}
    out = autotf.choose(frames, price=1.12, pip=0.0001)
    assert out["primary"] in frames
    assert out["critical_tf"] == out["primary"]
    assert out["trend_tf"] in ("m5", "m15", "m30", "h1", "h4", "d1", "w1")
    assert out["ranked"]
    assert out["ranked"][0]["score"] >= out["ranked"][-1]["score"]


def test_choose_skips_short_frames():
    frames = {"m1": _frame([1.1] * 5), "m15": _trending(freq="15min")}
    out = autotf.choose(frames, price=1.12, pip=0.0001)
    assert out["skipped"]["m1"] == "data kurang"
    assert out["primary"] == "m15"


def test_choose_marks_multi_when_two_ideal(monkeypatch):
    monkeypatch.setattr(autotf, "score_tf", lambda df, tf, price, pip, now=None: {
        "tf": tf, "score": 90, "ideal": True, "strong": True, "parts": {},
        "checks": {}, "atr_pips": 5.0, "rsi14": 60.0, "lag_s": 0,
        "structure": "up", "reasons": ["ok"],
    })
    frames = {"m15": _trending(freq="15min"), "m30": _trending(freq="30min")}
    out = autotf.choose(frames, price=1.12, pip=0.0001)
    assert out["multi"] is True
    assert set(out["ideal"]) == {"m15", "m30"}


def test_choose_single_ideal_not_multi(monkeypatch):
    scores = {"m15": 90, "m30": 40}

    def fake(df, tf, price, pip, now=None):
        s = scores[tf]
        return {"tf": tf, "score": s, "ideal": s >= autotf.IDEAL_SCORE,
                "strong": s >= autotf.STRONG_SCORE, "parts": {}, "checks": {},
                "atr_pips": 5.0, "rsi14": 60.0, "lag_s": 0, "structure": "up",
                "reasons": ["ok"]}

    monkeypatch.setattr(autotf, "score_tf", fake)
    frames = {"m15": _trending(freq="15min"), "m30": _trending(freq="30min")}
    out = autotf.choose(frames, price=1.12, pip=0.0001)
    assert out["primary"] == "m15"
    assert out["multi"] is False
    assert out["ideal"] == ["m15"]


def test_choose_tolerates_scoring_error(monkeypatch):
    def fake(df, tf, price, pip, now=None):
        if tf == "m15":
            raise RuntimeError("boom")
        return {"tf": tf, "score": 70, "ideal": True, "strong": False, "parts": {},
                "checks": {}, "atr_pips": 5.0, "rsi14": 60.0, "lag_s": 0,
                "structure": "up", "reasons": ["ok"]}

    monkeypatch.setattr(autotf, "score_tf", fake)
    frames = {"m15": _trending(freq="15min"), "m30": _trending(freq="30min")}
    out = autotf.choose(frames, price=1.12, pip=0.0001)
    assert "m15" in out["skipped"]
    assert out["primary"] == "m30"


def test_choose_empty_frames():
    out = autotf.choose({}, price=1.1, pip=0.0001)
    assert out["primary"] == ""
    assert out["ideal"] == []
    assert out["multi"] is False


def test_as_text_lists_ranked():
    frames = {"m15": _trending(freq="15min")}
    out = autotf.choose(frames, price=1.12, pip=0.0001)
    text = autotf.as_text(out)
    assert "m15: skor" in text


# ------------------------------------------------------------------ scheduler
def test_scheduler_auto_interval():
    assert auto_interval("auto") == 15
    assert auto_interval("m1") == 15


def test_scheduler_auto_targets():
    assert targets("auto") == ("m30", "m15")
    assert targets("m1") == ("m15", "m5")


# --------------------------------------------------------------------- render
def test_render_shows_auto_section():
    from forex_agents.render import render

    final = {
        "symbol": "XAUUSD", "base_tf": "auto", "price": 3600.0,
        "decision": {"action": "NO_TRADE", "confidence": 70,
                     "reasons": [], "vetoes": []},
        "auto_tf": {
            "primary": "m5", "trend_tf": "m30", "validation_tf": "m15",
            "critical_tf": "m5", "ideal": ["m5", "h1"], "multi": True,
            "threshold": 60,
            "ranked": [
                {"tf": "m5", "score": 91, "ideal": True, "reasons": ["ema stack bull"]},
                {"tf": "h1", "score": 78, "ideal": True, "reasons": ["wick lower x2"]},
            ],
        },
    }
    out = render(final)
    assert "5) Timeframe otomatis" in out
    assert "dipilih: <code>m5</code>" in out
    assert "multi-timeframe" in out
    assert "m5, h1" in out
    assert "auto-tf" in out.splitlines()[0]


def test_render_auto_without_ideal_tf():
    from forex_agents.render import render

    final = {
        "symbol": "XAUUSD", "base_tf": "auto", "price": 3600.0,
        "decision": {"action": "NO_TRADE", "confidence": 30,
                     "reasons": [], "vetoes": []},
        "auto_tf": {"primary": "m15", "trend_tf": "h1", "validation_tf": "m30",
                    "critical_tf": "m15", "ideal": [], "multi": False,
                    "threshold": 60, "ranked": []},
    }
    out = render(final)
    assert "belum ada TF yang lolos ambang skor 60" in out
    assert "multi-timeframe" not in out


def test_render_hides_auto_section_for_fixed_tf():
    from forex_agents.render import render

    final = {
        "symbol": "XAUUSD", "base_tf": "m1", "price": 3600.0,
        "decision": {"action": "NO_TRADE", "confidence": 30,
                     "reasons": [], "vetoes": []},
    }
    out = render(final)
    assert "Timeframe otomatis" not in out
    assert "auto-tf" not in out.splitlines()[0]
