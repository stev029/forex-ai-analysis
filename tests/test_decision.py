"""Decision guardrails + renderer tests. LLM calls are stubbed out."""
from __future__ import annotations

import pytest

from forex_agents import nodes
from forex_agents.render import render, split_message
from forex_agents.schemas import DecisionOut


def _state(**over) -> dict:
    base = {
        "symbol": "EURUSD",
        "base_tf": "m1",
        "trend_tf": "m15",
        "validation_tf": "m5",
        "critical_tf": "m1",
        "price": 1.1000,
        "snapshots": {"m1": {"atr_pips": 2.0}},
        "trend": {"bias": "bullish", "strength": "strong", "rationale": "ema stack"},
        "validation": {"agrees_with_trend": True, "phase": "continuation",
                       "pattern": "bull flag", "invalidation_level": 1.0985},
        "critical": {"zones": [], "nearest_support": 1.0990, "nearest_resistance": 1.1020,
                     "entry_trigger": "close above 1.1010"},
        "errors": [],
        "trace": [],
    }
    base.update(over)
    return base


async def _run(monkeypatch, decision: DecisionOut) -> dict:
    async def fake(prompt, schema, retries=2):
        return decision

    monkeypatch.setattr(nodes, "structured", fake)
    return await nodes.decision_node(_state())


def _fake_decision(decision: DecisionOut):
    """Stub `structured` yang selalu mengembalikan satu DecisionOut."""
    async def fake(prompt, schema, retries=2):
        return decision

    return fake


@pytest.mark.asyncio
async def test_good_plan_kept(monkeypatch):
    out = await _run(monkeypatch, DecisionOut(
        action="BUY", confidence=70, entry=1.1000, stop_loss=1.0995, take_profit=1.1020,
        reasons=["trend up"],
    ))
    assert out["decision"]["action"] == "BUY"
    assert out["decision"]["rr"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_low_rr_vetoed(monkeypatch):
    out = await _run(monkeypatch, DecisionOut(
        action="BUY", confidence=70, entry=1.1000, stop_loss=1.0990, take_profit=1.1005,
    ))
    assert out["decision"]["action"] == "NO_TRADE"
    assert any("min_rr" in v for v in out["vetoes"])


@pytest.mark.asyncio
async def test_wrong_side_vetoed(monkeypatch):
    out = await _run(monkeypatch, DecisionOut(
        action="BUY", confidence=70, entry=1.1000, stop_loss=1.1010, take_profit=1.0990,
    ))
    assert out["decision"]["action"] == "NO_TRADE"
    assert any("sisi yang salah" in v for v in out["vetoes"])


@pytest.mark.asyncio
async def test_wide_stop_vetoed(monkeypatch):
    out = await _run(monkeypatch, DecisionOut(
        action="SELL", confidence=70, entry=1.1000, stop_loss=1.1100, take_profit=1.0800,
    ))
    assert out["decision"]["action"] == "NO_TRADE"
    assert any("3x ATR" in v for v in out["vetoes"])


def test_render_escapes_html():
    state = _state(decision={"action": "NO_TRADE", "confidence": 10,
                             "reasons": ["<script>"], "vetoes": []})
    text = render(state)
    assert "<script>" not in text
    assert "TIDAK ENTRY" in text
    assert "m15" in text and "m5" in text and "m1" in text

def test_render_keeps_full_text_uncut():
    long = " ".join(f"kata{i}" for i in range(200))
    state = _state(trend={"bias": "bearish", "strength": "strong", "rationale": long},
                   decision={"action": "NO_TRADE", "confidence": 10,
                             "reasons": [long], "vetoes": [long]})
    text = render(state)
    assert text.count(long) == 3
    assert "…" not in text

def test_split_message_keeps_every_line():
    lines = [f"baris {i} " + "x" * 40 for i in range(200)]
    text = "\n".join(lines)
    chunks = split_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks) == text

SCALP_OK = {
    "area": {"support": {"low": 1.0990, "high": 1.0995},
             "resistance": {"low": 1.1040, "high": 1.1045},
             "active": {"low": 1.0990, "high": 1.0995},
             "inside": True, "side": "buy", "touches": 3, "tolerance": 0.0002},
    "ideal": {"ok": True, "checks": {"harga_di_area": True, "area_teruji": True,
                                     "wick_penolakan": True, "wick_searah_area": True,
                                     "kelelahan": True},
              "missing": [], "rejection": {"last": "lower"}, "exhaustion": {"score": 3}},
    "plan": {"side": "buy", "entry": 1.0992, "stop_loss": 1.0980,
             "take_profit": 1.1040, "rr": 4.0},
    "push": {"direction": "down", "net": -0.002, "consecutive": 3, "decay": True},
}


@pytest.mark.asyncio
async def test_scalp_blocks_when_area_not_ideal(monkeypatch):
    scalp = {**SCALP_OK, "ideal": {**SCALP_OK["ideal"], "ok": False,
                                   "missing": ["wick_penolakan"]}}
    monkeypatch.setattr(nodes, "structured", _fake_decision(DecisionOut(
        action="BUY", confidence=90, entry=1.0992, stop_loss=1.0980,
        take_profit=1.1040, reasons=[], vetoes=[])))
    state = _state(mode="scalp", scalp=scalp)
    res = await nodes.decision_node(state)
    assert res["decision"]["action"] == "NO_TRADE"
    assert any("area ideal belum terbentuk" in v for v in res["vetoes"])


@pytest.mark.asyncio
async def test_scalp_blocks_wrong_side(monkeypatch):
    monkeypatch.setattr(nodes, "structured", _fake_decision(DecisionOut(
        action="SELL", confidence=80, entry=1.0992, stop_loss=1.1004,
        take_profit=1.0950, reasons=[], vetoes=[])))
    res = await nodes.decision_node(_state(mode="scalp", scalp=SCALP_OK))
    assert res["decision"]["action"] == "NO_TRADE"
    assert any("area menuntut BUY" in v for v in res["vetoes"])


@pytest.mark.asyncio
async def test_scalp_allows_aligned_entry(monkeypatch):
    monkeypatch.setattr(nodes, "structured", _fake_decision(DecisionOut(
        action="BUY", confidence=80, entry=1.0992, stop_loss=1.0990,
        take_profit=1.1040, reasons=["area ideal"], vetoes=[])))
    res = await nodes.decision_node(_state(mode="scalp", scalp=SCALP_OK))
    assert res["decision"]["action"] == "BUY"
    assert res["vetoes"] == []


@pytest.mark.asyncio
async def test_cascade_mode_ignores_scalp_gate(monkeypatch):
    scalp = {**SCALP_OK, "ideal": {**SCALP_OK["ideal"], "ok": False,
                                   "missing": ["harga_di_area"]}}
    monkeypatch.setattr(nodes, "structured", _fake_decision(DecisionOut(
        action="BUY", confidence=80, entry=1.0992, stop_loss=1.0990,
        take_profit=1.1040, reasons=[], vetoes=[])))
    res = await nodes.decision_node(_state(mode="cascade", scalp=scalp))
    assert res["decision"]["action"] == "BUY"


def test_render_shows_scalp_section():
    final = {
        "symbol": "XAUUSD", "base_tf": "m1", "mode": "scalp", "price": 1.0992,
        "decision": {"action": "NO_TRADE", "confidence": 40, "reasons": [], "vetoes": []},
        "scalp": SCALP_OK,
    }
    out = render(final)
    assert "5) Scalping" in out
    assert "area m15/m30" in out
    assert "gerbang area ideal: <b>terbentuk</b>" in out
    assert "RR <b>4.0</b>" in out
    assert "scalp" in out.splitlines()[0]


def test_render_hides_scalp_section_in_cascade():
    final = {
        "symbol": "XAUUSD", "base_tf": "m1", "mode": "cascade", "price": 1.0992,
        "decision": {"action": "NO_TRADE", "confidence": 40, "reasons": [], "vetoes": []},
        "scalp": SCALP_OK,
    }
    assert "Scalping" not in render(final)
