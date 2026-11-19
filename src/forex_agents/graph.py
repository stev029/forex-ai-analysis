"""Graph wiring: linear cascade with a data gate."""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from .nodes import critical_node, decision_node, fetch_node, trend_node, validation_node
from .state import AnalysisState


def _has_data(state: AnalysisState) -> str:
    return "decision" if not state.get("snapshots") else "trend"


def build_graph(checkpointer=None):
    g = StateGraph(AnalysisState)
    g.add_node("fetch", fetch_node)
    g.add_node("trend", trend_node)
    g.add_node("validation", validation_node)
    g.add_node("critical", critical_node)
    g.add_node("decision", decision_node)

    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch", _has_data, {"trend": "trend", "decision": "decision"})
    g.add_edge("trend", "validation")
    g.add_edge("validation", "critical")
    g.add_edge("critical", "decision")
    g.add_edge("decision", END)

    return g.compile(checkpointer=checkpointer)


@lru_cache(maxsize=1)
def get_graph(checkpointer=None):
    return build_graph(checkpointer)
