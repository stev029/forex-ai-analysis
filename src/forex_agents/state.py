"""LangGraph state schema for the multi-timeframe cascade."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AnalysisState(TypedDict, total=False):
    # input
    symbol: str
    base_tf: str
    trend_tf: str
    validation_tf: str
    critical_tf: str
    request_id: str

    # deterministic data
    snapshots: dict[str, dict[str, Any]]
    price: float
    price_time: str
    price_lag_s: int | None
    quote: dict[str, Any]
    market: dict[str, Any]
    pip: float
    memory: dict[str, Any]
    mode: str
    scalp: dict[str, Any]
    auto_tf: dict[str, Any]
    advice: dict[str, Any]

    # agent outputs (one per stage)
    trend: dict[str, Any]
    validation: dict[str, Any]
    critical: dict[str, Any]
    decision: dict[str, Any]
    vetoes: list[str]

    # bookkeeping
    errors: Annotated[list[str], operator.add]
    trace: Annotated[list[str], operator.add]
