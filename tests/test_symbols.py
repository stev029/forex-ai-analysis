"""Symbol mapping + pip sizing tests (no network)."""
from __future__ import annotations

import pytest

from forex_agents.market import SYMBOL_ALIASES, pip_size, to_yf_symbol


@pytest.mark.parametrize(
    "symbol,ticker",
    [
        ("EURUSD", "EURUSD=X"),
        ("gbpusd", "GBPUSD=X"),
        ("XAUUSD", "GC=F"),
        ("XAGUSD", "SI=F"),
        ("BTCUSD", "BTC-USD"),
        ("NAS100", "^NDX"),
        ("US30", "^DJI"),
        ("SPX500", "^GSPC"),
        ("USOIL", "CL=F"),
        ("DXY", "DX-Y.NYB"),
        ("GC=F", "GC=F"),
        ("^GSPC", "^GSPC"),
    ],
)
def test_to_yf_symbol(symbol, ticker):
    assert to_yf_symbol(symbol) == ticker


def test_aliases_are_uppercase_keys():
    assert all(k == k.upper() for k in SYMBOL_ALIASES)


@pytest.mark.parametrize(
    "symbol,pip",
    [
        ("EURUSD", 0.0001),
        ("GBPUSD", 0.0001),
        ("USDJPY", 0.01),
        ("XAUUSD", 0.1),
        ("NAS100", 1.0),
        ("BTCUSD", 1.0),
        ("DXY", 1.0),
    ],
)
def test_pip_size(symbol, pip):
    assert pip_size(symbol) == pytest.approx(pip)
