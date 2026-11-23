"""Mode analisis yang bisa di-switch kapan pun.

`cascade` = perilaku lama (tren base+2, validasi base+1, area kritis base).
`scalp`   = konteks h1/h4/d1/w1 hanya latar, area m15/m30 jadi acuan, entry m1/m5.
"""
from __future__ import annotations

MODE_CASCADE = "cascade"
MODE_SCALP = "scalp"
MODES = (MODE_CASCADE, MODE_SCALP)

ALIASES = {
    "cascade": MODE_CASCADE,
    "normal": MODE_CASCADE,
    "standar": MODE_CASCADE,
    "default": MODE_CASCADE,
    "scalp": MODE_SCALP,
    "scalping": MODE_SCALP,
    "scalper": MODE_SCALP,
    "fast": MODE_SCALP,
}


def normalize_mode(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return MODE_CASCADE
    if key not in ALIASES:
        raise ValueError(
            f"mode tidak dikenal {raw!r}; pakai salah satu {list(MODES)}"
        )
    return ALIASES[key]


def describe(mode: str) -> str:
    if normalize_mode(mode) == MODE_SCALP:
        return (
            "scalp: konteks h1/h4/d1/w1 (latar saja) → area m15/m30 (acuan) → "
            "entry m1/m5 (hanya bila area ideal terbentuk)"
        )
    return "cascade: tren base+2 → validasi base+1 → area kritis base → keputusan"
