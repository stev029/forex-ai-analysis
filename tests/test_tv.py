"""TradingView provider tests — no websocket, no network. Fake client injected."""
from __future__ import annotations

import asyncio
import sys
import types

import pandas as pd
import pytest

from forex_agents.market import pip_size
from forex_agents.market_tv import (
    TV_TF,
    TVUnavailable,
    TradingViewProvider,
    _periods_to_frame,
    to_tv_symbol,
)


def test_timeframe_map_covers_every_ladder_step():
    assert set(TV_TF) == {"m1", "m5", "m15", "m30", "h1", "h4", "d1", "w1"}
    assert TV_TF["m1"] == "1"
    assert TV_TF["h4"] == "240"
    assert TV_TF["d1"] == "1D"
    assert TV_TF["w1"] == "1W"


def test_to_tv_symbol_uses_exchange_prefix():
    assert to_tv_symbol("EURUSD") == "FX:EURUSD"
    assert to_tv_symbol("eurusd", "OANDA") == "OANDA:EURUSD"
    assert to_tv_symbol("FX_IDC:USDMXN") == "FX_IDC:USDMXN"


def test_to_tv_symbol_maps_non_fx_instruments():
    assert to_tv_symbol("XAUUSD") == "TVC:GOLD"
    assert to_tv_symbol("BTCUSD") == "BITSTAMP:BTCUSD"
    assert to_tv_symbol("NAS100") == "TVC:NDX"
    assert to_tv_symbol("DXY") == "TVC:DXY"


def test_pip_size_understands_tv_tickers():
    assert pip_size("TVC:GOLD") == 0.1
    assert pip_size("TVC:NDX") == 1.0
    assert pip_size("FX:EURUSD") == 0.0001
    assert pip_size("FX:USDJPY") == 0.01


def test_periods_to_frame_sorted_deduped_utc():
    periods = {
        120: {"time": 120, "open": 1.2, "max": 1.3, "min": 1.1, "close": 1.25, "volume": 5},
        60: {"time": 60, "open": 1.1, "max": 1.2, "min": 1.0, "close": 1.15, "volume": 3},
    }
    df = _periods_to_frame(periods)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None
    assert df.index.is_monotonic_increasing
    assert df["close"].tolist() == [1.15, 1.25]


class FakeChart:
    def __init__(self, client):
        self.client = client
        self.periods = {}
        self._update = None
        self._error = None
        self.subscribed = None

    def on_update(self, cb):
        self._update = cb

    def on_symbol_loaded(self, cb):
        pass

    def on_error(self, cb):
        self._error = cb

    def subscribe(self, symbol, timeframe, count):
        self.subscribed = (symbol, timeframe, count)
        rows = [
            {"time": 60 * i, "open": 1.1, "max": 1.2, "min": 1.0, "close": 1.15, "volume": 1}
            for i in range(1, 4)
        ]
        self._update({"periods": rows})


class FakeClient:
    def __init__(self, debug=False, server="data"):
        self.debug = debug
        self.server = server
        self.logged_cbs = []
        self.sent = []
        self.disconnected = False
        self.chart = None

    def on_logged(self, cb):
        self.logged_cbs.append(cb)

    def on_error(self, cb):
        pass

    def on_event(self, cb):
        pass

    def _send_queue(self):
        self.sent.append("flush")

    async def connect(self):
        for cb in self.logged_cbs:
            cb({"session_id": "x"})

    def disconnect(self):
        self.disconnected = True


def _install(monkeypatch):
    holder = {}

    def factory(debug=False, server="data"):
        client = FakeClient(debug=debug, server=server)
        holder["client"] = client
        return client

    class ChartFactory(FakeChart):
        def __init__(self, client):
            super().__init__(client)
            holder["chart"] = self

    pkg = types.ModuleType("TradingView")
    pkg.Client = factory
    pkg.ChartSession = ChartFactory
    monkeypatch.setitem(sys.modules, "TradingView", pkg)
    return holder


def test_fetch_flushes_queue_on_logged(monkeypatch):
    """Tanpa hook on_logged, subscribe tidak pernah terkirim -> timeout."""
    holder = _install(monkeypatch)
    p = TradingViewProvider(timeout=5)
    df = p.fetch("EURUSD", "m1", bars=60)
    assert holder["client"].sent == ["flush"]
    assert holder["chart"].subscribed == ("FX:EURUSD", "1", 300)
    assert len(df) == 3
    assert holder["client"].disconnected is True


def test_fetch_returns_requested_tail(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5)
    df = p.fetch("EURUSD", "m5", bars=2)
    assert len(df) == 2


def test_quote_reports_lag_and_source(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5)
    q = p.quote("EURUSD")
    assert q["ticker"] == "FX:EURUSD"
    assert q["source"] == "tradingview"
    assert q["price"] == 1.15
    assert isinstance(q["lag_s"], int)


def test_fetch_raises_when_no_periods(monkeypatch):
    holder = _install(monkeypatch)
    p = TradingViewProvider(timeout=5)

    class Empty(FakeChart):
        def subscribe(self, symbol, timeframe, count):
            self._error("symbol_error")

    monkeypatch.setitem(sys.modules, "TradingView", types.ModuleType("TradingView"))
    sys.modules["TradingView"].Client = lambda debug=False, server="data": FakeClient()
    sys.modules["TradingView"].ChartSession = Empty
    with pytest.raises(TVUnavailable):
        p.fetch("NOPE", "m1", bars=60)


def test_missing_package_becomes_tvunavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "TradingView", None)
    p = TradingViewProvider(timeout=1)
    with pytest.raises(TVUnavailable) as err:
        p.fetch("EURUSD", "m1")
    assert "tradingview-api" in str(err.value)


def test_cache_ttl_skips_second_session(monkeypatch):
    holder = _install(monkeypatch)
    p = TradingViewProvider(timeout=5, cache_ttl=60.0)
    p.fetch("EURUSD", "m1", bars=60)
    first = holder["client"]
    p.fetch("EURUSD", "m1", bars=60)
    assert holder["client"] is first  # tidak ada client baru


def test_fetch_works_inside_running_event_loop(monkeypatch):
    """Node graph sudah di dalam loop; asyncio.run() langsung akan meledak."""
    import asyncio

    _install(monkeypatch)
    p = TradingViewProvider(timeout=5)

    async def scenario():
        return p.fetch("EURUSD", "m1", bars=60)

    df = asyncio.run(scenario())
    assert len(df) == 3


def test_retry_recovers_from_handshake_timeout(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5, retries=3)
    calls = {"n": 0}
    real = p._fetch_async

    async def flaky(ticker, tf, count):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TVUnavailable(
                "TradingView session gagal: TimeoutError: timed out during opening handshake"
            )
        return await real(ticker, tf, count)

    monkeypatch.setattr(p, "_fetch_async", flaky)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    df = p.fetch("EURUSD", "m1", bars=60)
    assert calls["n"] == 2
    assert len(df) == 3


def test_retry_gives_up_on_non_retryable_error(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5, retries=3)
    calls = {"n": 0}

    async def broken(ticker, tf, count):
        calls["n"] += 1
        raise TVUnavailable("TradingView tidak mengirim bar untuk NOPE 1")

    monkeypatch.setattr(p, "_fetch_async", broken)
    with pytest.raises(TVUnavailable):
        p.fetch("EURUSD", "m1", bars=60)
    assert calls["n"] == 1


def test_run_wraps_unexpected_errors(monkeypatch):
    p = TradingViewProvider(timeout=1)

    async def boom():
        raise ValueError("kaboom")

    with pytest.raises(TVUnavailable) as err:
        p._run(lambda: boom())
    assert "kaboom" in str(err.value)


@pytest.mark.asyncio
async def test_afetch_uses_loop_without_extra_thread(monkeypatch):
    """Jalur bot: afetch harus jalan di loop yang sama, tanpa threading."""
    import threading

    holder = _install(monkeypatch)
    p = TradingViewProvider(timeout=5)
    before = threading.active_count()
    df = await p.afetch("EURUSD", "m1", bars=60)
    assert len(df) == 3
    assert holder["chart"].subscribed == ("FX:EURUSD", "1", 300)
    assert threading.active_count() == before


@pytest.mark.asyncio
async def test_aquote_reports_lag(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5)
    q = await p.aquote("EURUSD")
    assert q["price"] == 1.15
    assert q["source"] == "tradingview"
    assert isinstance(q["lag_s"], int)


@pytest.mark.asyncio
async def test_afetch_retries_handshake_timeout(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5, retries=3)
    calls = {"n": 0}
    real = p._fetch_async

    async def flaky(ticker, tf, count):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TVUnavailable("timed out during opening handshake")
        return await real(ticker, tf, count)

    monkeypatch.setattr(p, "_fetch_async", flaky)
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    df = await p.afetch("EURUSD", "m1", bars=60)
    assert calls["n"] == 2
    assert len(df) == 3


@pytest.mark.asyncio
async def test_afetch_empty_frame_raises(monkeypatch):
    _install(monkeypatch)
    p = TradingViewProvider(timeout=5)

    async def empty(*_a, **_k):
        return _periods_to_frame({})

    monkeypatch.setattr(p, "_fetch_async", empty)
    with pytest.raises(TVUnavailable):
        await p.afetch("EURUSD", "m1", bars=60)
