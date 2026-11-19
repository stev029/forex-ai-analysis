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


def test_mt5linux_bridge_is_instantiated(monkeypatch):
    """mt5linux mengekspos class, bukan fungsi modul: harus di-instansiasi."""
    from forex_agents import market_mt5

    created: dict = {}

    class Bridge:
        def __init__(self, host="localhost", port=18812, timeout=300):
            created.update(host=host, port=port, timeout=timeout)

        def initialize(self, **kwargs):
            created["init"] = kwargs
            return True

    pkg = types.ModuleType("mt5linux")
    pkg.MetaTrader5 = Bridge
    monkeypatch.setitem(sys.modules, "mt5linux", pkg)
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)

    api = market_mt5._import_mt5("10.0.0.5", 19000, 42)
    assert isinstance(api, Bridge)
    assert created == {"host": "10.0.0.5", "port": 19000, "timeout": 42}
    assert market_mt5._is_bridge(api) is True


def test_bridge_passes_engine_when_supported(monkeypatch):
    """mt5linux 1.x menerima engine; 0.2.x tidak, jadi harus difilter."""
    from forex_agents import market_mt5

    seen: dict = {}

    class Bridge1x:
        def __init__(self, host="localhost", port=18812, timeout=300, engine="auto"):
            seen.update(host=host, port=port, timeout=timeout, engine=engine)

    pkg = types.ModuleType("mt5linux")
    pkg.MetaTrader5 = Bridge1x
    monkeypatch.setitem(sys.modules, "mt5linux", pkg)
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)

    market_mt5._import_mt5("127.0.0.1", 18812, 300, "udocker")
    assert seen == {"host": "127.0.0.1", "port": 18812, "timeout": 300, "engine": "udocker"}


def test_bridge_omits_engine_for_02x(monkeypatch):
    from forex_agents import market_mt5

    seen: dict = {}

    class Bridge02x:
        def __init__(self, host="localhost", port=18812):
            seen.update(host=host, port=port)

    pkg = types.ModuleType("mt5linux")
    pkg.MetaTrader5 = Bridge02x
    monkeypatch.setitem(sys.modules, "mt5linux", pkg)
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)

    market_mt5._import_mt5("127.0.0.1", 18812, 300, "auto")
    assert seen == {"host": "127.0.0.1", "port": 18812}


def test_bridge_start_failure_becomes_mt5unavailable(monkeypatch):
    """Tanpa docker/udocker, mt5linux 1.x raise RuntimeError -> MT5Unavailable."""
    from forex_agents import market_mt5

    class Bridge:
        def __init__(self, host="localhost", port=18812, timeout=300, engine="auto"):
            raise RuntimeError(
                "No container runtime available. engine='auto' but neither docker "
                "nor udocker is installed."
            )

    pkg = types.ModuleType("mt5linux")
    pkg.MetaTrader5 = Bridge
    monkeypatch.setitem(sys.modules, "mt5linux", pkg)
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)

    with pytest.raises(MT5Unavailable) as err:
        market_mt5._import_mt5()
    assert "No container runtime available" in str(err.value)
    assert "docker/udocker" in str(err.value)


def test_connect_does_not_pass_host_to_initialize(monkeypatch):
    fake = _install(monkeypatch, FakeMT5())
    p = MT5Provider(login=1234, password="pw", server="Broker-Demo")
    p.connect()
    assert fake.init_kwargs == {"login": 1234, "password": "pw", "server": "Broker-Demo"}
    assert "host" not in fake.init_kwargs and "port" not in fake.init_kwargs


@pytest.mark.asyncio
async def test_fetch_node_falls_back_when_mt5_dead(monkeypatch):
    """MARKET_PROVIDER=mt5 tanpa terminal harus jatuh ke MARKET_FALLBACK."""
    from types import SimpleNamespace

    from forex_agents import nodes
    from forex_agents.market import YFinanceProvider

    calls: list[str] = []

    class Dead:
        def fetch(self, *a, **k):
            raise MT5Unavailable("tidak ada terminal")

    def fake_get_provider(name):
        calls.append(name)
        return Dead() if name == "mt5" else YFinanceProvider()

    monkeypatch.setattr(nodes, "get_provider", fake_get_provider)
    monkeypatch.setattr(
        nodes, "get_settings",
        lambda: SimpleNamespace(market_provider="mt5", market_fallback="yfinance"),
    )
    monkeypatch.setattr(
        nodes, "snapshot",
        lambda df, tf, pip=0.0001: {"timeframe": tf, "last_price": float(df["close"].iloc[-1]),
                                   "last_bar_time": str(df.index[-1])},
    )

    class FakeProvider:
        def fetch(self, symbol, tf, bars=300):
            idx = pd.date_range("2026-01-01", periods=80, freq="1min", tz="UTC")
            return pd.DataFrame({"open": 1.1, "high": 1.2, "low": 1.0,
                                 "close": 1.15, "volume": 1.0}, index=idx)

        def quote(self, symbol, ttl=5.0):
            return {"price": 1.16, "time": "2026-01-01T01:20:00+00:00", "lag_s": 3}

    def dispatch(name):
        calls.append(name)
        return Dead() if name == "mt5" else FakeProvider()

    monkeypatch.setattr(nodes, "get_provider", dispatch)

    out = await nodes.fetch_node({"symbol": "EURUSD", "base_tf": "m1"})
    assert out["price"] == pytest.approx(1.16)
    assert out["price_lag_s"] == 3
    assert set(out["snapshots"]) == {"m15", "m5", "m1"}
    assert any("mt5" in e for e in out["errors"])
    assert "provider: yfinance" in out["trace"]
    assert calls == ["mt5", "yfinance"]
