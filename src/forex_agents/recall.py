"""Memori jangka panjang: riwayat keputusan per simbol + penilaian hasil.

Checkpointer LangGraph hanya menyimpan satu thread (satu request), jadi tidak
punya ingatan antar-analisis. Modul ini yang memberi agen ingatan: ambil N
keputusan terakhir untuk simbol+TF yang sama dari Postgres, nilai apakah
rencana lama kena TP/SL, lalu ringkas jadi teks yang disuntik ke prompt.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

HISTORY_LIMIT = 5
OUTCOME_TP = "tp"
OUTCOME_SL = "sl"
OUTCOME_OPEN = "terbuka"
OUTCOME_NEUTRAL = "netral"


@dataclass(frozen=True)
class PriorCall:
    """Satu keputusan lama yang sudah dinilai terhadap harga sekarang."""

    at: str
    action: str
    confidence: float
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    price: float | None
    outcome: str = OUTCOME_NEUTRAL
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def line(self) -> str:
        plan = ""
        if self.entry and self.stop_loss and self.take_profit:
            plan = (
                f" entry {self.entry:.5f} sl {self.stop_loss:.5f}"
                f" tp {self.take_profit:.5f}"
            )
        return (
            f"{self.at[:16]} {self.action} conf {self.confidence:g}"
            f"{plan} -> {self.outcome}"
        )


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def evaluate(
    action: str | None,
    entry: Any,
    stop_loss: Any,
    take_profit: Any,
    price_now: Any,
) -> str:
    """Nilai rencana lama terhadap harga terbaru (bukan backtest, hanya cek level)."""
    act = (action or "").upper()
    if act not in ("BUY", "SELL"):
        return OUTCOME_NEUTRAL
    now = _num(price_now)
    sl = _num(stop_loss)
    tp = _num(take_profit)
    if now is None or sl is None or tp is None:
        return OUTCOME_OPEN
    if act == "BUY":
        if now >= tp:
            return OUTCOME_TP
        if now <= sl:
            return OUTCOME_SL
        return OUTCOME_OPEN
    if now <= tp:
        return OUTCOME_TP
    if now >= sl:
        return OUTCOME_SL
    return OUTCOME_OPEN


def parse_rows(rows: Iterable[dict], price_now: Any = None) -> list[PriorCall]:
    """Ubah baris `analyses` jadi PriorCall, urut terbaru dulu."""
    calls: list[PriorCall] = []
    for row in rows:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        decision = payload.get("decision") or {}
        action = row.get("action") or decision.get("action") or "?"
        calls.append(
            PriorCall(
                at=str(row.get("created_at") or row.get("at") or ""),
                action=str(action).upper(),
                confidence=_num(row.get("confidence") or decision.get("confidence")) or 0.0,
                entry=_num(decision.get("entry")),
                stop_loss=_num(decision.get("stop_loss")),
                take_profit=_num(decision.get("take_profit")),
                price=_num(row.get("price") or payload.get("price")),
                outcome=evaluate(
                    action,
                    decision.get("entry"),
                    decision.get("stop_loss"),
                    decision.get("take_profit"),
                    price_now,
                ),
                reasons=tuple(str(r) for r in (decision.get("reasons") or [])[:2]),
            )
        )
    return calls


def streak(calls: list[PriorCall]) -> tuple[str, int]:
    """Berapa kali berturut-turut aksi terakhir diulang (terbaru dulu)."""
    if not calls:
        return "", 0
    head = calls[0].action
    count = 0
    for call in calls:
        if call.action != head:
            break
        count += 1
    return head, count


def tally(calls: list[PriorCall]) -> dict[str, int]:
    out = {OUTCOME_TP: 0, OUTCOME_SL: 0, OUTCOME_OPEN: 0, OUTCOME_NEUTRAL: 0}
    for call in calls:
        out[call.outcome] = out.get(call.outcome, 0) + 1
    return out


def summarize(calls: list[PriorCall], limit: int = HISTORY_LIMIT) -> str:
    """Teks ringkas Bahasa Indonesia untuk disuntik ke prompt agen."""
    if not calls:
        return "belum ada riwayat untuk simbol/timeframe ini."
    head, count = streak(calls)
    stats = tally(calls)
    lines = [
        f"{len(calls)} analisis terakhir, aksi berulang: {head} x{count}",
        f"hasil: tp {stats[OUTCOME_TP]}, sl {stats[OUTCOME_SL]}, "
        f"terbuka {stats[OUTCOME_OPEN]}, netral {stats[OUTCOME_NEUTRAL]}",
    ]
    lines.extend(call.line() for call in calls[:limit])
    return "\n".join(lines)


def build_state(calls: list[PriorCall], limit: int = HISTORY_LIMIT) -> dict[str, Any]:
    head, count = streak(calls)
    return {
        "count": len(calls),
        "text": summarize(calls, limit=limit),
        "last_action": calls[0].action if calls else "",
        "last_confidence": calls[0].confidence if calls else 0.0,
        "streak_action": head,
        "streak_count": count,
        "tally": tally(calls),
        "calls": [call.line() for call in calls[:limit]],
    }
