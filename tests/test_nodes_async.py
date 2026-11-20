"""fetch_node harus memakai jalur async, bukan fetch() sinkron di dalam loop."""
from __future__ import annotations

import pandas as pd
import pytest

from forex_agents import nodes


def _df(n: int = 70) -> pd.DataFrame:
    idx = pd.date_range("2026-11-20", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.1] * n,
            "high": [1.2] * n,
            "low": [1.0] * n,
            "close": [1.15] * n,
            "volume": [1] * n,
        },
        index=idx,
    )


class AsyncProvider:
    """Provider bergaya TradingView: punya afetch/aquote."""

    def __init__(self) -> None:
        self.sync_calls = 0
        self.async_calls = 0

    async def afetch(self, symbol, timeframe, bars=300):
        self.async_calls += 1
        return _df(bars)

    async def aquote(self, symbol, ttl=5.0):
        return {"price": 1.15, "time": "2026-11-20 00:00:00+00:00", "lag_s": 3}

    def fetch(self, *a, **k):  # pragma: no cover - harus tidak pernah dipanggil
        self.sync_calls += 1
        raise AssertionError("fetch() sinkron dipanggil dari dalam event loop")

    def quote(self, *a, **k):  # pragma: no cover
        self.sync_calls += 1
        raise AssertionError("quote() sinkron dipanggil dari dalam event loop")


class SyncProvider:
    """Provider bergaya yfinance: hanya sinkron."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, symbol, timeframe, bars=300):
        self.calls += 1
        return _df(bars)

    def quote(self, symbol, ttl=5.0):
        self.calls += 1
        return {"price": 1.15, "time": "2026-11-20 00:00:00+00:00", "lag_s": 3}


@pytest.fixture()
def patch_provider(monkeypatch):
    def _install(provider):
        monkeypatch.setattr(nodes, "get_provider", lambda name: provider)
        return provider

    return _install


@pytest.mark.asyncio
async def test_fetch_node_uses_async_provider(monkeypatch, patch_provider):
    provider = patch_provider(AsyncProvider())
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: type(
            "S", (), {"market_provider": "tradingview", "market_fallback": ""}
        )(),
    )
    state = {"symbol": "XAUUSD", "base_tf": "m1"}
    out = await nodes.fetch_node(state)
    assert provider.async_calls >= 4  # probe + 3 timeframe + quote
    assert provider.sync_calls == 0
    assert set(out["snapshots"]) == {"m1", "m5", "m15"}
    assert out["price"] == 1.15


@pytest.mark.asyncio
async def test_fetch_node_moves_sync_provider_to_thread(monkeypatch, patch_provider):
    provider = patch_provider(SyncProvider())
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: type("S", (), {"market_provider": "yfinance", "market_fallback": ""})(),
    )
    out = await nodes.fetch_node({"symbol": "EURUSD", "base_tf": "m1"})
    assert provider.calls >= 4
    assert set(out["snapshots"]) == {"m1", "m5", "m15"}


@pytest.mark.asyncio
async def test_fetch_node_falls_back_when_primary_dies(monkeypatch):
    class Dead:
        async def afetch(self, *a, **k):
            raise RuntimeError("session mati")

        async def aquote(self, *a, **k):
            raise RuntimeError("session mati")

    good = AsyncProvider()
    mapping = {"tradingview": Dead(), "yfinance": good}
    monkeypatch.setattr(nodes, "get_provider", lambda name: mapping[name])
    monkeypatch.setattr(
        nodes,
        "get_settings",
        lambda: type(
            "S", (), {"market_provider": "tradingview", "market_fallback": "yfinance"}
        )(),
    )
    out = await nodes.fetch_node({"symbol": "XAUUSD", "base_tf": "m1"})
    assert set(out["snapshots"]) == {"m1", "m5", "m15"}
    assert any("provider tradingview" in e for e in out["errors"])
    assert "provider: yfinance" in out["trace"]
