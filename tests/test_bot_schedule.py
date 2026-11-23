"""Uji handler /schedule pause|resume tanpa jaringan Telegram."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from forex_agents import bot as botmod
from forex_agents.scheduler import Scheduler


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text, **_kw):
        self.replies.append(text)


def _ctx(*args: str):
    return SimpleNamespace(args=list(args))


def _update(chat_id: int = 1):
    msg = FakeMessage()
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=chat_id),
    )


@pytest.fixture()
def sched(monkeypatch):
    sent: list[tuple[int, str]] = []

    async def deliver(chat_id, text):
        sent.append((chat_id, text))

    async def runner(symbol, base_tf, *, chat_id=None):
        return {"symbol": symbol, "base_tf": base_tf, "decision": {"action": "NO_TRADE"}}

    async def _noop(*_a, **_k):
        return None

    async def _allow(_update):
        return True

    s = Scheduler(deliver, runner=runner)
    monkeypatch.setattr(botmod, "SCHED", s)
    monkeypatch.setattr(botmod, "_guard", _allow)
    monkeypatch.setattr(botmod, "_persist_save", _noop)
    monkeypatch.setattr(botmod, "_persist_delete", _noop)
    return s


@pytest.mark.asyncio
async def test_schedule_pause_all(sched, monkeypatch):
    job = sched.add(1, "XAUUSD", "m1", 15)
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("pause"))
    assert job.enabled is False
    assert "dijeda" in upd.effective_message.replies[-1]
    assert job.id in upd.effective_message.replies[-1]


@pytest.mark.asyncio
async def test_schedule_pause_by_id(sched):
    job = sched.add(1, "XAUUSD", "m1", 15)
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("pause", job.id))
    assert job.enabled is False


@pytest.mark.asyncio
async def test_schedule_pause_unknown_id(sched):
    sched.add(1, "XAUUSD", "m1", 15)
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("pause", "tidakada"))
    assert "tidak ada jadwal aktif" in upd.effective_message.replies[-1]


@pytest.mark.asyncio
async def test_schedule_resume_all(sched):
    job = sched.add(1, "XAUUSD", "m1", 15)
    sched.pause(job.id, chat_id=1)
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("resume"))
    assert job.enabled is True
    assert "dilanjutkan" in upd.effective_message.replies[-1]


@pytest.mark.asyncio
async def test_schedule_resume_when_nothing_paused(sched):
    sched.add(1, "XAUUSD", "m1", 15)
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("resume"))
    assert "tidak ada jadwal terjeda" in upd.effective_message.replies[-1]


@pytest.mark.asyncio
async def test_schedule_list_marks_paused(sched):
    job = sched.add(1, "XAUUSD", "m1", 15)
    sched.pause(job.id, chat_id=1)
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("list"))
    assert "terjeda" in upd.effective_message.replies[-1]


@pytest.mark.asyncio
async def test_schedule_help_mentions_pause_and_resume(sched):
    upd = _update(1)
    await botmod.cmd_schedule(upd, _ctx("bantuan-tidak-dikenal"))
    text = upd.effective_message.replies[-1]
    assert "pause" in text and "resume" in text


@pytest.mark.asyncio
async def test_memory_command_shows_history(sched, monkeypatch):
    mem = {
        "count": 2,
        "text": "2 analisis terakhir, aksi berulang: NO_TRADE x2\nhasil: tp 0",
        "streak_action": "NO_TRADE",
        "streak_count": 2,
        "calls": ["2026-11-20 10:00 NO_TRADE conf 70 -> netral"],
    }

    async def fake_load(symbol, base, limit=10):
        return mem

    monkeypatch.setattr(botmod, "load_memory", fake_load)
    upd = _update(1)
    await botmod.cmd_memory(upd, _ctx("xauusd", "m1"))
    text = upd.effective_message.replies[-1]
    assert "Ingatan XAUUSD m1" in text
    assert "NO_TRADE x2" in text


@pytest.mark.asyncio
async def test_memory_command_empty(sched, monkeypatch):
    async def fake_load(symbol, base, limit=10):
        return {"count": 0, "text": ""}

    monkeypatch.setattr(botmod, "load_memory", fake_load)
    upd = _update(1)
    await botmod.cmd_memory(upd, _ctx("eurusd", "m1"))
    assert "belum ada ingatan" in upd.effective_message.replies[-1]


@pytest.mark.asyncio
async def test_memory_command_reports_db_error(sched, monkeypatch):
    async def fake_load(symbol, base, limit=10):
        return {"count": 0, "text": "", "error": "memori tidak terbaca: db mati"}

    monkeypatch.setattr(botmod, "load_memory", fake_load)
    upd = _update(1)
    await botmod.cmd_memory(upd, _ctx())
    assert "memori tidak terbaca" in upd.effective_message.replies[-1]


# ------------------------------------------------------------- /mode handler
@pytest.mark.asyncio
async def test_mode_shows_default_when_unset(monkeypatch):
    async def _allow(_update):
        return True

    async def _none(_chat_id):
        return None

    monkeypatch.setattr(botmod, "_guard", _allow)
    monkeypatch.setattr(botmod, "_chat_mode", _none)
    upd = _update()
    await botmod.cmd_mode(upd, _ctx())
    text = upd.effective_message.replies[0]
    assert "mode aktif" in text
    assert "cascade" in text


@pytest.mark.asyncio
async def test_mode_switch_persists(monkeypatch):
    saved: list[tuple[int, str]] = []

    async def _allow(_update):
        return True

    async def _set(conn, chat_id, mode):
        saved.append((chat_id, mode))

    class _CM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(botmod, "_guard", _allow)
    monkeypatch.setattr(botmod, "db_cm", lambda: _CM())
    monkeypatch.setattr(botmod, "set_chat_mode", _set)
    upd = _update(7)
    await botmod.cmd_mode(upd, _ctx("scalping"))
    assert saved == [(7, "scalp")]
    assert "mode diganti ke <b>scalp</b>" in upd.effective_message.replies[0]


@pytest.mark.asyncio
async def test_mode_rejects_unknown(monkeypatch):
    async def _allow(_update):
        return True

    monkeypatch.setattr(botmod, "_guard", _allow)
    upd = _update()
    await botmod.cmd_mode(upd, _ctx("swing"))
    assert "mode tidak dikenal" in upd.effective_message.replies[0]
