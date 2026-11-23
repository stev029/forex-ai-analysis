"""Jadwal analisis otomatis: jalankan cascade tiap bar tutup timeframe target.

Contoh: basis `m1` → dua TF di atasnya `m5` (validasi) dan `m15` (tren). Cron
mengikuti TF terbesar = `m15`, jadi analisis jalan tiap 15 menit tepat setelah
bar `m15` tutup.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .autotf import is_auto
from .timeframes import cascade, normalize_tf

log = logging.getLogger(__name__)

TF_MINUTES: dict[str, int] = {
    "m1": 1,
    "m5": 5,
    "m15": 15,
    "m30": 30,
    "h1": 60,
    "h4": 240,
    "d1": 1440,
    "w1": 10080,
}

MIN_INTERVAL = 1
MAX_INTERVAL = 10080
MAX_JOBS = 20
MAX_JOBS_PER_CHAT = 5
# Beri waktu provider menutup & menerbitkan bar sebelum analisis dijalankan.
GRACE_S = 5.0
ANALYSIS_TIMEOUT = 240.0


# --------------------------------------------------------------------- helpers
def auto_interval(base_tf: str) -> int:
    """Interval default (menit) = panjang TF target terbesar dari cascade."""
    if is_auto(base_tf):
        # TF dasar dipilih sendiri tiap tick, jadi pakai ritme scalping tercepat.
        return TF_MINUTES["m15"]
    base = normalize_tf(base_tf)
    return TF_MINUTES[cascade(base)["trend"]]


def targets(base_tf: str) -> tuple[str, str]:
    """Dua timeframe di atas TF dasar: (tren, validasi)."""
    if is_auto(base_tf):
        return "m30", "m15"
    base = normalize_tf(base_tf)
    m = cascade(base)
    return m["trend"], m["validation"]


def parse_interval(raw: str) -> int:
    """`15`, `15m`, `15mnt`, `2h`, `1d`, `1w` -> menit."""
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("interval kosong")

    unit = 1
    for suffix, factor in (
        ("menit", 1), ("mnt", 1), ("min", 1),
        ("m", 1), ("h", 60), ("d", 1440), ("w", 10080),
    ):
        if text.endswith(suffix) and len(text) > len(suffix):
            text, unit = text[: -len(suffix)], factor
            break

    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"interval tidak valid: {raw}") from None
    if value <= 0:
        raise ValueError(f"interval harus > 0: {raw}")

    minutes = int(round(value * unit))
    if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
        raise ValueError(
            f"interval {minutes} menit di luar batas {MIN_INTERVAL}–{MAX_INTERVAL}"
        )
    return minutes


def human_interval(minutes: int) -> str:
    if minutes % 10080 == 0 and minutes >= 10080:
        return f"{minutes // 10080} minggu"
    if minutes % 1440 == 0 and minutes >= 1440:
        return f"{minutes // 1440} hari"
    if minutes % 60 == 0 and minutes >= 60:
        return f"{minutes // 60} jam"
    return f"{minutes} menit"


def next_run_at(interval_min: int, now: float | None = None, *, grace: float = GRACE_S) -> float:
    """Waktu (epoch) bar tutup berikutnya + grace.

    Interval < 1 hari disejajarkan ke kelipatan menit epoch, jadi `m15` jatuh di
    :00 :15 :30 :45. Interval harian/mingguan disejajarkan ke tengah malam UTC.
    """
    now = time.time() if now is None else now
    if interval_min >= 1440:
        days = max(interval_min // 1440, 1)
        midnight = datetime.fromtimestamp(now, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        while midnight.timestamp() + grace <= now:
            midnight += timedelta(days=days)
        return midnight.timestamp() + grace

    step = interval_min * 60
    slot = math.floor((now - grace) / step) + 1
    return slot * step + grace


# ------------------------------------------------------------------------ job
@dataclass
class Job:
    id: str
    chat_id: int
    symbol: str
    base_tf: str
    interval_min: int
    enabled: bool = True
    last_run: float | None = None
    next_run: float = 0.0
    runs: int = 0
    failures: int = 0

    @property
    def trend_tf(self) -> str:
        return targets(self.base_tf)[0]

    def label(self) -> str:
        trend, validation = targets(self.base_tf)
        return (
            f"{self.symbol} {self.base_tf} → tiap {human_interval(self.interval_min)}"
            f" (target {validation}/{trend})"
        )


# -------------------------------------------------------------------- scheduler
Runner = Callable[..., Awaitable[Any]]
Deliver = Callable[[int, str], Awaitable[None]]
Persist = Callable[[Job, str], Awaitable[None]]


class Scheduler:
    """Antrian job in-memory dengan loop async; persistence opsional."""

    def __init__(
        self,
        deliver: Deliver,
        *,
        runner: Runner | None = None,
        persist: Persist | None = None,
        clock: Callable[[], float] = time.time,
        grace: float = GRACE_S,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._deliver = deliver
        self._runner = runner
        self._persist = persist
        self._clock = clock
        self._grace = grace
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ manage
    def add(
        self, chat_id: int, symbol: str, base_tf: str, interval: int | None = None
    ) -> Job:
        base = normalize_tf(base_tf)
        sym = (symbol or "").strip().upper()
        if not sym:
            raise ValueError("simbol kosong")
        minutes = auto_interval(base) if interval is None else int(interval)
        if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
            raise ValueError(
                f"interval {minutes} menit di luar batas {MIN_INTERVAL}–{MAX_INTERVAL}"
            )

        for job in self._jobs.values():
            if job.chat_id == chat_id and job.symbol == sym and job.base_tf == base:
                job.interval_min = minutes
                job.enabled = True
                job.next_run = next_run_at(minutes, self._clock(), grace=self._grace)
                self._wake.set()
                return job

        mine = [j for j in self._jobs.values() if j.chat_id == chat_id]
        if len(mine) >= MAX_JOBS_PER_CHAT:
            raise ValueError(f"maksimal {MAX_JOBS_PER_CHAT} jadwal per chat")
        if len(self._jobs) >= MAX_JOBS:
            raise ValueError(f"maksimal {MAX_JOBS} jadwal total")

        job = Job(
            id=uuid.uuid4().hex[:8],
            chat_id=chat_id,
            symbol=sym,
            base_tf=base,
            interval_min=minutes,
            next_run=next_run_at(minutes, self._clock(), grace=self._grace),
        )
        self._jobs[job.id] = job
        self._wake.set()
        return job

    def remove(self, selector: str, chat_id: int | None = None) -> list[Job]:
        """Hapus berdasarkan id, atau semua bila selector `all`."""
        targets_ = self._select(selector, chat_id)
        for job in targets_:
            self._jobs.pop(job.id, None)
        if targets_:
            self._wake.set()
        return targets_

    def jobs(self, chat_id: int | None = None) -> list[Job]:
        items = [j for j in self._jobs.values() if chat_id is None or j.chat_id == chat_id]
        return sorted(items, key=lambda j: (j.next_run, j.symbol))

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def _select(self, selector: str, chat_id: int | None = None) -> list[Job]:
        """Cocokkan job lewat id, `SYMBOL:TF`, atau `all`."""
        key = (selector or "").strip().lower()
        return [
            j
            for j in self.jobs(chat_id)
            if key in ("all", "semua", "*")
            or j.id == key
            or f"{j.symbol}:{j.base_tf}".lower() == key
        ]

    def pause(self, selector: str, chat_id: int | None = None) -> list[Job]:
        """Hentikan sementara tanpa menghapus; next_run tetap disimpan."""
        targets_ = [j for j in self._select(selector, chat_id) if j.enabled]
        for job in targets_:
            job.enabled = False
        if targets_:
            self._wake.set()
        return targets_

    def resume(
        self, selector: str, chat_id: int | None = None, now: float | None = None
    ) -> list[Job]:
        """Aktifkan lagi dan jadwalkan bar tutup berikutnya (bukan langsung tembak)."""
        now = self._clock() if now is None else now
        targets_ = [j for j in self._select(selector, chat_id) if not j.enabled]
        for job in targets_:
            job.enabled = True
            job.next_run = next_run_at(job.interval_min, now, grace=self._grace)
        if targets_:
            self._wake.set()
        return targets_

    def due(self, now: float | None = None) -> list[Job]:
        now = self._clock() if now is None else now
        return [j for j in self.jobs() if j.enabled and j.next_run <= now]

    def sleep_for(self, now: float | None = None, cap: float = 30.0) -> float:
        now = self._clock() if now is None else now
        pending = [j.next_run for j in self._jobs.values() if j.enabled]
        if not pending:
            return cap
        return max(0.5, min(min(pending) - now, cap))

    # -------------------------------------------------------------------- run
    async def tick(self, now: float | None = None) -> list[Job]:
        """Jalankan semua job yang jatuh tempo sekarang."""
        fired: list[Job] = []
        for job in self.due(now):
            job.next_run = next_run_at(job.interval_min, self._clock(), grace=self._grace)
            job.last_run = self._clock()
            fired.append(job)
            await self._fire(job)
        return fired

    async def _fire(self, job: Job) -> None:
        if self._runner is None:
            return
        try:
            result = await asyncio.wait_for(
                self._runner(job.symbol, job.base_tf, chat_id=job.chat_id),
                timeout=ANALYSIS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            job.failures += 1
            await self._safe_send(
                job.chat_id,
                f"jadwal {job.symbol} {job.base_tf}: analisis melebihi batas waktu "
                f"({int(ANALYSIS_TIMEOUT)} detik)",
            )
            return
        except Exception as exc:  # noqa: BLE001 - satu job gagal tidak boleh mematikan loop
            job.failures += 1
            log.exception("scheduled analysis failed: %s", job.label())
            await self._safe_send(job.chat_id, f"jadwal {job.symbol} {job.base_tf} gagal: {exc}")
            return

        job.runs += 1
        # run_analysis() mengembalikan (final, request_id); runner uji boleh dict saja.
        final = result[0] if isinstance(result, tuple) else result
        if not isinstance(final, dict):
            final = {"symbol": job.symbol, "base_tf": job.base_tf, "errors": ["hasil kosong"]}
        if self._persist is not None:
            try:
                await self._persist(job, "run")
            except Exception:  # noqa: BLE001 - persistence tidak boleh menghalangi
                log.exception("persist schedule run failed")

        from .render import render, split_message

        header = (
            f"⏱ <b>jadwal</b> {job.symbol} <code>{job.base_tf}</code> "
            f"tiap {human_interval(job.interval_min)}"
        )
        for part in split_message(header + "\n\n" + render(final)):
            await self._safe_send(job.chat_id, part)

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self._deliver(chat_id, text)
        except Exception:  # noqa: BLE001 - chat diblokir / jaringan
            log.exception("gagal mengirim hasil jadwal ke chat %s", chat_id)

    # ------------------------------------------------------------------- loop
    async def run_forever(self) -> None:
        log.info("scheduler mulai (%d job)", len(self._jobs))
        while True:
            try:
                if self.due():
                    await self.tick()
                    continue
                delay = self.sleep_for()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                log.info("scheduler berhenti")
                raise
            except Exception:  # noqa: BLE001 - loop harus tetap hidup
                log.exception("scheduler loop error")
                await asyncio.sleep(5)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="forex-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # ----------------------------------------------------------------- restore
    def restore(self, rows: list[dict]) -> int:
        """Muat job dari DB; next_run dihitung ulang supaya tidak menumpuk."""
        count = 0
        for row in rows:
            try:
                job = Job(
                    id=str(row["id"]),
                    chat_id=int(row["chat_id"] or 0),
                    symbol=str(row["symbol"]).upper(),
                    base_tf=normalize_tf(str(row["base_tf"])),
                    interval_min=int(row["interval_min"]),
                    enabled=bool(row.get("enabled", True)),
                    next_run=next_run_at(
                        int(row["interval_min"]), self._clock(), grace=self._grace
                    ),
                )
            except Exception:  # noqa: BLE001 - baris rusak dilewati
                log.warning("lewati jadwal rusak: %s", row)
                continue
            self._jobs[job.id] = job
            count += 1
        if count:
            self._wake.set()
        return count
