"""Jam pasar FX: kapan data harga boleh dianggap basi.

Data broker/TradingView berhenti di bar terakhir sebelum tutup, jadi umur data
yang besar di luar jam bursa adalah normal, bukan bug provider. Modul ini
memisahkan dua hal itu supaya render bisa membedakan "harga basi" dari
"pasar tutup".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# FX spot: buka Minggu 22:00 UTC, tutup Jumat 22:00 UTC.
OPEN_WEEKDAY = 6  # Minggu (Monday=0)
OPEN_HOUR_UTC = 22
CLOSE_WEEKDAY = 4  # Jumat
CLOSE_HOUR_UTC = 22

# Umur data dianggap basi kalau pasar sedang buka dan bar lebih tua dari ini.
STALE_AFTER_S = 180


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _at(day: datetime, weekday: int, hour: int) -> datetime:
    """Hari (tanggal) `day` pada weekday tertentu di minggu yang sama, jam UTC."""
    base = day.replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


@dataclass(frozen=True)
class MarketState:
    is_open: bool
    reason: str
    next_open: datetime | None
    last_close: datetime | None

    def as_dict(self) -> dict:
        return {
            "is_open": self.is_open,
            "reason": self.reason,
            "next_open": self.next_open.isoformat() if self.next_open else None,
            "last_close": self.last_close.isoformat() if self.last_close else None,
        }


def market_state(now: datetime | None = None) -> MarketState:
    """Status pasar FX pada `now` (default sekarang, UTC)."""
    now = (now or _utcnow()).astimezone(timezone.utc)
    wd, hour = now.weekday(), now.hour

    # Sabtu penuh + Minggu sebelum jam buka.
    if wd == 5:
        return MarketState(False, "weekend", _at(now, OPEN_WEEKDAY, OPEN_HOUR_UTC), None)
    if wd == OPEN_WEEKDAY and hour < OPEN_HOUR_UTC:
        return MarketState(False, "weekend", _at(now, OPEN_WEEKDAY, OPEN_HOUR_UTC), None)
    # Jumat setelah tutup.
    if wd == CLOSE_WEEKDAY and hour >= CLOSE_HOUR_UTC:
        nxt = _at(now, OPEN_WEEKDAY, OPEN_HOUR_UTC)
        return MarketState(False, "weekend", nxt, _at(now, CLOSE_WEEKDAY, CLOSE_HOUR_UTC))
    return MarketState(True, "bursa buka", None, None)


def is_stale(lag_s: int | None, now: datetime | None = None) -> bool:
    """True hanya kalau pasar buka tapi data lebih tua dari STALE_AFTER_S."""
    if lag_s is None:
        return False
    if not market_state(now).is_open:
        return False
    return lag_s > STALE_AFTER_S
