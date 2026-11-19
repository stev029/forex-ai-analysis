"""Telegram rendering. Plain text + HTML-escaped, no markdown parse traps."""
from __future__ import annotations

import html
from typing import Any


def _p(v: Any, nd: int = 5) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def render(final: dict) -> str:
    d = final.get("decision") or {}
    t = final.get("trend") or {}
    v = final.get("validation") or {}
    c = final.get("critical") or {}
    action = d.get("action", "NO_TRADE")

    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    lines: list[str] = [
        f"{icon} <b>{html.escape(action)}</b> — {html.escape(str(final.get('symbol', '')))} "
        f"<code>{html.escape(str(final.get('base_tf', '')))}</code>",
        f"confidence: <b>{d.get('confidence', 0)}</b>/100   price: <code>{_p(final.get('price'))}</code>",
        "",
        f"<b>1) Trend ({html.escape(str(final.get('trend_tf', '')))} )</b> "
        f"{html.escape(str(t.get('bias', '?')))} / {html.escape(str(t.get('strength', '?')))}",
        f"   {html.escape(str(t.get('rationale', '')))}",
        f"<b>2) Validation ({html.escape(str(final.get('validation_tf', '')))} )</b> "
        f"{html.escape(str(v.get('phase', '?')))} — agree: {v.get('agrees_with_trend')}",
        f"   {html.escape(str(v.get('pattern', '')))} | invalidation {_p(v.get('invalidation_level'))}",
        f"<b>3) Critical ({html.escape(str(final.get('critical_tf', '')))} )</b>",
        f"   support {_p(c.get('nearest_support'))} | resistance {_p(c.get('nearest_resistance'))}",
    ]

    for z in (c.get("zones") or [])[:4]:
        lines.append(
            f"   • {html.escape(str(z.get('type', '?')))} "
            f"{_p(z.get('low'))}–{_p(z.get('high'))}: {html.escape(str(z.get('why', ''))[:80])}"
        )

    lines += ["", "<b>4) Decision</b>"]
    if action in ("BUY", "SELL"):
        lines += [
            f"   entry <code>{_p(d.get('entry'))}</code>  sl <code>{_p(d.get('stop_loss'))}</code>  "
            f"tp <code>{_p(d.get('take_profit'))}</code>  rr <b>{d.get('rr')}</b>",
        ]
    else:
        lines.append(f"   trigger: {html.escape(str(c.get('entry_trigger', '—'))[:120])}")

    for r in (d.get("reasons") or [])[:4]:
        lines.append(f"   + {html.escape(str(r)[:120])}")
    for vt in (d.get("vetoes") or [])[:4]:
        lines.append(f"   − {html.escape(str(vt)[:120])}")

    if final.get("errors"):
        lines += ["", "<i>errors:</i>"] + [f"   {html.escape(str(e)[:100])}" for e in final["errors"][:3]]

    return "\n".join(lines)
