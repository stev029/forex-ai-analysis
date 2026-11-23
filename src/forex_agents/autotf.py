"""Pemilihan timeframe otomatis: skor tiap TF, pilih yang paling ideal untuk entry.

Semua deterministik. LLM hanya menerima hasil skor, jadi tidak bisa mengarang TF.
Dipakai saat `base_tf == "auto"`; TF terbaik jadi acuan entry, dan TF lain yang
skornya juga lolos ambang dilaporkan sebagai saran multi-timeframe.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .indicators import atr, macd, rsi, snapshot, structure, swing_points
from .timeframes import LADDER, cascade

AUTO = "auto"
IDEAL_SCORE = 60
STRONG_SCORE = 75

# Bobot tiap komponen; total 100.
W_TREND = 25
W_MOMENTUM = 20
W_AREA = 25
W_WICK = 15
W_EXHAUST = 15
W_FRESH = 10
W_VOL = 10

TF_SECONDS = {
    "m1": 60, "m5": 300, "m15": 900, "m30": 1800,
    "h1": 3600, "h4": 14400, "d1": 86400, "w1": 604800,
}


def is_auto(raw: str | None) -> bool:
    return (raw or "").strip().lower() in (AUTO, "otomatis", "auto-tf", "autotf")


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _wick_stats(df: pd.DataFrame, bars: int = 3) -> dict[str, Any]:
    tail = df.tail(max(bars, 1))
    bull = bear = 0
    last = "none"
    for _, row in tail.iterrows():
        o, h, l, c = (float(row["open"]), float(row["high"]),
                      float(row["low"]), float(row["close"]))
        rng = h - l
        body = max(_body(o, c), 1e-12)
        upper = h - max(o, c)
        lower = min(o, c) - l
        pos = (c - l) / rng if rng else 0.5
        if upper >= 1.5 * body and pos <= 0.5:
            bear += 1
            last = "upper"
        if lower >= 1.5 * body and pos >= 0.5:
            bull += 1
            last = "lower"
    return {"bullish": bull, "bearish": bear, "last": last}


def _lag_s(df: pd.DataFrame, tf: str, now: datetime | None = None) -> int | None:
    if df is None or len(df) == 0:
        return None
    stamp = df.index[-1]
    if not isinstance(stamp, pd.Timestamp):
        return None
    moment = now or datetime.now(timezone.utc)
    return max(int((moment - stamp.to_pydatetime()).total_seconds()), 0)


def score_tf(df: pd.DataFrame, tf: str, price: float, pip: float,
             now: datetime | None = None) -> dict[str, Any]:
    """Skor 0-100 kelayakan satu timeframe sebagai acuan entry."""
    snap = snapshot(df, tf, pip=pip)
    close = df["close"].tail(120)
    atr_v = float(atr(df.tail(120)).iloc[-1])
    atr_pips = atr_v / pip if pip else 0.0
    rsi_v = float(rsi(close).iloc[-1])
    _, _, hist = macd(close)
    hist_now = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) > 1 else hist_now
    decay = abs(hist_now) < abs(hist_prev)
    struct = structure(df.tail(120))
    wick = _wick_stats(df)
    lag = _lag_s(df, tf, now)
    tf_s = TF_SECONDS.get(tf, 60)
    fresh = lag is not None and lag <= tf_s * 2

    highs, lows = swing_points(df.tail(160))
    levels = highs + lows
    nearest = min((abs(price - lv) for lv in levels), default=float("inf"))
    near_area = atr_v > 0 and nearest <= atr_v * 0.5

    exhaust = 0
    if rsi_v >= 70 or rsi_v <= 30:
        exhaust += 1
    if decay:
        exhaust += 1
    consecutive = 1
    closes = [float(v) for v in df["close"].tail(10)]
    if len(closes) >= 2:
        direction = 1 if closes[-1] > closes[0] else -1 if closes[-1] < closes[0] else 0
        for i in range(len(closes) - 1, 0, -1):
            step = closes[i] - closes[i - 1]
            if direction and (step * direction) > 0:
                consecutive += 1
            else:
                break
    if consecutive >= 4:
        exhaust += 1

    parts: dict[str, float] = {}
    # Pasar benar-benar datar (ATR 0 / variansi 0) tidak punya keunggulan apa pun.
    flat = atr_v <= 0 or float(close.std(ddof=0) or 0.0) <= max(abs(price), 1.0) * 1e-9
    parts["trend"] = 0.0 if flat else W_TREND * (1.0 if snap["ema_stack"] != "mixed" else 0.0)
    parts["momentum"] = 0.0 if flat else W_MOMENTUM * min(abs(rsi_v - 50) / 25.0, 1.0)
    parts["area"] = 0.0 if flat else W_AREA * (1.0 if near_area else 0.0)
    parts["wick"] = 0.0 if flat else W_WICK * (1.0 if wick["last"] != "none" else 0.0)
    parts["exhaust"] = 0.0 if flat else W_EXHAUST * (exhaust / 3.0)
    parts["fresh"] = 0.0 if flat else W_FRESH * (1.0 if fresh else 0.0)
    parts["vol"] = 0.0 if flat else W_VOL * (1.0 if atr_pips >= 3 else atr_pips / 3.0)

    score = int(round(sum(parts.values())))
    reasons: list[str] = []
    if flat:
        reasons.append("pasar datar (ATR/variasi nol)")
    if parts["trend"]:
        reasons.append(f"ema stack {snap['ema_stack']}")
    if near_area:
        reasons.append(f"harga {round(nearest / pip, 1) if pip else 0} pips dari swing")
    if wick["last"] != "none":
        reasons.append(f"wick {wick['last']} x{wick['bullish'] + wick['bearish']}")
    if exhaust:
        reasons.append(f"kelelahan {exhaust}/3")
    if not fresh:
        reasons.append(f"bar tertinggal {lag}s" if lag is not None else "waktu bar tidak diketahui")
    if atr_pips < 3:
        reasons.append(f"ATR {round(atr_pips, 1)} pips terlalu tipis")
    if not reasons:
        reasons.append("tidak ada keunggulan terukur")

    return {
        "tf": tf,
        "score": score,
        "ideal": score >= IDEAL_SCORE,
        "strong": score >= STRONG_SCORE,
        "parts": {k: round(v, 1) for k, v in parts.items()},
        "checks": {
            "tren_jelas": snap["ema_stack"] != "mixed",
            "momentum": round(abs(rsi_v - 50), 1) >= 8,
            "dekat_area": near_area,
            "wick_penolakan": wick["last"] != "none",
            "kelelahan": exhaust >= 2,
            "bar_segar": fresh,
            "volatilitas": atr_pips >= 3,
            "tidak_datar": not flat,
        },
        "atr_pips": round(atr_pips, 1),
        "rsi14": round(rsi_v, 2),
        "lag_s": lag,
        "structure": struct,
        "reasons": reasons,
    }


def choose(frames: dict[str, pd.DataFrame], price: float, pip: float,
           now: datetime | None = None) -> dict[str, Any]:
    """Skor semua TF yang datanya ada, kembalikan pilihan utama + saran multi-TF."""
    scored: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    for tf in LADDER:
        df = frames.get(tf)
        if df is None or len(df) < 30:
            skipped[tf] = "data kurang"
            continue
        try:
            scored.append(score_tf(df, tf, price, pip, now=now))
        except Exception as exc:  # noqa: BLE001 - satu TF rusak tidak boleh memblokir
            skipped[tf] = f"{type(exc).__name__}: {exc}"
    ranked = sorted(scored, key=lambda s: (-s["score"], LADDER.index(s["tf"])))
    ideal = [s for s in ranked if s["ideal"]]
    best = ranked[0] if ranked else None
    primary = best["tf"] if best else ""
    tf_map = cascade(primary) if primary else {"trend": "", "validation": "", "critical": ""}
    return {
        "primary": primary,
        "trend_tf": tf_map["trend"],
        "validation_tf": tf_map["validation"],
        "critical_tf": tf_map["critical"],
        "ideal": [s["tf"] for s in ideal],
        "multi": len(ideal) >= 2,
        "ranked": ranked,
        "skipped": skipped,
        "threshold": IDEAL_SCORE,
    }


def as_text(block: dict[str, Any]) -> str:
    """Ringkasan satu baris per TF untuk prompt LLM."""
    lines = []
    for row in block.get("ranked") or []:
        lines.append(
            f"{row['tf']}: skor {row['score']} "
            f"({'ideal' if row['ideal'] else 'belum'}) — {'; '.join(row['reasons'])}"
        )
    return "\n".join(lines)
