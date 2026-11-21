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
