"""Run one cascade end-to-end."""
from __future__ import annotations

import uuid

from .graph import build_graph
from .memory import checkpointer_cm
from .state import AnalysisState
from .timeframes import cascade, normalize_tf


async def run_analysis(symbol: str, base_tf: str, *, chat_id: int | None = None,
                       request_id: str | None = None) -> tuple[dict, str]:
    base = normalize_tf(base_tf)
    rid = request_id or uuid.uuid4().hex
    tf_map = cascade(base)

    init: AnalysisState = {
        "symbol": symbol.strip().upper(),
        "base_tf": base,
        "trend_tf": tf_map["trend"],
        "validation_tf": tf_map["validation"],
        "critical_tf": tf_map["critical"],
        "request_id": rid,
        "snapshots": {},
        "errors": [],
        "trace": [],
    }

    async with checkpointer_cm() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": f"{symbol}:{base}:{chat_id or 0}:{rid}"}}
        final = await graph.ainvoke(init, config=config)
    return final, rid
