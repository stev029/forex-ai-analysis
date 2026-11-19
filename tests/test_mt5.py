"""MT5 provider tests — no terminal, no network. Fake mt5 module injected."""
from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd
import pytest

from forex_agents.market_mt5 import TF_CONST, MT5Provider, MT5Unavailable


class _Info:
    def __init__(self, visible=True, point=0.00001):
        self.visible = visible
        self.point = point


class _Tick:
    def __init__(self, bid, ask, last, ts):
        self.bid = bid
        self.ask = ask
        self.last = last
        self.time = ts


class FakeMT5:
    def __init__(self, bars=120):
        self.bars = bars
        self.initialized = False
        self.shutdown_called = False
        self.selected: list[str] = []

    def initialize(self, **kwargs):
        self.init_kwargs = kwargs
        self.initialized = True
        return True

    def shutdown(self):
        self.shutdown_called = True

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, name):
        return _Info() if name.startswith("EURUSD") else None

    def symbol_select(self, name, enable):
        self.selected.append(name)
        return True

    def copy_rates_from_pos(self, name, tf, start, count):
        base = 1.1000
        arr = np.zeros(count, dtype=[("time", "<i8"), ("open", "<f8"), ("high", "<f8"),
                                     ("low", "<f8"), ("close", "<f8"),
                                     ("tick_volume", "<i8"), ("spread", "<i4"),
                                     ("real_volume", "<i8")])
        ts0 = 1_700_000_000
        for i in range(count):
            px = base + i * 0.0001
            arr[i] = (ts0 + i * 60, px, px + 0.0002, px - 0.0002, px, 10, 1, 0)
        return arr

    def symbol_info_tick(self, name):
        return _Tick(bid=1.10000, ask=1.10002, last=1.10001, ts=1_700_000_060)


def _install(monkeypatch, fake):
    mod = types.ModuleType("MetaTrader5")
    for attr in ("initialize", "shutdown", "last_error", "symbol_info",
                 "symbol_select", "copy_rates_from_pos", "symbol_info_tick"):
        setattr(mod, attr, getattr(fake, attr))
    monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
    return fake


def test_timeframe_constants_match_mt5():
    assert TF_CONST["m1"] == 1
    assert TF_CONST["m5"] == 5
    assert TF_CONST["m15"] == 15
    assert TF_CONST["m30"] == 30
    assert TF_CONST["h1"] == 16385
    assert TF_CONST["h4"] == 16388
    assert TF_CONST["d1"] == 16408
    assert TF_CONST["w1"] == 32769


def test_fetch_returns_ohlcv_frame(monkeypatch):
    fake = _install(monkeypatch, FakeMT5(bars=120))
    p = MT5Provider()
    df = p.fetch("EURUSD", "m5", bars=100)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 100
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing
    assert fake.initialized


def test_fetch_is_cached_only_when_ttl_set(monkeypatch):
    fake = _install(monkeypatch, FakeMT5(bars=80))
    p = MT5Provider(cache_ttl=0.0)
    p.fetch("EURUSD", "m1", bars=50)
    first = len(fake.selected)
    p.fetch("EURUSD", "m1", bars=50)
    assert len(fake.selected) == first  # symbol_select tidak dipanggil ulang

    p2 = MT5Provider(cache_ttl=60.0)
    p2.fetch("EURUSD", "m1", bars=50)
    a = p2.fetch("EURUSD", "m1", bars=50)
    b = p2.fetch("EURUSD", "m1", bars=50)
    pd.testing.assert_frame_equal(a, b)


def test_unknown_symbol_raises(monkeypatch):
    _install(monkeypatch, FakeMT5())
    p = MT5Provider()
    with pytest.raises(MT5Unavailable):
        p.fetch("GBPJPY", "m1", bars=10)


def test_quote_uses_mid_and_reports_spread(monkeypatch):
    _install(monkeypatch, FakeMT5())
    p = MT5Provider()
    q = p.quote("EURUSD", ttl=0.0)
    assert q["price"] == pytest.approx(1.10001)
    assert q["bid"] == pytest.approx(1.10000)
    assert q["ask"] == pytest.approx(1.10002)
    assert q["spread_pips"] == pytest.approx(0.00002)
    assert q["lag_s"] is not None and q["lag_s"] >= 0


def test_symbol_suffix_applied(monkeypatch):
    fake = _install(monkeypatch, FakeMT5())
    p = MT5Provider(suffix="m")
    assert p.mt5_symbol("EURUSD") == "EURUSDM"
    p2 = MT5Provider(suffix=".m")
    assert p2.mt5_symbol("EURUSD") == "EURUSD.M"
    assert fake.initialized is False  # belum connect


def test_close_shuts_down(monkeypatch):
    fake = _install(monkeypatch, FakeMT5())
    p = MT5Provider()
    p.connect()
    p.close()
    assert fake.shutdown_called
