"""Saran entry deterministik: skenario BUY/SELL + SL/TP/RR."""
from __future__ import annotations

import pytest

from forex_agents import advice

STATE = {
    "price": 1.1000,
    "pip": 0.0001,
    "critical_tf": "m5",
    "snapshots": {
        "m5": {
            "atr_pips": 8.0,
            "recent_highs": [1.1040, 1.1060],
            "recent_lows": [1.0960, 1.0940],
            "range_high": 1.1080,
            "range_low": 1.0920,
        }
    },
    "critical": {
        "nearest_support": 1.0975,
        "nearest_resistance": 1.1035,
        "zones": [
            {"type": "support", "low": 1.0970, "high": 1.0980},
            {"type": "resistance", "low": 1.1030, "high": 1.1045},
        ],
    },
    "trend": {"bias": "bullish"},
}


def test_build_returns_both_sides():
    out = advice.build(STATE, min_rr=1.5)
    assert out["ok"] is True
    assert out["buy"]["side"] == "buy"
    assert out["sell"]["side"] == "sell"
    assert out["buy"]["stop_loss"] < out["buy"]["entry"] < out["buy"]["take_profit"]
    assert out["sell"]["take_profit"] < out["sell"]["entry"] < out["sell"]["stop_loss"]


def test_bullish_bias_picks_buy_primary():
    out = advice.build(STATE, min_rr=1.5)
    assert out["aligned"] == "buy"
    assert out["primary"]["side"] == "buy"
    assert out["alt"]["side"] == "sell"
    assert out["buy"]["aligned"] is True
    assert out["sell"]["aligned"] is False


def test_bearish_bias_picks_sell_primary():
    state = {**STATE, "trend": {"bias": "bearish"}}
    out = advice.build(state, min_rr=1.5)
    assert out["primary"]["side"] == "sell"
    assert out["alt"]["side"] == "buy"


def test_range_bias_picks_higher_rr():
    state = {**STATE, "trend": {"bias": "range"}}
    out = advice.build(state, min_rr=1.5)
    assert out["aligned"] == "none"
    assert out["primary"]["rr"] >= out["alt"]["rr"]


def test_rr_matches_levels():
    out = advice.build(STATE, min_rr=1.5)
    for row in (out["buy"], out["sell"]):
        risk = abs(row["entry"] - row["stop_loss"])
        reward = abs(row["take_profit"] - row["entry"])
        assert row["rr"] == pytest.approx(round(reward / risk, 2), abs=0.02)


def test_min_rr_flagging():
    out = advice.build(STATE, min_rr=99.0)
    assert out["buy"]["valid"] is False
    assert "RR" in out["buy"]["note"] or "SL" in out["buy"]["note"]


def test_no_price_returns_not_ok():
    out = advice.build({"price": 0.0, "pip": 0.0001}, min_rr=1.5)
    assert out["ok"] is False
    assert out["primary"] is None


def test_missing_snapshot_still_builds():
    state = {"price": 1.1, "pip": 0.0001, "critical_tf": "m5",
             "snapshots": {}, "critical": {}, "trend": {}}
    out = advice.build(state, min_rr=1.5)
    assert out["ok"] is True
    assert out["buy"]["entry"] == 1.1
    assert out["buy"]["stop_loss"] < 1.1


def test_as_text_mentions_both_sides():
    text = advice.as_text(advice.build(STATE, min_rr=1.5))
    assert "primary BUY" in text
    assert "alt SELL" in text
    assert "RR" in text


def test_as_text_handles_not_ok():
    assert "saran:" in advice.as_text({"ok": False, "reason": "harga tidak tersedia"})


def test_render_shows_advice_section():
    from forex_agents.render import render

    final = {
        "symbol": "EURUSD", "base_tf": "m5", "price": 1.1,
        "decision": {"action": "NO_TRADE", "confidence": 30,
                     "reasons": [], "vetoes": []},
        "advice": advice.build(STATE, min_rr=1.5),
    }
    out = render(final)
    assert "5) Saran entry" in out
    assert "utama: BELI" in out
    assert "alternatif: JUAL" in out
    assert "searah tren" in out
    assert "SL <code>" in out
    assert "TP <code>" in out


def test_render_hides_advice_when_missing():
    from forex_agents.render import render

    final = {
        "symbol": "EURUSD", "base_tf": "m5", "price": 1.1,
        "decision": {"action": "NO_TRADE", "confidence": 30,
                     "reasons": [], "vetoes": []},
    }
    assert "Saran entry" not in render(final)


def test_render_section_numbers_stay_sequential():
    from forex_agents.render import render

    final = {
        "symbol": "EURUSD", "base_tf": "auto", "price": 1.1,
        "decision": {"action": "NO_TRADE", "confidence": 30,
                     "reasons": [], "vetoes": []},
        "advice": advice.build(STATE, min_rr=1.5),
        "auto_tf": {"primary": "m5", "trend_tf": "m30", "validation_tf": "m15",
                    "critical_tf": "m5", "ideal": ["m5"], "multi": False,
                    "threshold": 60, "ranked": []},
        "memory": {"count": 2, "tally": {}, "streak_action": "NO_TRADE",
                   "streak_count": 2, "calls": []},
    }
    out = render(final)
    assert "5) Saran entry" in out
    assert "6) Timeframe otomatis" in out
    assert "7) Ingatan" in out
