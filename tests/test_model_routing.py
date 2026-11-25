"""Tes routing model per-tahap + rantai fallback."""
from __future__ import annotations

import pytest

from forex_agents import llm as llm_mod
from forex_agents.config import Settings
from forex_agents.llm import model_chain
from forex_agents.schemas import TrendOut


def _settings(**kw) -> Settings:
    base = dict(
        llm_base_url="http://x/v1",
        llm_api_key="k",
        llm_model="base-model",
        llm_temperature=0.1,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_model_for_defaults_to_base_model():
    s = _settings()
    for stage in ("trend", "validation", "critical", "decision"):
        assert s.model_for(stage) == "base-model"


def test_model_for_overrides_per_stage():
    s = _settings(
        llm_model_trend="cheap",
        llm_model_validation="cheap2",
        llm_model_critical="cheap3",
        llm_model_decision="abacus/gpt-6-sol",
    )
    assert s.model_for("trend") == "cheap"
    assert s.model_for("validation") == "cheap2"
    assert s.model_for("critical") == "cheap3"
    assert s.model_for("decision") == "abacus/gpt-6-sol"


def test_model_for_unknown_stage_falls_back():
    assert _settings(llm_model="m").model_for("nope") == "m"


def test_model_for_blank_override_uses_base():
    assert _settings(llm_model_trend="   ").model_for("trend") == "base-model"


def test_fallbacks_for_stage_then_global_dedup():
    s = _settings(
        llm_model_decision="strong",
        llm_fallback_decision="mid, strong ; last",
        llm_fallbacks="last, Tier2",
    )
    assert s.fallbacks_for("decision") == ["mid", "last", "Tier2"]


def test_fallbacks_for_empty():
    assert _settings().fallbacks_for("trend") == []


def test_model_chain_order_and_dedup():
    assert model_chain("a", ["b", "a", "", "  ", "c"]) == ["a", "b", "c"]


def test_model_chain_defaults_to_settings_when_empty(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: _settings(llm_model="def"))
    assert model_chain(None, None) == ["def"]
    assert model_chain("", []) == ["def"]


@pytest.mark.asyncio
async def test_structured_uses_first_model(monkeypatch):
    calls: list[str] = []

    async def fake_once(prompt, schema, retries, model):
        calls.append(model)
        return TrendOut(bias="bullish", strength="moderate", rationale="ok")

    monkeypatch.setattr(llm_mod, "_structured_once", fake_once)
    out = await llm_mod.structured("p", TrendOut, model="first", fallbacks=["second"])
    assert out.bias == "bullish"
    assert calls == ["first"]


@pytest.mark.asyncio
async def test_structured_falls_back_on_failure(monkeypatch):
    calls: list[str] = []

    async def fake_once(prompt, schema, retries, model):
        calls.append(model)
        if model == "first":
            raise RuntimeError("503 insufficient_quota")
        return TrendOut(bias="range", strength="weak", rationale="ok")

    monkeypatch.setattr(llm_mod, "_structured_once", fake_once)
    out = await llm_mod.structured("p", TrendOut, model="first", fallbacks=["second"])
    assert out.bias == "range"
    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_structured_raises_when_all_models_fail(monkeypatch):
    async def fake_once(prompt, schema, retries, model):
        raise RuntimeError(f"boom {model}")

    monkeypatch.setattr(llm_mod, "_structured_once", fake_once)
    with pytest.raises(RuntimeError, match="structured output failed: boom second"):
        await llm_mod.structured("p", TrendOut, model="first", fallbacks=["second"])


@pytest.mark.asyncio
async def test_structured_single_model_no_fallback(monkeypatch):
    async def fake_once(prompt, schema, retries, model):
        raise RuntimeError("nope")

    monkeypatch.setattr(llm_mod, "_structured_once", fake_once)
    with pytest.raises(RuntimeError, match="structured output failed: nope"):
        await llm_mod.structured("p", TrendOut, model="solo")


@pytest.mark.asyncio
async def test_structured_records_used_model_on_fallback(monkeypatch):
    async def fake_once(prompt, schema, retries, model):
        if model == "first":
            raise RuntimeError("503")
        return TrendOut(bias="bullish", strength="moderate", rationale="ok")

    monkeypatch.setattr(llm_mod, "_structured_once", fake_once)
    await llm_mod.structured("p", TrendOut, model="first", fallbacks=["second"])
    assert llm_mod.used_model() == "second"


@pytest.mark.asyncio
async def test_structured_records_used_model_primary(monkeypatch):
    async def fake_once(prompt, schema, retries, model):
        return TrendOut(bias="bullish", strength="moderate", rationale="ok")

    monkeypatch.setattr(llm_mod, "_structured_once", fake_once)
    await llm_mod.structured("p", TrendOut, model="first", fallbacks=["second"])
    assert llm_mod.used_model() == "first"


def test_get_llm_uses_given_model(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: _settings())
    assert llm_mod.get_llm("custom").model_name == "custom"
    assert llm_mod.get_llm().model_name == "base-model"


def test_render_shows_model_line():
    from forex_agents.render import render

    out = render({
        "symbol": "EURUSD", "base_tf": "m1", "trend_tf": "m15",
        "validation_tf": "m5", "critical_tf": "m1", "price": 1.1,
        "decision": {"action": "NO_TRADE", "confidence": 50},
        "trend": {}, "validation": {}, "critical": {},
        "trace": [
            "trend m15: bearish/moderate via cheap-lite",
            "validation m5: continuation agree=True via cheap-lite",
            "critical m1: 2 zones via cheap-lite",
            "decision: NO_TRADE conf=50 rr=None via abacus/gpt-6-sol",
        ],
    })
    assert "model:" in out
    assert "cheap-lite" in out
    assert "abacus/gpt-6-sol" in out
    assert out.count("cheap-lite") == 1  # dedup


def test_render_hides_model_line_without_trace():
    from forex_agents.render import render

    out = render({
        "symbol": "EURUSD", "base_tf": "m1", "trend_tf": "m15",
        "validation_tf": "m5", "critical_tf": "m1", "price": 1.1,
        "decision": {"action": "NO_TRADE", "confidence": 50},
        "trend": {}, "validation": {}, "critical": {}, "trace": ["provider: tradingview"],
    })
    assert "model:" not in out
