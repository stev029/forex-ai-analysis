"""Telegram rendering (Bahasa Indonesia). Plain text + HTML-escaped, no markdown traps.

Tidak ada pemotongan teks: semua rationale/pattern/why/pemicu/reasons/vetoes
ditampilkan utuh. Pesan panjang dipecah per baris oleh `split_message()`.
"""
from __future__ import annotations

import html
from typing import Any

# Label teknis dari LLM tetap Inggris di state; terjemahan hanya di lapisan tampilan.
BIAS_ID = {"bullish": "bullish (naik)", "bearish": "bearish (turun)", "range": "range (sideways)"}
STRENGTH_ID = {"weak": "lemah", "moderate": "sedang", "strong": "kuat"}
PHASE_ID = {
    "continuation": "kelanjutan tren",
    "pullback": "pullback",
    "reversal": "pembalikan arah",
    "consolidation": "konsolidasi",
}
ACTION_ID = {"BUY": "BELI", "SELL": "JUAL", "NO_TRADE": "TIDAK ENTRY"}
ZONE_ID = {
    "support": "support",
    "resistance": "resistensi",
    "demand": "zona demand",
    "supply": "zona supply",
    "order_block": "order block",
    "fvg": "FVG (fair value gap)",
    "liquidity_sweep": "sapuan likuiditas",
}

# Batas keras Telegram ~4096 karakter; sisakan ruang untuk tag HTML.
MAX_CHUNK = 3500


def _p(v: Any, nd: int = 5) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _t(table: dict[str, str], key: Any) -> str:
    raw = str(key or "?").strip().lower()
    return table.get(raw, str(key or "?"))


def _esc(text: Any) -> str:
    """Escape HTML dan rapikan spasi, tanpa memotong apa pun."""
    return html.escape(" ".join(str(text or "").split()))


def split_message(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Pecah pesan panjang per baris utuh; tidak memotong kata maupun tag."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        add = len(line) + 1
        if size + add > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def render(final: dict) -> str:
    d = final.get("decision") or {}
    t = final.get("trend") or {}
    v = final.get("validation") or {}
    c = final.get("critical") or {}
    action = d.get("action", "NO_TRADE")

    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    lines: list[str] = [
        f"{icon} <b>{ACTION_ID.get(action, action)}</b> — {_esc(final.get('symbol'))} "
        f"<code>{_esc(final.get('base_tf'))}</code>",
        f"keyakinan: <b>{d.get('confidence', 0)}</b>/100   harga: <code>{_p(final.get('price'))}</code>",
        "",
        f"<b>1) Tren ({_esc(final.get('trend_tf'))})</b> "
        f"{_t(BIAS_ID, t.get('bias'))} / {_t(STRENGTH_ID, t.get('strength'))}",
        f"   {_esc(t.get('rationale'))}",
        f"<b>2) Validasi ({_esc(final.get('validation_tf'))})</b> "
        f"{_t(PHASE_ID, v.get('phase'))} — sepakat: "
        f"{'ya' if v.get('agrees_with_trend') else 'tidak'}",
        f"   {_esc(v.get('pattern'))} | level invalidasi {_p(v.get('invalidation_level'))}",
        f"<b>3) Area kritis ({_esc(final.get('critical_tf'))})</b>",
        f"   support {_p(c.get('nearest_support'))} | resistensi {_p(c.get('nearest_resistance'))}",
    ]

    for z in c.get("zones") or []:
        lines.append(
            f"   • {_t(ZONE_ID, z.get('type'))} "
            f"{_p(z.get('low'))}–{_p(z.get('high'))}: {_esc(z.get('why'))}"
        )

    lines += ["", "<b>4) Keputusan</b>"]
    if action in ("BUY", "SELL"):
        lines.append(
            f"   entry <code>{_p(d.get('entry'))}</code>  SL <code>{_p(d.get('stop_loss'))}</code>  "
            f"TP <code>{_p(d.get('take_profit'))}</code>  RR <b>{d.get('rr')}</b>"
        )
    else:
        lines.append(f"   pemicu: {_esc(c.get('entry_trigger') or '—')}")

    for r in d.get("reasons") or []:
        lines.append(f"   + {_esc(r)}")
    for vt in d.get("vetoes") or []:
        lines.append(f"   − {_esc(vt)}")

    if final.get("errors"):
        lines += ["", "<i>kesalahan:</i>"] + [f"   {_esc(e)}" for e in final["errors"]]

    return "\n".join(lines)
