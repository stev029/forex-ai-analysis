"""Telegram rendering (Bahasa Indonesia). Plain text + HTML-escaped, no markdown traps."""
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


def _p(v: Any, nd: int = 5) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _t(table: dict[str, str], key: Any) -> str:
    raw = str(key or "?").strip().lower()
    return table.get(raw, str(key or "?"))


def render(final: dict) -> str:
    d = final.get("decision") or {}
    t = final.get("trend") or {}
    v = final.get("validation") or {}
    c = final.get("critical") or {}
    action = d.get("action", "NO_TRADE")

    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    lines: list[str] = [
        f"{icon} <b>{ACTION_ID.get(action, action)}</b> — {html.escape(str(final.get('symbol', '')))} "
        f"<code>{html.escape(str(final.get('base_tf', '')))}</code>",
        f"keyakinan: <b>{d.get('confidence', 0)}</b>/100   harga: <code>{_p(final.get('price'))}</code>",
        "",
        f"<b>1) Tren ({html.escape(str(final.get('trend_tf', '')))})</b> "
        f"{_t(BIAS_ID, t.get('bias'))} / {_t(STRENGTH_ID, t.get('strength'))}",
        f"   {html.escape(str(t.get('rationale', '')))}",
        f"<b>2) Validasi ({html.escape(str(final.get('validation_tf', '')))})</b> "
        f"{_t(PHASE_ID, v.get('phase'))} — sepakat: "
        f"{'ya' if v.get('agrees_with_trend') else 'tidak'}",
        f"   {html.escape(str(v.get('pattern', '')))} | level invalidasi {_p(v.get('invalidation_level'))}",
        f"<b>3) Area kritis ({html.escape(str(final.get('critical_tf', '')))})</b>",
        f"   support {_p(c.get('nearest_support'))} | resistensi {_p(c.get('nearest_resistance'))}",
    ]

    for z in (c.get("zones") or [])[:4]:
        lines.append(
            f"   • {_t(ZONE_ID, z.get('type'))} "
            f"{_p(z.get('low'))}–{_p(z.get('high'))}: {html.escape(str(z.get('why', ''))[:80])}"
        )

    lines += ["", "<b>4) Keputusan</b>"]
    if action in ("BUY", "SELL"):
        lines += [
            f"   entry <code>{_p(d.get('entry'))}</code>  SL <code>{_p(d.get('stop_loss'))}</code>  "
            f"TP <code>{_p(d.get('take_profit'))}</code>  RR <b>{d.get('rr')}</b>",
        ]
    else:
        lines.append(f"   pemicu: {html.escape(str(c.get('entry_trigger', '—'))[:120])}")

    for r in (d.get("reasons") or [])[:4]:
        lines.append(f"   + {html.escape(str(r)[:120])}")
    for vt in (d.get("vetoes") or [])[:4]:
        lines.append(f"   − {html.escape(str(vt)[:120])}")

    if final.get("errors"):
        lines += ["", "<i>kesalahan:</i>"] + [
            f"   {html.escape(str(e)[:100])}" for e in final["errors"][:3]
        ]

    return "\n".join(lines)
