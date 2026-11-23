"""Graph nodes: fetch -> trend -> validation -> critical -> decision."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .config import get_settings
from .indicators import snapshot
from .llm import structured
from .market import get_provider, last_price, pip_size
from .market_hours import STALE_AFTER_S, market_state
from .scalp import all_tfs, read as scalp_read
from .schemas import CriticalOut, DecisionOut, TrendOut, ValidationOut
from .state import AnalysisState
from .timeframes import PIP, cascade

log = logging.getLogger(__name__)

# Semua teks yang dilihat pengguna harus Bahasa Indonesia; kunci JSON tetap Inggris.
_LANG = (
    "\n\nTulis semua teks bebas (rationale, pattern, why, entry_trigger, reasons, vetoes) "
    "dalam Bahasa Indonesia yang ringkas. Kunci JSON dan nilai enum tetap Bahasa Inggris."
)

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


def _mem(state: AnalysisState) -> str:
    """Blok riwayat keputusan untuk disuntik ke prompt."""
    mem = state.get("memory") or {}
    text = str(mem.get("text") or "").strip()
    if not text:
        return ""
    return f"\nRiwayat keputusan sebelumnya (ingatan):\n{text}\n"


async def _fetch_df(provider: Any, symbol: str, tf: str, bars: int):
    """Ambil bar tanpa memblokir event loop.

    Provider yang punya `afetch` dipakai langsung; provider sinkron (yfinance)
    dipindah ke thread. Jalur `fetch()` TradingView sengaja tidak dipakai dari
    dalam loop bot karena membuat thread + loop sendiri, dan pola itu di proot
    memicu `OSError: [Errno 38] Function not implemented` / futex abort.
    """
    afetch = getattr(provider, "afetch", None)
    if afetch is not None:
        return await afetch(symbol, tf, bars)
    return await asyncio.to_thread(provider.fetch, symbol, tf, bars)


async def _quote(provider: Any, symbol: str) -> dict:
    aquote = getattr(provider, "aquote", None)
    if aquote is not None:
        return await aquote(symbol)
    return await asyncio.to_thread(provider.quote, symbol)


async def fetch_node(state: AnalysisState) -> dict:
    s = get_settings()
    tf_map = cascade(state["base_tf"])
    pip = pip_size(state["symbol"])
    snaps: dict[str, dict] = {}
    frames: dict[str, Any] = {}
    errors: list[str] = []
    trace: list[str] = []

    names = [s.market_provider]
    if s.market_fallback and s.market_fallback != s.market_provider:
        names.append(s.market_fallback)

    provider = None
    for name in names:
        try:
            candidate = get_provider(name)
            await _fetch_df(candidate, state["symbol"], tf_map["critical"], 60)
        except Exception as exc:  # noqa: BLE001 - provider mati, coba berikutnya
            errors.append(f"provider {name}: {exc}")
            log.warning("provider %s tidak bisa dipakai: %s", name, exc)
            continue
        provider = candidate
        trace.append(f"provider: {name}")
        break

    if provider is None:
        return {
            "trend_tf": tf_map["trend"],
            "validation_tf": tf_map["validation"],
            "critical_tf": tf_map["critical"],
            "snapshots": {},
            "price": 0.0,
            "price_time": "",
            "price_lag_s": None,
            "quote": {},
            "market": market_state().as_dict(),
            "pip": pip,
            "errors": errors,
            "trace": trace + ["fetch: semua provider gagal"],
        }

    for role in ("trend", "validation", "critical"):
        tf = tf_map[role]
        try:
            df = await _fetch_df(provider, state["symbol"], tf, 300)
            frames[tf] = df
            snaps[tf] = snapshot(df, tf, pip=pip)
            trace.append(f"fetched {state['symbol']} {tf} ({len(df)} bars)")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fetch {tf}: {exc}")
            log.exception("fetch failed for %s %s", state["symbol"], tf)

    if state.get("mode") == "scalp":
        for tf in all_tfs():
            if tf in snaps:
                continue
            try:
                df = await _fetch_df(provider, state["symbol"], tf, 300)
                frames[tf] = df
                snaps[tf] = snapshot(df, tf, pip=pip)
                trace.append(f"fetched {state['symbol']} {tf} ({len(df)} bars)")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"fetch {tf}: {exc}")
                log.warning("fetch scalping gagal %s %s: %s", state["symbol"], tf, exc)

    # Harga acuan = quote tick terbaru, bukan close bar terakhir (bisa tertinggal 1-10 menit).
    quote: dict = {}
    try:
        quote = await _quote(provider, state["symbol"])
    except Exception as exc:  # noqa: BLE001 - quote is best effort
        errors.append(f"quote: {exc}")
        log.warning("quote failed for %s: %s", state["symbol"], exc)

    bar_price = snaps.get(tf_map["critical"], {}).get("last_price") or (
        next(iter(snaps.values()))["last_price"] if snaps else 0.0
    )
    price = float(quote.get("price") or bar_price)
    price_time = str(
        quote.get("time")
        or snaps.get(tf_map["critical"], {}).get("last_bar_time")
        or ""
    )
    scalp_block: dict = {}
    if state.get("mode") == "scalp":
        try:
            scalp_block = scalp_read(
                price,
                frames.get("m15"),
                frames.get("m30"),
                frames.get("m1"),
                pip,
            ).as_dict()
            trace.append(f"scalp: area_ideal={scalp_block['ideal']['ok']}")
        except Exception as exc:  # noqa: BLE001 - scalping tidak boleh mematikan cascade
            errors.append(f"scalp: {exc}")
            log.exception("scalp read failed for %s", state["symbol"])
    return {
        "trend_tf": tf_map["trend"],
        "validation_tf": tf_map["validation"],
        "critical_tf": tf_map["critical"],
        "snapshots": snaps,
        "price": price,
        "price_time": price_time,
        "price_lag_s": quote.get("lag_s"),
        "quote": quote,
        "market": market_state().as_dict(),
        "pip": pip,
        "scalp": scalp_block,
        "errors": errors,
        "trace": trace,
    }


async def trend_node(state: AnalysisState) -> dict:
    snap = state["snapshots"].get(state["trend_tf"])
    if not snap:
        return {"trend": {"bias": "range", "strength": "weak", "key_levels": [],
                          "rationale": "tidak ada data"}, "errors": [f"tren {state['trend_tf']}: tidak ada data"]}
    prompt = (
        f"{_TREND_SYS}{_LANG}\n\nSymbol: {state['symbol']}\nTimeframe: {state['trend_tf']}\n"
        f"Snapshot:\n{_fmt(snap)}{_mem(state)}"
    )
    try:
        out: TrendOut = await structured(prompt, TrendOut)
        return {"trend": out.model_dump(), "trace": [f"trend {state['trend_tf']}: {out.bias}/{out.strength}"]}
    except Exception as exc:  # noqa: BLE001
        return {"trend": {"bias": "range", "strength": "weak", "key_levels": [],
                          "rationale": f"agen gagal: {exc}"}, "errors": [f"tren: {exc}"]}


async def validation_node(state: AnalysisState) -> dict:
    snap = state["snapshots"].get(state["validation_tf"])
    if not snap:
        return {"validation": {"agrees_with_trend": False, "phase": "consolidation",
                               "pattern": "n/a", "invalidation_level": state.get("price", 0.0),
                               "rationale": "tidak ada data"},
                "errors": [f"validasi {state['validation_tf']}: tidak ada data"]}
    prompt = (
        f"{_VALID_SYS}{_LANG}\n\nSymbol: {state['symbol']}\nTimeframe: {state['validation_tf']}\n"
        f"Trend verdict ({state['trend_tf']}):\n{_fmt(state.get('trend', {}))}\n"
        f"Snapshot:\n{_fmt(snap)}{_mem(state)}"
    )
    try:
        out: ValidationOut = await structured(prompt, ValidationOut)
        return {"validation": out.model_dump(),
                "trace": [f"validation {state['validation_tf']}: {out.phase} agree={out.agrees_with_trend}"]}
    except Exception as exc:  # noqa: BLE001
        return {"validation": {"agrees_with_trend": False, "phase": "consolidation",
                               "pattern": "n/a", "invalidation_level": state.get("price", 0.0),
                               "rationale": f"agen gagal: {exc}"}, "errors": [f"validasi: {exc}"]}


async def critical_node(state: AnalysisState) -> dict:
    snap = state["snapshots"].get(state["critical_tf"])
    if not snap:
        return {"critical": {"zones": [], "nearest_support": state.get("price", 0.0),
                             "nearest_resistance": state.get("price", 0.0),
                             "entry_trigger": "n/a", "rationale": "tidak ada data"},
                "errors": [f"area kritis {state['critical_tf']}: tidak ada data"]}
    prompt = (
        f"{_CRIT_SYS}{_LANG}\n\nSymbol: {state['symbol']}\nTimeframe: {state['critical_tf']}\n"
        f"Trend: {state.get('trend', {}).get('bias')}\n"
        f"Validation: {state.get('validation', {}).get('phase')}\n"
        f"Snapshot:\n{_fmt(snap)}{_mem(state)}"
    )
    try:
        out: CriticalOut = await structured(prompt, CriticalOut)
        return {"critical": out.model_dump(),
                "trace": [f"critical {state['critical_tf']}: {len(out.zones)} zones"]}
    except Exception as exc:  # noqa: BLE001
        return {"critical": {"zones": [], "nearest_support": state.get("price", 0.0),
                             "nearest_resistance": state.get("price", 0.0),
                             "entry_trigger": "n/a", "rationale": f"agen gagal: {exc}"},
                "errors": [f"area kritis: {exc}"]}


async def decision_node(state: AnalysisState) -> dict:
    s = get_settings()
    if not state.get("snapshots"):
        return {
            "decision": {"action": "NO_TRADE", "confidence": 0, "reasons": [],
                         "vetoes": ["tidak ada data pasar untuk timeframe mana pun"]},
            "vetoes": ["tidak ada data pasar untuk timeframe mana pun"],
            "trace": ["keputusan: NO_TRADE (tanpa data)"],
        }
    snap = state["snapshots"].get(state["critical_tf"], {})
    atr_pips = snap.get("atr_pips", 0.0)
    scalp = state.get("scalp") or {}
    scalp_block = ""
    if state.get("mode") == "scalp" and scalp:
        scalp_block = (
            "\nMODE SCALPING. Konteks h1/h4/d1/w1 hanya latar, BUKAN acuan utama.\n"
            "Acuan utama = area potensial m15/m30. Entry hanya di m1/m5.\n"
            f"Data scalping (deterministik):\n{_fmt(scalp)}\n"
            "Aturan: entry hanya bila area ideal terbentuk (harga di area, area teruji, "
            "wick penolakan searah, kelelahan dorongan). Jika tidak, action wajib NO_TRADE.\n"
        )
    prompt = (
        f"{_DECISION_SYS}{_LANG}\n\nSymbol: {state['symbol']}\n"
        f"Execution TF: {state['critical_tf']}\nPrice: {state.get('price')}\n"
        f"ATR({state['critical_tf']}) = {atr_pips} pips\n"
        f"min_rr = {s.min_rr}\nmax_spread_pips = {s.max_spread_pips}\n"
        f"Trend:\n{_fmt(state.get('trend', {}))}\n"
        f"Validation:\n{_fmt(state.get('validation', {}))}\n"
        f"Critical:\n{_fmt(state.get('critical', {}))}{scalp_block}{_mem(state)}"
    )
    try:
        out: DecisionOut = await structured(prompt, DecisionOut)
    except Exception as exc:  # noqa: BLE001
        return {"decision": {"action": "NO_TRADE", "confidence": 0, "reasons": [],
                             "vetoes": [f"agen keputusan gagal: {exc}"]},
                "errors": [f"decision: {exc}"]}

    # deterministic guardrails on top of the LLM
    vetoes = list(out.vetoes)
    if out.action in ("BUY", "SELL") and out.entry and out.stop_loss and out.take_profit:
        risk = abs(out.entry - out.stop_loss)
        reward = abs(out.take_profit - out.entry)
        rr = round(reward / risk, 2) if risk else 0.0
        out.rr = rr
        pip = state.get("pip") or PIP
        if risk <= 0 or rr < s.min_rr:
            vetoes.append(f"RR {rr} di bawah min_rr {s.min_rr}")
        if risk / pip > max(atr_pips * 3, 1e-9):
            vetoes.append(f"jarak SL {round(risk / pip, 1)} pips melebihi 3x ATR {atr_pips}")
        side_ok = (out.action == "BUY" and out.stop_loss < out.entry < out.take_profit) or (
            out.action == "SELL" and out.take_profit < out.entry < out.stop_loss
        )
        if not side_ok:
            vetoes.append("SL/TP berada di sisi yang salah dari entry")
    elif out.action in ("BUY", "SELL"):
        vetoes.append("rencana trade tidak lengkap (entry/SL/TP kosong)")

    # Harga basi saat bursa buka = rencana trade tidak bisa dipercaya.
    lag = state.get("price_lag_s")
    market = state.get("market") or {}
    if (
        out.action in ("BUY", "SELL")
        and lag is not None
        and market.get("is_open")
        and int(lag) > STALE_AFTER_S
    ):
        vetoes.append(
            f"harga tertinggal {int(lag) // 60} menit padahal pasar buka "
            f"(batas {STALE_AFTER_S // 60} menit)"
        )

    # Mode scalping: area ideal adalah gerbang keras, bukan saran.
    if state.get("mode") == "scalp" and out.action in ("BUY", "SELL"):
        ideal = (scalp.get("ideal") or {})
        if not ideal.get("ok"):
            missing = ", ".join(ideal.get("missing") or []) or "area ideal belum terbentuk"
            vetoes.append(f"scalping: area ideal belum terbentuk ({missing})")
        elif scalp.get("plan", {}).get("side") in ("buy", "sell"):
            side = scalp["plan"]["side"]
            if (side == "buy") != (out.action == "BUY"):
                vetoes.append(
                    f"scalping: area menuntut {side.upper()} tapi agen memilih {out.action}"
                )

    if vetoes and out.action != "NO_TRADE":
        out.action = "NO_TRADE"
        out.confidence = min(out.confidence, 40)

    return {"decision": out.model_dump(), "vetoes": vetoes,
            "trace": [f"decision: {out.action} conf={out.confidence} rr={out.rr}"]}
