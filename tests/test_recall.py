"""Uji memori jangka panjang: penilaian hasil, ringkasan, injeksi prompt."""
from __future__ import annotations

import pytest

from forex_agents.recall import (
    OUTCOME_NEUTRAL,
    OUTCOME_OPEN,
    OUTCOME_SL,
    OUTCOME_TP,
    build_state,
    evaluate,
    parse_rows,
    streak,
    summarize,
    tally,
)


def _row(action, entry, sl, tp, price=None, conf=70, at="2026-11-20T10:00:00"):
    return {
        "created_at": at,
        "action": action,
        "confidence": conf,
        "price": price,
        "payload": {
            "decision": {
                "action": action,
                "confidence": conf,
                "entry": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "reasons": ["alasan satu", "alasan dua", "alasan tiga"],
            }
        },
    }


@pytest.mark.parametrize(
    ("action", "entry", "sl", "tp", "now", "want"),
    [
        ("BUY", 1.1000, 1.0950, 1.1100, 1.1101, OUTCOME_TP),
        ("BUY", 1.1000, 1.0950, 1.1100, 1.0949, OUTCOME_SL),
        ("BUY", 1.1000, 1.0950, 1.1100, 1.1050, OUTCOME_OPEN),
        ("SELL", 1.1000, 1.1050, 1.0900, 1.0899, OUTCOME_TP),
        ("SELL", 1.1000, 1.1050, 1.0900, 1.1051, OUTCOME_SL),
        ("SELL", 1.1000, 1.1050, 1.0900, 1.0950, OUTCOME_OPEN),
        ("NO_TRADE", None, None, None, 1.10, OUTCOME_NEUTRAL),
        ("BUY", None, None, None, 1.10, OUTCOME_OPEN),
        ("BUY", 1.1, 1.09, 1.12, None, OUTCOME_OPEN),
    ],
)
def test_evaluate(action, entry, sl, tp, now, want):
    assert evaluate(action, entry, sl, tp, now) == want


def test_evaluate_accepts_strings():
    assert evaluate("buy", "1.1000", "1.0950", "1.1100", "1.1105") == OUTCOME_TP


def test_parse_rows_reads_payload_and_trims_reasons():
    calls = parse_rows([_row("BUY", 1.1, 1.09, 1.12, price=1.125)], price_now=1.125)
    assert len(calls) == 1
    call = calls[0]
    assert call.action == "BUY"
    assert call.outcome == OUTCOME_TP
    assert call.reasons == ("alasan satu", "alasan dua")


def test_parse_rows_handles_json_string_payload():
    rows = [
        {
            "created_at": "2026-11-20T10:00:00",
            "action": "SELL",
            "confidence": 55,
            "payload": '{"decision": {"entry": 1.1, "stop_loss": 1.11, "take_profit": 1.08}}',
        }
    ]
    calls = parse_rows(rows, price_now=1.075)
    assert calls[0].outcome == OUTCOME_TP


def test_parse_rows_handles_broken_payload():
    calls = parse_rows([{"created_at": "", "action": "BUY", "payload": "{rusak"}])
    assert calls[0].entry is None
    assert calls[0].outcome == OUTCOME_OPEN


def test_streak_counts_repeats():
    calls = parse_rows(
        [
            _row("NO_TRADE", None, None, None),
            _row("NO_TRADE", None, None, None),
            _row("BUY", 1.1, 1.09, 1.12),
        ]
    )
    assert streak(calls) == ("NO_TRADE", 2)


def test_streak_empty():
    assert streak([]) == ("", 0)


def test_tally_counts_outcomes():
    calls = parse_rows(
        [_row("BUY", 1.1, 1.09, 1.12), _row("BUY", 1.1, 1.09, 1.12)],
        price_now=1.13,
    )
    assert tally(calls)[OUTCOME_TP] == 2


def test_summarize_without_history():
    assert "belum ada riwayat" in summarize([])


def test_summarize_mentions_streak_and_outcomes():
    calls = parse_rows([_row("BUY", 1.1, 1.09, 1.12)], price_now=1.13)
    text = summarize(calls)
    assert "aksi berulang: BUY x1" in text
    assert "tp 1" in text
    assert "entry 1.10000 sl 1.09000 tp 1.12000 -> tp" in text


def test_build_state_shape():
    calls = parse_rows([_row("SELL", 1.1, 1.11, 1.08)], price_now=1.09)
    st = build_state(calls)
    assert st["count"] == 1
    assert st["last_action"] == "SELL"
    assert st["streak_action"] == "SELL"
    assert st["streak_count"] == 1
    assert st["tally"][OUTCOME_OPEN] == 1
    assert st["calls"] and st["text"]


def test_build_state_empty():
    st = build_state([])
    assert st["count"] == 0
    assert st["last_action"] == ""
    assert st["streak_count"] == 0


def test_memory_block_injected_into_prompt():
    from forex_agents.nodes import _mem

    assert _mem({"memory": {"text": ""}}) == ""
    assert _mem({}) == ""
    block = _mem({"memory": {"text": "BUY x2"}})
    assert "Riwayat keputusan sebelumnya" in block
    assert "BUY x2" in block


def test_render_shows_memory_section():
    from forex_agents.render import render

    final = {
        "symbol": "EURUSD",
        "base_tf": "m1",
        "trend_tf": "m15",
        "validation_tf": "m5",
        "critical_tf": "m1",
        "price": 1.1,
        "decision": {"action": "NO_TRADE", "confidence": 50, "reasons": [], "vetoes": []},
        "memory": {
            "count": 3,
            "tally": {"tp": 1, "sl": 1, "terbuka": 1, "netral": 0},
            "streak_action": "NO_TRADE",
            "streak_count": 3,
            "calls": ["2026-11-20 10:00 NO_TRADE conf 70 -> netral"],
        },
    }
    out = render(final)
    assert "<b>5) Ingatan</b>" in out
    assert "tp 1" in out
    assert "aksi berulang: NO_TRADE x3" in out


def test_render_hides_memory_section_when_empty():
    from forex_agents.render import render

    final = {
        "symbol": "EURUSD",
        "base_tf": "m1",
        "decision": {"action": "NO_TRADE", "confidence": 0, "reasons": [], "vetoes": []},
        "memory": {"count": 0},
    }
    assert "Ingatan" not in render(final)


@pytest.mark.asyncio
async def test_load_memory_survives_db_failure(monkeypatch):
    from forex_agents import runner

    class Boom:
        async def __aenter__(self):
            raise RuntimeError("db mati")

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(runner, "db_cm", lambda: Boom())
    mem = await runner.load_memory("EURUSD", "m1")
    assert mem["count"] == 0
    assert "memori tidak terbaca" in mem["error"]
