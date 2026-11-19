"""Graph nodes: fetch -> trend -> validation -> critical -> decision."""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import get_settings
from .indicators import snapshot
from .llm import structured
from .market import get_provider, last_price
from .schemas import CriticalOut, DecisionOut, TrendOut, ValidationOut
from .state import AnalysisState
from .timeframes import PIP, cascade

log = logging.getLogger(__name__)

_TREND_SYS = """You are a senior FX top-down analyst. Judge ONLY the higher-timeframe trend.
Rules:
- Use the numeric snapshot given. Do not invent prices.
- EMA stack + swing structure decide bias. Momentum (RSI/MACD hist) decides strength.
- 'range' is a valid and common answer. Do not force a bias.
- rationale <= 60 words, must cite at least two concrete numbers from the snapshot."""

_VALID_SYS = """You are an FX price-action validator. The trend agent already gave a bias.
Decide whether the intermediate timeframe CONFIRMS or CONTRADICTS it, and in which phase.
Rules:
- agrees_with_trend=false when structure or momentum clearly fights the bias.
- invalidation_level = the price that, if broken, kills the trend thesis.
- Be strict: a pullback that respects structure is still agreement."""

_CRIT_SYS = """You are an FX liquidity/order-block mapper on the execution timeframe.
Produce the critical areas that price is reacting to right now.
Rules:
- zones: 1-4 entries, each {type, low, high, why}. type in
  [support, resistance, demand, supply, order_block, fvg, liquidity_sweep].
- Use recent swing highs/lows and round levels from the snapshot.
- nearest_support < last_price < nearest_resistance.
- entry_trigger must be observable price action, not a feeling."""

_DECISION_SYS = """You are the risk manager. You decide, you do not analyse.
Inputs: trend bias, validation verdict, critical zones, ATR, account rules.
Rules:
- Default to NO_TRADE. Trade only when trend and validation agree AND price sits at a critical zone.
- If ATR is tiny relative to the stop distance, or zones are far from price, veto.
- stop_loss must sit beyond the invalidation level; take_profit at the opposite zone.
- rr = |tp-entry| / |entry-sl|, must be >= min_rr else NO_TRADE.
- Every reason/veto must reference a number from the inputs."""


def _fmt(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


async def fetch_node(state: AnalysisState) -> dict:
    s = get_settings()
    provider = get_provider(s.market_provider)
    tf_map = cascade(state["base_tf"])
    snaps: dict[str, dict] = {}
    errors: list[str] = []
    trace: list[str] = []

    for role in ("trend", "validation", "critical"):
        tf = tf_map[role]
        try:
            df = provider.fetch(state["symbol"], tf, bars=300)
            snaps[tf] = snapshot(df, tf)
            trace.append(f"fetched {state['symbol']} {tf} ({len(df)} bars)")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fetch {tf}: {exc}")
            log.exception("fetch failed for %s %s", state["symbol"], tf)

    price = snaps.get(tf_map["critical"], {}).get("last_price") or (
        next(iter(snaps.values()))["last_price"] if snaps else 0.0
    )
    return {
        "trend_tf": tf_map["trend"],
        "validation_tf": tf_map["validation"],
        "critical_tf": tf_map["critical"],
        "snapshots": snaps,
        "price": float(price),
        "errors": errors,
        "trace": trace,
    }


async def trend_node(state: AnalysisState) -> dict:
    snap = state["snapshots"].get(state["trend_tf"])
    if not snap:
        return {"trend": {"bias": "range", "strength": "weak", "key_levels": [],
                          "rationale": "no data"}, "errors": [f"trend {state['trend_tf']}: no data"]}
    prompt = (
        f"{_TREND_SYS}\n\nSymbol: {state['symbol']}\nTimeframe: {state['trend_tf']}\n"
        f"Snapshot:\n{_fmt(snap)}"
    )
    try:
        out: TrendOut = await structured(prompt, TrendOut)
        return {"trend": out.model_dump(), "trace": [f"trend {state['trend_tf']}: {out.bias}/{out.strength}"]}
    except Exception as exc:  # noqa: BLE001
        return {"trend": {"bias": "range", "strength": "weak", "key_levels": [],
                          "rationale": f"agent failed: {exc}"}, "errors": [f"trend: {exc}"]}


async def validation_node(state: AnalysisState) -> dict:
    snap = state["snapshots"].get(state["validation_tf"])
    if not snap:
        return {"validation": {"agrees_with_trend": False, "phase": "consolidation",
                               "pattern": "n/a", "invalidation_level": state.get("price", 0.0),
                               "rationale": "no data"},
                "errors": [f"validation {state['validation_tf']}: no data"]}
    prompt = (
        f"{_VALID_SYS}\n\nSymbol: {state['symbol']}\nTimeframe: {state['validation_tf']}\n"
        f"Trend verdict ({state['trend_tf']}):\n{_fmt(state.get('trend', {}))}\n"
        f"Snapshot:\n{_fmt(snap)}"
    )
    try:
        out: ValidationOut = await structured(prompt, ValidationOut)
        return {"validation": out.model_dump(),
                "trace": [f"validation {state['validation_tf']}: {out.phase} agree={out.agrees_with_trend}"]}
    except Exception as exc:  # noqa: BLE001
        return {"validation": {"agrees_with_trend": False, "phase": "consolidation",
                               "pattern": "n/a", "invalidation_level": state.get("price", 0.0),
                               "rationale": f"agent failed: {exc}"}, "errors": [f"validation: {exc}"]}


async def critical_node(state: AnalysisState) -> dict:
    snap = state["snapshots"].get(state["critical_tf"])
    if not snap:
        return {"critical": {"zones": [], "nearest_support": state.get("price", 0.0),
                             "nearest_resistance": state.get("price", 0.0),
                             "entry_trigger": "n/a", "rationale": "no data"},
                "errors": [f"critical {state['critical_tf']}: no data"]}
    prompt = (
        f"{_CRIT_SYS}\n\nSymbol: {state['symbol']}\nTimeframe: {state['critical_tf']}\n"
        f"Trend: {state.get('trend', {}).get('bias')}\n"
        f"Validation: {state.get('validation', {}).get('phase')}\n"
        f"Snapshot:\n{_fmt(snap)}"
    )
    try:
        out: CriticalOut = await structured(prompt, CriticalOut)
        return {"critical": out.model_dump(),
                "trace": [f"critical {state['critical_tf']}: {len(out.zones)} zones"]}
    except Exception as exc:  # noqa: BLE001
        return {"critical": {"zones": [], "nearest_support": state.get("price", 0.0),
                             "nearest_resistance": state.get("price", 0.0),
                             "entry_trigger": "n/a", "rationale": f"agent failed: {exc}"},
                "errors": [f"critical: {exc}"]}


async def decision_node(state: AnalysisState) -> dict:
    s = get_settings()
    snap = state["snapshots"].get(state["critical_tf"], {})
    atr_pips = snap.get("atr_pips", 0.0)
    prompt = (
        f"{_DECISION_SYS}\n\nSymbol: {state['symbol']}\n"
        f"Execution TF: {state['critical_tf']}\nPrice: {state.get('price')}\n"
        f"ATR({state['critical_tf']}) = {atr_pips} pips\n"
        f"min_rr = {s.min_rr}\nmax_spread_pips = {s.max_spread_pips}\n"
        f"Trend:\n{_fmt(state.get('trend', {}))}\n"
        f"Validation:\n{_fmt(state.get('validation', {}))}\n"
        f"Critical:\n{_fmt(state.get('critical', {}))}"
    )
    try:
        out: DecisionOut = await structured(prompt, DecisionOut)
    except Exception as exc:  # noqa: BLE001
        return {"decision": {"action": "NO_TRADE", "confidence": 0, "reasons": [],
                             "vetoes": [f"decision agent failed: {exc}"]},
                "errors": [f"decision: {exc}"]}

    # deterministic guardrails on top of the LLM
    vetoes = list(out.vetoes)
    if out.action in ("BUY", "SELL") and out.entry and out.stop_loss and out.take_profit:
        risk = abs(out.entry - out.stop_loss)
        reward = abs(out.take_profit - out.entry)
        rr = round(reward / risk, 2) if risk else 0.0
        out.rr = rr
        if risk <= 0 or rr < s.min_rr:
            vetoes.append(f"rr {rr} < min_rr {s.min_rr}")
        if risk / PIP > max(atr_pips * 3, 1e-9):
            vetoes.append(f"stop {round(risk / PIP, 1)} pips > 3x ATR {atr_pips}")
        side_ok = (out.action == "BUY" and out.stop_loss < out.entry < out.take_profit) or (
            out.action == "SELL" and out.take_profit < out.entry < out.stop_loss
        )
        if not side_ok:
            vetoes.append("stop/target on wrong side of entry")
    elif out.action in ("BUY", "SELL"):
        vetoes.append("incomplete trade plan (entry/sl/tp missing)")

    if vetoes and out.action != "NO_TRADE":
        out.action = "NO_TRADE"
        out.confidence = min(out.confidence, 40)

    return {"decision": out.model_dump(), "vetoes": vetoes,
            "trace": [f"decision: {out.action} conf={out.confidence} rr={out.rr}"]}
