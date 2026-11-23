"""Telegram rendering (Bahasa Indonesia). Plain text + HTML-escaped, no markdown traps.

Tidak ada pemotongan teks: semua rationale/pattern/why/pemicu/reasons/vetoes
ditampilkan utuh. Pesan panjang dipecah per baris oleh `split_message()`.
"""
from __future__ import annotations

import html
from typing import Any

from .market_hours import STALE_AFTER_S

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

SIDE_ID = {"buy": "beli", "sell": "jual", "none": "belum ada"}

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


def _ago(lag: int) -> str:
    if lag < 90:
        return f"{lag} detik lalu"
    if lag < 5400:
        return f"{lag // 60} menit lalu"
    if lag < 172800:
        return f"{lag // 3600} jam lalu"
    return f"{lag // 86400} hari lalu"


def _stamp_line(final: dict) -> str:
    """Baris waktu data harga + umur data + status bursa.

    Umur data besar saat pasar tutup itu normal, jadi jangan dilaporkan sebagai
    harga basi. Peringatan hanya muncul kalau bursa buka tapi bar tertinggal.
    """
    stamp = final.get("price_time") or ""
    lag = final.get("price_lag_s")
    market = final.get("market") or {}
    is_open = bool(market.get("is_open"))

    if not stamp:
        return "data harga: <i>tidak tersedia</i>"
    short = stamp.replace("+00:00", "Z").replace("T", " ")[:19]
    parts = [f"data harga: <code>{_esc(short)}</code>"]
    if lag is not None:
        parts.append(f"({_ago(int(lag))})")
    if market:
        parts.append("pasar buka" if is_open else "pasar tutup (weekend)")
    line = " ".join(parts)
    if lag is not None and is_open and int(lag) > STALE_AFTER_S:
        line += f"\n⚠️ harga tertinggal {_ago(int(lag))} padahal pasar buka"
    return line


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


def _scalp_lines(scalp: dict) -> list[str]:
    """Seksi 5) Scalping: area potensial, gerbang area ideal, rencana entry."""
    area = scalp.get("area") or {}
    ideal = scalp.get("ideal") or {}
    plan_ = scalp.get("plan") or {}
    checks = ideal.get("checks") or {}
    out = ["", "<b>5) Scalping</b> (area m15/m30 → entry m1/m5)"]
    sup = (area.get("support") or {}).get("high")
    res = (area.get("resistance") or {}).get("low")
    out.append(
        f"   area potensial: support {_p(sup)} | resistensi {_p(res)}"
        f" | di dalam area: {'ya' if area.get('inside') else 'tidak'}"
    )
    out.append(
        f"   sisi: {_t(SIDE_ID, area.get('side'))} | sentuhan {area.get('touches', 0)}"
    )
    out.append(
        "   gerbang area ideal: " + ("<b>terbentuk</b>" if ideal.get("ok") else "belum")
    )
    out.append(
        f"   {'✓' if checks.get('harga_di_area') else '✗'} harga di area"
        f" | {'✓' if checks.get('area_teruji') else '✗'} area teruji"
        f" | {'✓' if checks.get('wick_penolakan') else '✗'} wick penolakan"
        f" | {'✓' if checks.get('wick_searah_area') else '✗'} wick searah"
        f" | {'✓' if checks.get('kelelahan') else '✗'} kelelahan"
    )
    if plan_.get("entry"):
        out.append(
            f"   rencana {_t(SIDE_ID, plan_.get('side'))}: "
            f"entry <code>{_p(plan_.get('entry'))}</code>  "
            f"SL <code>{_p(plan_.get('stop_loss'))}</code>  "
            f"TP <code>{_p(plan_.get('take_profit'))}</code>  RR <b>{plan_.get('rr')}</b>"
        )
    else:
        out.append("   rencana: belum ada sisi yang sah")
    return out


def render(final: dict) -> str:
    d = final.get("decision") or {}
    t = final.get("trend") or {}
    v = final.get("validation") or {}
    c = final.get("critical") or {}
    action = d.get("action", "NO_TRADE")

    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    mode_tag = " · scalp" if final.get("mode") == "scalp" else ""
    lines: list[str] = [
        f"{icon} <b>{ACTION_ID.get(action, action)}</b> — {_esc(final.get('symbol'))} "
        f"<code>{_esc(final.get('base_tf'))}</code>{mode_tag}",
        f"keyakinan: <b>{d.get('confidence', 0)}</b>/100   harga: <code>{_p(final.get('price'))}</code>",
        _stamp_line(final),
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

    scalp = final.get("scalp") or {}
    scalp_shown = bool(final.get("mode") == "scalp" and scalp)
    if scalp_shown:
        lines += _scalp_lines(scalp)

    mem = final.get("memory") or {}
    if mem.get("count"):
        tally_ = mem.get("tally") or {}
        lines += [
            "",
            f"<b>{6 if scalp_shown else 5}) Ingatan</b> ({mem['count']} analisis terakhir)",
            f"   hasil: tp {tally_.get('tp', 0)} | sl {tally_.get('sl', 0)} | "
            f"terbuka {tally_.get('terbuka', 0)} | netral {tally_.get('netral', 0)}",
            f"   aksi berulang: {_esc(mem.get('streak_action'))} x{mem.get('streak_count')}",
        ]
        lines += [f"   • {_esc(c)}" for c in (mem.get("calls") or [])[:3]]

    if final.get("errors"):
        lines += ["", "<i>kesalahan:</i>"] + [f"   {_esc(e)}" for e in final["errors"]]

    return "\n".join(lines)
