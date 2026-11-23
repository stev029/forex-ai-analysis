"""Timeframe ladder + cascade resolution.

User asks for m1 -> analyse m15 (trend) -> m5 (validation) -> m1 (critical) -> decision.
So for a base TF the two context TFs are: 2 steps up (trend) and 1 step up (validation).
"""
from __future__ import annotations

LADDER: list[str] = ["m1", "m5", "m15", "m30", "h1", "h4", "d1", "w1"]

# `auto` bukan TF nyata: grafik memilih TF sendiri lewat autotf.choose().
AUTO = "auto"

ALIASES = {
    "1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30",
    "1h": "h1", "4h": "h4", "1d": "d1", "1w": "w1",
    "m1": "m1", "m5": "m5", "m15": "m15", "m30": "m30",
    "h1": "h1", "h4": "h4", "d1": "d1", "w1": "w1",
    "auto": AUTO, "otomatis": AUTO, "autotf": AUTO, "auto-tf": AUTO,
}

# yfinance interval + period per timeframe
YF_MAP: dict[str, tuple[str, str]] = {
    "m1": ("1m", "7d"),
    "m5": ("5m", "60d"),
    "m15": ("15m", "60d"),
    "m30": ("30m", "60d"),
    "h1": ("60m", "730d"),
    "h4": ("60m", "730d"),
    "d1": ("1d", "5y"),
    "w1": ("1wk", "10y"),
}

PIP = 0.0001


def normalize_tf(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key not in ALIASES:
        raise ValueError(f"unknown timeframe {raw!r}; use one of {sorted(set(ALIASES))}")
    return ALIASES[key]


def cascade(base_tf: str) -> dict[str, str]:
    """Return {'trend': tf, 'validation': tf, 'critical': tf} for a base timeframe."""
    base = normalize_tf(base_tf)
    if base == AUTO:
        raise ValueError("timeframe auto tidak punya cascade tetap; pakai autotf.choose()")
    idx = LADDER.index(base)
    trend_idx = min(idx + 2, len(LADDER) - 1)
    val_idx = min(idx + 1, len(LADDER) - 1)
    return {
        "trend": LADDER[trend_idx],
        "validation": LADDER[val_idx],
        "critical": base,
    }
