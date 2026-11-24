"""Saran entry deterministik: skenario BUY dan SELL lengkap dengan SL/TP/RR.

Selalu dihitung, termasuk saat keputusan NO_TRADE, supaya trader tetap punya
angka acuan: berapa entry, di mana SL, di mana TP, dan berapa RR-nya.
Semua level berasal dari struktur (swing, zona kritis, ATR) — bukan karangan LLM.
"""
from __future__ import annotations

from typing import Any

from .timeframes import PIP

# SL minimal 0.5 x ATR di balik level struktur.
BUFFER_ATR = 0.5
# Batas risiko wajar: 3 x ATR (sama dengan guardrail decision node).
MAX_RISK_ATR = 3.0
# TP minimum bila struktur tidak menyediakan zona lawan.
FALLBACK_RR = 2.0


def _f(v: Any, nd: int = 5) -> float | None:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return round(out, nd)


def _levels(state: dict) -> dict[str, float]:
    """Kumpulkan level struktur yang layak jadi acuan SL/TP."""
    snap = (state.get("snapshots") or {}).get(state.get("critical_tf") or "", {}) or {}
    crit = state.get("critical") or {}
    highs = [float(v) for v in (snap.get("recent_highs") or []) if v is not None]
    lows = [float(v) for v in (snap.get("recent_lows") or []) if v is not None]
    zones = crit.get("zones") or []
    z_high = [float(z["high"]) for z in zones if z.get("high") is not None]
    z_low = [float(z["low"]) for z in zones if z.get("low") is not None]
    support = _f(crit.get("nearest_support"))
    resistance = _f(crit.get("nearest_resistance"))
    if support:
        lows.append(support)
    if resistance:
        highs.append(resistance)
    return {
        "swing_high": max(highs) if highs else _f(snap.get("range_high")) or 0.0,
        "swing_low": min(lows) if lows else _f(snap.get("range_low")) or 0.0,
        "zone_high": min(z_high) if z_high else 0.0,
        "zone_low": max(z_low) if z_low else 0.0,
        "support": support or 0.0,
        "resistance": resistance or 0.0,
        "range_high": _f(snap.get("range_high")) or 0.0,
        "range_low": _f(snap.get("range_low")) or 0.0,
    }


def _nearest(values: list[float], price: float, side: str) -> float | None:
    """Level struktur terdekat di sisi yang benar: di bawah harga untuk buy, di atas untuk sell."""
    if side == "buy":
        pool = [v for v in values if v and v < price]
        return max(pool) if pool else None
    pool = [v for v in values if v and v > price]
    return min(pool) if pool else None


def _scenario(side: str, price: float, pip: float, atr: float, buf: float,
              lv: dict[str, float], min_rr: float) -> dict[str, Any]:
    if side == "buy":
        anchor = _nearest([lv["swing_low"], lv["support"], lv["zone_low"]], price, "buy")
        if anchor is None or (price - anchor) > atr * MAX_RISK_ATR:
            anchor = price - min(buf, atr)
        stop = anchor - buf
        target = _nearest([lv["resistance"], lv["zone_high"], lv["swing_high"],
                           lv["range_high"]], price, "buy")
        if target is None or (target - price) < (price - stop) * min_rr:
            target = price + max((price - stop) * FALLBACK_RR, buf * 4)
    else:
        anchor = _nearest([lv["swing_high"], lv["resistance"], lv["zone_high"]], price, "sell")
        if anchor is None or (anchor - price) > atr * MAX_RISK_ATR:
            anchor = price + min(buf, atr)
        stop = anchor + buf
        target = _nearest([lv["support"], lv["zone_low"], lv["swing_low"],
                           lv["range_low"]], price, "sell")
        if target is None or (price - target) < (stop - price) * min_rr:
            target = price - max((stop - price) * FALLBACK_RR, buf * 4)

    risk = abs(price - stop)
    reward = abs(target - price)
    rr = round(reward / risk, 2) if risk else 0.0
    risk_pips = round(risk / pip, 1) if pip else 0.0
    reward_pips = round(reward / pip, 1) if pip else 0.0

    note = ""
    if risk > atr * MAX_RISK_ATR:
        note = f"SL {risk_pips} pips > {MAX_RISK_ATR:g}x ATR, perlebar TP atau tunggu pullback"
    elif rr < min_rr:
        need = round(risk * min_rr, 5)
        note = f"RR {rr} < min {min_rr}; TP minimal {need} agar layak"

    return {
        "side": side,
        "entry": _f(price),
        "stop_loss": _f(stop),
        "take_profit": _f(target),
        "rr": rr,
        "risk_pips": risk_pips,
        "reward_pips": reward_pips,
        "valid": bool(rr >= min_rr and risk <= atr * MAX_RISK_ATR),
        "note": note,
    }


def build(state: dict, min_rr: float | None = None) -> dict[str, Any]:
    """Dua skenario (BUY & SELL) + penanda mana yang searah tren."""
    price = _f(state.get("price")) or 0.0
    if price <= 0:
        return {"ok": False, "reason": "harga tidak tersedia", "primary": None, "alt": None}
    pip = float(state.get("pip") or PIP)
    snap = (state.get("snapshots") or {}).get(state.get("critical_tf") or "", {}) or {}
    atr_pips = float(snap.get("atr_pips") or 0.0)
    atr = atr_pips * pip
    if atr <= 0:
        atr = pip * 5
    buf = max(atr * BUFFER_ATR, pip)
    lv = _levels(state)
    rr_min = float(min_rr if min_rr is not None else 1.5)

    buy = _scenario("buy", price, pip, atr, buf, lv, rr_min)
    sell = _scenario("sell", price, pip, atr, buf, lv, rr_min)

    bias = str((state.get("trend") or {}).get("bias") or "").lower()
    aligned = "buy" if bias == "bullish" else "sell" if bias == "bearish" else ""
    primary = buy if aligned == "buy" else sell if aligned == "sell" else None
    alt = sell if aligned == "buy" else buy if aligned == "sell" else None
    for row in (buy, sell):
        row["aligned"] = row["side"] == aligned
    if primary is None:
        primary = buy if buy["rr"] >= sell["rr"] else sell
        alt = sell if primary is buy else buy
    return {
        "ok": True,
        "bias": bias or "range",
        "aligned": aligned or "none",
        "atr_pips": round(atr_pips, 1),
        "buffer": _f(buf),
        "levels": {k: _f(v) for k, v in lv.items()},
        "primary": primary,
        "alt": alt,
        "buy": buy,
        "sell": sell,
        "min_rr": rr_min,
    }


def as_text(block: dict[str, Any]) -> str:
    """Ringkasan untuk prompt LLM."""
    if not block.get("ok"):
        return f"saran: {block.get('reason')}"
    rows = []
    for key in ("primary", "alt"):
        row = block.get(key) or {}
        if not row:
            continue
        rows.append(
            f"{key} {row['side'].upper()} entry {row['entry']} SL {row['stop_loss']} "
            f"TP {row['take_profit']} RR {row['rr']} "
            f"({row['risk_pips']} pips risiko / {row['reward_pips']} pips reward, "
            f"{'layak' if row['valid'] else 'belum layak'})"
        )
    return "\n".join(rows)
