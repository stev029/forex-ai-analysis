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
