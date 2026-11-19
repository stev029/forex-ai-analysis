"""Pydantic contracts for each agent stage. Keeps LLM output parseable and auditable."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TrendOut(BaseModel):
    bias: Literal["bullish", "bearish", "range"] = Field(description="Higher-timeframe directional bias")
    strength: Literal["weak", "moderate", "strong"]
    key_levels: list[float] = Field(default_factory=list, description="Levels that matter on this TF")
    rationale: str = Field(description="<= 60 words, cite EMA stack / structure / momentum")


class ValidationOut(BaseModel):
    agrees_with_trend: bool
    phase: Literal["continuation", "pullback", "reversal", "consolidation"]
    pattern: str = Field(description="e.g. 'bull flag', 'double top', 'break-retest'")
    invalidation_level: float
    rationale: str


class CriticalOut(BaseModel):
    zones: list[dict] = Field(default_factory=list, description="[{type, low, high, why}]")
    nearest_support: float
    nearest_resistance: float
    entry_trigger: str = Field(description="Concrete price action needed before entry")
    rationale: str


class DecisionOut(BaseModel):
    action: Literal["BUY", "SELL", "NO_TRADE"]
    confidence: int = Field(ge=0, le=100)
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    rr: float | None = None
    reasons: list[str] = Field(default_factory=list)
    vetoes: list[str] = Field(default_factory=list)


STAGE_MODELS = {
    "trend": TrendOut,
    "validation": ValidationOut,
    "critical": CriticalOut,
    "decision": DecisionOut,
}
