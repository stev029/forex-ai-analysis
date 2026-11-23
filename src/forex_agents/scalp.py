"""Teknik scalping: konteks besar → area m15/m30 → entry m1/m5.

Semua deteksi di sini deterministik (tanpa LLM) supaya bisa diuji dan angka yang
dilihat agen selalu bisa direproduksi. LLM hanya menilai, bukan mengarang level.

Prinsip yang dipakai:
- Harga tidak naik/turun dengan mudah, jadi cari dulu dorongan yang sudah ada.
- Ujung dorongan ditandai wick (rejection), bukan body.
- Entry hanya sah kalau area ideal sudah terbentuk DAN ada dorongan terakhir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .indicators import atr, macd, rsi, swing_points

WICK_RATIO = 1.5
REJECT_BARS = 3
PUSH_LOOKBACK = 10
AREA_TOL_ATR = 0.75
IDEAL_TOUCHES = 2
EXHAUST_MIN = 2


def _r(value: float, nd: int = 5) -> float:
    return round(float(value), nd)


def _body(o: float, c: float) -> float:
    return abs(c - o)


def candle_stats(df: pd.DataFrame, bars: int = REJECT_BARS) -> list[dict[str, Any]]:
    """Statistik body/wick beberapa candle terakhir."""
    tail = df.tail(max(bars, 1))
    out: list[dict[str, Any]] = []
    for ts, row in tail.iterrows():
        o, h, l, c = (float(row["open"]), float(row["high"]),
                      float(row["low"]), float(row["close"]))
        rng = h - l
        body = _body(o, c)
        upper = h - max(o, c)
        lower = min(o, c) - l
        out.append(
            {
                "time": str(ts),
                "open": _r(o),
                "high": _r(h),
                "low": _r(l),
                "close": _r(c),
                "body": _r(body),
                "upper_wick": _r(upper),
                "lower_wick": _r(lower),
                "close_pos": _r((c - l) / rng if rng else 0.5, 3),
            }
        )
    return out


def wick_rejection(df: pd.DataFrame, bars: int = REJECT_BARS,
                   ratio: float = WICK_RATIO) -> dict[str, Any]:
    """Hitung wick penolakan di ujung atas/bawah."""
    stats = candle_stats(df, bars)
    bull = bear = 0
    last = "none"
    best = 0.0
    for candle in stats:
        body = max(candle["body"], 1e-9)
        upper = candle["upper_wick"]
        lower = candle["lower_wick"]
        if upper >= ratio * body and candle["close_pos"] <= 0.5:
            bear += 1
            last = "upper"
            best = max(best, upper / body)
        if lower >= ratio * body and candle["close_pos"] >= 0.5:
            bull += 1
            last = "lower"
            best = max(best, lower / body)
    return {
        "bullish": bull,
        "bearish": bear,
        "last": last,
        "ratio": _r(best, 2),
        "candles": stats,
    }


def push(df: pd.DataFrame, lookback: int = PUSH_LOOKBACK) -> dict[str, Any]:
    """Dorongan terakhir: arah, besar, dan jumlah candle searah berturut-turut."""
    tail = df.tail(max(lookback, 2))
    closes = [float(v) for v in tail["close"]]
    if len(closes) < 2:
        return {"direction": "flat", "net": 0.0, "consecutive": 0, "decay": False}
    net = closes[-1] - closes[0]
    direction = "up" if net > 0 else "down" if net < 0 else "flat"
    consecutive = 1
    for i in range(len(closes) - 1, 0, -1):
        step = closes[i] - closes[i - 1]
        if (direction == "up" and step > 0) or (direction == "down" and step < 0):
            consecutive += 1
        else:
            break
    _, _, hist = macd(tail["close"])
    decay = bool(len(hist) >= 2 and abs(float(hist.iloc[-1])) < abs(float(hist.iloc[-2])))
    return {
        "direction": direction,
        "net": _r(net),
        "consecutive": consecutive,
        "decay": decay,
    }


def exhaustion(df: pd.DataFrame) -> dict[str, Any]:
    """Skor kelelahan dorongan 0-3: RSI ekstrem, hist mengecil, candle searah panjang."""
    tail = df.tail(120)
    close = tail["close"]
    rsi_v = float(rsi(close).iloc[-1])
    p = push(df)
    score = 0
    if rsi_v >= 70 or rsi_v <= 30:
        score += 1
    if p["decay"]:
        score += 1
    if p["consecutive"] >= 4:
        score += 1
    return {
        "score": score,
        "rsi14": _r(rsi_v, 2),
        "decay": p["decay"],
        "consecutive": p["consecutive"],
        "direction": p["direction"],
    }


def _cluster(levels: list[float], tol: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for level in sorted(levels):
        if groups and abs(level - groups[-1][-1]) <= tol:
            groups[-1].append(level)
        else:
            groups.append([level])
    return groups


def potential_area(df_area: pd.DataFrame, df_confirm: pd.DataFrame | None,
                   price: float) -> dict[str, Any]:
    """Zona potensial dari swing m15/m30 yang di-cluster, plus jumlah sentuhan."""
    tol = max(float(atr(df_area.tail(120)).iloc[-1]) * AREA_TOL_ATR, 1e-9)
    highs, lows = swing_points(df_area.tail(160))
    highs2: list[float] = []
    lows2: list[float] = []
    if df_confirm is not None and len(df_confirm) > 0:
        highs2, lows2 = swing_points(df_confirm.tail(160))
    levels = highs + lows + highs2 + lows2
    groups = [g for g in _cluster(levels, tol) if len(g) >= 1]
    zones = [
        {"low": _r(min(g)), "high": _r(max(g)), "touches": len(g)}
        for g in groups
    ]
    below = [z for z in zones if z["high"] <= price]
    above = [z for z in zones if z["low"] >= price]
    support = max(below, key=lambda z: z["high"]) if below else None
    resistance = min(above, key=lambda z: z["low"]) if above else None
    inside = [z for z in zones if z["low"] <= price <= z["high"]]
    active = inside[0] if inside else (support or resistance)
    side = "none"
    if inside:
        side = "buy" if (support and abs(price - support["high"]) < abs((resistance or support)["low"] - price)) else "sell"
    return {
        "zones": zones[:6],
        "support": support,
        "resistance": resistance,
        "active": active,
        "inside": bool(inside),
        "side": side,
        "tolerance": _r(tol),
        "touches": int(active["touches"]) if active else 0,
    }


def ideal_area(area: dict[str, Any], entry_df: pd.DataFrame,
               price: float) -> dict[str, Any]:
    """Gerbang utama: area ideal hanya true kalau semua syarat terpenuhi."""
    rejection = wick_rejection(entry_df)
    exhaust = exhaustion(entry_df)
    inside = bool(area.get("inside"))
    touches = int(area.get("touches") or 0)
    aligned = (
        (area.get("side") == "buy" and rejection["last"] == "lower")
        or (area.get("side") == "sell" and rejection["last"] == "upper")
    )
    checks = {
        "harga_di_area": inside,
        "area_teruji": touches >= IDEAL_TOUCHES,
        "wick_penolakan": rejection["last"] != "none",
        "wick_searah_area": aligned,
        "kelelahan": exhaust["score"] >= EXHAUST_MIN,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "rejection": rejection,
        "exhaustion": exhaust,
        "missing": [k for k, v in checks.items() if not v],
    }


def plan(price: float, area: dict[str, Any], entry_df: pd.DataFrame,
         pip: float) -> dict[str, Any]:
    """Rencana entry deterministik: entry, SL di balik wick, TP di zona lawan."""
    active = area.get("active") or {}
    low = float(active.get("low") or price)
    high = float(active.get("high") or price)
    side = area.get("side")
    stats = candle_stats(entry_df, 2)
    last = stats[-1] if stats else {}
    atr_v = float(atr(entry_df.tail(120)).iloc[-1]) if len(entry_df) > 5 else 0.0
    buf = max(atr_v * 0.5, pip)
    if side == "buy":
        entry = price
        stop = min(low, float(last.get("low") or low)) - buf
        target = (area.get("resistance") or {}).get("low") or (high + buf * 3)
    elif side == "sell":
        entry = price
        stop = max(high, float(last.get("high") or high)) + buf
        target = (area.get("support") or {}).get("high") or (low - buf * 3)
    else:
        return {"side": "none", "entry": None, "stop_loss": None,
                "take_profit": None, "rr": None, "buffer": _r(buf)}
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return {
        "side": side,
        "entry": _r(entry),
        "stop_loss": _r(stop),
        "take_profit": _r(target),
        "rr": _r(reward / risk, 2) if risk else 0.0,
        "buffer": _r(buf),
    }


@dataclass
class ScalpRead:
    """Ringkasan deterministik satu simbol untuk mode scalping."""

    price: float
    area: dict[str, Any]
    ideal: dict[str, Any]
    plan: dict[str, Any]
    push: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "area": self.area,
            "ideal": self.ideal,
            "plan": self.plan,
            "push": self.push,
        }


def read(price: float, df_area: pd.DataFrame | None, df_confirm: pd.DataFrame | None,
         df_entry: pd.DataFrame | None, pip: float) -> ScalpRead:
    """Ringkas satu simbol. TF yang gagal di-fetch dilewati, bukan bikin crash."""
    area_df = df_area if df_area is not None and len(df_area) > 0 else None
    if area_df is None:
        area_df = df_confirm if df_confirm is not None and len(df_confirm) > 0 else None
    if area_df is None:
        raise ValueError("tidak ada data m15/m30 untuk menghitung area potensial")
    entry_df = df_entry if df_entry is not None and len(df_entry) > 0 else area_df
    area = potential_area(area_df, df_confirm, price)
    ideal = ideal_area(area, entry_df, price)
    return ScalpRead(
        price=price,
        area=area,
        ideal=ideal,
        plan=plan(price, area, entry_df, pip),
        push=push(entry_df),
    )


CONTEXT_TFS = ("h1", "h4", "d1", "w1")
AREA_TFS = ("m15", "m30")
ENTRY_TFS = ("m1", "m5")


def scalp_tfs() -> dict[str, tuple[str, ...]]:
    """TF yang dipakai mode scalping, dipisah per peran."""
    return {"context": CONTEXT_TFS, "area": AREA_TFS, "entry": ENTRY_TFS}


def all_tfs() -> tuple[str, ...]:
    return ENTRY_TFS + AREA_TFS + CONTEXT_TFS
