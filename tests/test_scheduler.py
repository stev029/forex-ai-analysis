"""Scheduler: interval otomatis dari TF, parsing interval custom, eksekusi."""
from __future__ import annotations

import asyncio
import time

import pytest

from forex_agents.scheduler import (
    MAX_JOBS_PER_CHAT,
    Scheduler,
    auto_interval,
    human_interval,
    next_run_at,
    parse_interval,
    targets,
)


# --------------------------------------------------------------- TF -> interval
@pytest.mark.parametrize(
    ("base", "interval", "trend", "validation"),
    [
        ("m1", 15, "m15", "m5"),
        ("m5", 30, "m30", "m15"),
        ("m15", 60, "h1", "m30"),
        ("m30", 240, "h4", "h1"),
        ("h1", 1440, "d1", "h4"),
        ("h4", 10080, "w1", "d1"),
    ],
)
def test_auto_interval_follows_largest_target_tf(base, interval, trend, validation):
    assert auto_interval(base) == interval
    assert targets(base) == (trend, validation)


def test_auto_interval_accepts_alias():
    assert auto_interval("1m") == 15
    assert auto_interval("15m") == 60


def test_auto_interval_rejects_unknown_tf():
    with pytest.raises(ValueError):
        auto_interval("m7")


# ------------------------------------------------------------- parse interval
@pytest.mark.parametrize(
    ("raw", "minutes"),
    [
        ("15", 15),
        ("15m", 15),
        ("15mnt", 15),
        ("15 menit", 15),
        ("15min", 15),
        ("2h", 120),
        ("1d", 1440),
        ("1w", 10080),
        ("30M", 30),
        ("0.5h", 30),
    ],
)
def test_parse_interval(raw, minutes):
    assert parse_interval(raw) == minutes


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "0m", "999999", "m"])
def test_parse_interval_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_interval(raw)


def test_human_interval():
    assert human_interval(15) == "15 menit"
    assert human_interval(120) == "2 jam"
    assert human_interval(1440) == "1 hari"
    assert human_interval(10080) == "1 minggu"


# --------------------------------------------------------------- next_run_at
def test_next_run_aligns_to_interval_grid():
    # 2026-11-20 00:07:00 UTC -> bar m15 berikutnya :15:00 + grace 5s
    now = 1795133220.0  # 00:07:00
    nxt = next_run_at(15, now=now, grace=5.0)
    assert nxt == 1795133705.0  # 00:15:05


def test_next_run_is_strictly_future_even_on_boundary():
    now = 1795133700.0  # tepat 00:15:00
    assert next_run_at(15, now=now, grace=5.0) > now


def test_next_run_daily_uses_utc_midnight():
    now = 1795133700.0  # 2026-11-20 00:15:00 UTC
    nxt = next_run_at(1440, now=now, grace=5.0)
    assert nxt == 1795219205.0  # 2026-11-21 00:00:05 UTC


# -------------------------------------------------------------------- Scheduler
def _sched(**kw):
    sent: list[tuple[int, str]] = []

    async def deliver(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    async def runner(symbol, tf, *, chat_id=None):
        return {
            "symbol": symbol,
            "base_tf": tf,
            "decision": {"action": "NO_TRADE", "confidence": 10},
            "errors": [],
        }

    s = Scheduler(deliver, runner=runner, **kw)
    return s, sent


def test_add_uses_auto_interval_and_is_idempotent():
    s, _ = _sched()
    job = s.add(1, "xauusd", "m1")
    assert job.interval_min == 15
    assert job.symbol == "XAUUSD"
    again = s.add(1, "XAUUSD", "1m", 30)
    assert again.id == job.id
    assert again.interval_min == 30
    assert len(s.jobs()) == 1


def test_add_rejects_bad_interval_and_limits():
    s, _ = _sched()
    with pytest.raises(ValueError):
        s.add(1, "EURUSD", "m1", 0)
    for tf in ("m1", "m5", "m15", "m30", "h1"):
        s.add(1, "EURUSD", tf)
    with pytest.raises(ValueError, match="maksimal"):
        s.add(1, "EURUSD", "h4")
    # chat lain tetap boleh
    assert s.add(2, "EURUSD", "h4").chat_id == 2


def test_add_without_db_still_works():
    s, _ = _sched()
    assert s.add(9, "EURUSD", "m1", 15).id


def test_remove_by_id_all_and_chat_scope():
    s, _ = _sched()
    a = s.add(1, "EURUSD", "m1")
    s.add(1, "GBPUSD", "m1")
    assert s.remove(a.id, chat_id=1) == [a]
    assert s.remove("all", chat_id=2) == []
    assert len(s.remove("all", chat_id=1)) == 1
    assert s.jobs() == []


def test_remove_by_symbol_pair():
    s, _ = _sched()
    job = s.add(1, "EURUSD", "m5")
    assert s.remove("eurusd:m5", chat_id=1) == [job]


def test_jobs_sorted_by_next_run():
    s, _ = _sched()
    s.add(1, "EURUSD", "m1", 15)
    s.add(1, "GBPUSD", "h4", 1440)
    assert s.jobs(1)[0].interval_min == 15


@pytest.mark.asyncio
async def test_tick_fires_due_job_and_reschedules():
    s, sent = _sched()
    job = s.add(1, "EURUSD", "m1", 15)
    job.next_run = time.time() - 1
    fired = await s.tick()
    assert fired == [job]
    assert job.runs == 1
    assert job.next_run > time.time()
    assert sent and sent[0][0] == 1
    assert "jadwal" in sent[0][1].lower()


@pytest.mark.asyncio
async def test_tick_skips_future_jobs():
    s, sent = _sched()
    job = s.add(1, "EURUSD", "m1", 15)
    job.next_run = time.time() + 600
    assert await s.tick() == []
    assert sent == []


@pytest.mark.asyncio
async def test_runner_failure_does_not_stop_scheduler():
    sent: list[tuple[int, str]] = []

    async def deliver(chat_id, text):
        sent.append((chat_id, text))

    async def runner(*_a, **_k):
        raise RuntimeError("provider mati")

    s = Scheduler(deliver, runner=runner)
    job = s.add(1, "EURUSD", "m1", 15)
    job.next_run = time.time() - 1
    await s.tick()
    assert job.failures == 1
    assert "gagal" in sent[0][1]
    assert job.next_run > time.time()


@pytest.mark.asyncio
async def test_deliver_failure_is_swallowed():
    async def deliver(*_a):
        raise RuntimeError("chat diblokir")

    async def runner(symbol, tf, *, chat_id=None):
        return {"symbol": symbol, "base_tf": tf, "decision": {"action": "NO_TRADE"}}

    s = Scheduler(deliver, runner=runner)
    job = s.add(1, "EURUSD", "m1", 15)
    job.next_run = time.time() - 1
    await s.tick()
    assert job.runs == 1


@pytest.mark.asyncio
async def test_run_forever_stops_cleanly():
    s, _ = _sched()
    s.add(1, "EURUSD", "m1", 15)
    s.start()
    await asyncio.sleep(0.05)
    await s.stop()
    assert s._task is None


def test_restore_recomputes_next_run():
    s, _ = _sched()
    rows = [
        {"id": "abc123", "chat_id": 5, "symbol": "XAUUSD", "base_tf": "m1",
         "interval_min": 15, "enabled": True},
        {"id": "bad", "chat_id": 5, "symbol": "X", "base_tf": "zzz",
         "interval_min": 15, "enabled": True},
    ]
    assert s.restore(rows) == 1
    job = s.get("abc123")
    assert job is not None and job.next_run > time.time()
    assert s.get("bad") is None


def test_sleep_for_capped():
    s, _ = _sched()
    assert s.sleep_for() == 30.0
    s.add(1, "EURUSD", "m1", 15)
    assert 0.5 <= s.sleep_for() <= 30.0


def test_per_chat_limit_constant():
    assert MAX_JOBS_PER_CHAT >= 1


@pytest.mark.asyncio
async def test_runner_receives_chat_id_as_keyword_and_unpacks_tuple():
    """run_analysis(symbol, base_tf, *, chat_id=...) -> (final, rid)."""
    sent: list[tuple[int, str]] = []
    seen: dict = {}

    async def deliver(chat_id, text):
        sent.append((chat_id, text))

    async def runner(symbol, base_tf, *, chat_id=None):
        seen["args"] = (symbol, base_tf, chat_id)
        return ({"symbol": symbol, "base_tf": base_tf,
                 "decision": {"action": "NO_TRADE", "confidence": 5}}, "rid123")

    s = Scheduler(deliver, runner=runner)
    job = s.add(7, "XAUUSD", "m1", 15)
    job.next_run = time.time() - 1
    await s.tick()
    assert seen["args"] == ("XAUUSD", "m1", 7)
    assert job.runs == 1
    assert sent and "TIDAK ENTRY" in sent[0][1]


@pytest.mark.asyncio
async def test_runner_non_dict_result_is_replaced():
    async def deliver(*_a):
        raise AssertionError("tidak boleh mengirim")

    async def runner(*_a, **_k):
        return None

    s = Scheduler(deliver, runner=runner)
    job = s.add(1, "EURUSD", "m1", 15)
    job.next_run = time.time() - 1
    await s.tick()
    assert job.runs == 1
