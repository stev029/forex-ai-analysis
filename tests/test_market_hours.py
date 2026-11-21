"""Jam bursa FX + deteksi harga basi."""
from __future__ import annotations

from datetime import datetime, timezone

from forex_agents.market_hours import (
    STALE_AFTER_S,
    is_stale,
    market_state,
)


def _dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_saturday_is_closed():
    st = market_state(_dt(2026, 11, 21, 12))
    assert st.is_open is False
    assert st.reason == "weekend"
    assert st.next_open == _dt(2026, 11, 22, 22)


def test_sunday_before_open_is_closed():
    st = market_state(_dt(2026, 11, 22, 21, 59))
    assert st.is_open is False
    assert st.next_open == _dt(2026, 11, 22, 22)


def test_sunday_after_open_is_open():
    assert market_state(_dt(2026, 11, 22, 22)).is_open is True


def test_friday_after_close_is_closed():
    st = market_state(_dt(2026, 11, 20, 22, 30))
    assert st.is_open is False
    assert st.last_close == _dt(2026, 11, 20, 22)
    assert st.next_open == _dt(2026, 11, 22, 22)


def test_midweek_is_open():
    st = market_state(_dt(2026, 11, 18, 9))
    assert st.is_open is True
    assert st.as_dict()["next_open"] is None


def test_is_stale_ignores_closed_market():
    # Sabtu: lag 19 jam tetap bukan "basi"
    assert is_stale(67705, _dt(2026, 11, 21, 16)) is False


def test_is_stale_flags_open_market_gap():
    assert is_stale(STALE_AFTER_S + 1, _dt(2026, 11, 18, 9)) is True
    assert is_stale(STALE_AFTER_S - 1, _dt(2026, 11, 18, 9)) is False


def test_is_stale_none_lag_is_not_stale():
    assert is_stale(None, _dt(2026, 11, 18, 9)) is False
