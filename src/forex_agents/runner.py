"""Run one cascade end-to-end."""
from __future__ import annotations

import uuid

from .autotf import AUTO, is_auto
from .config import get_settings
from .graph import build_graph
from .memory import checkpointer_cm, db_cm, history_for
from .modes import normalize_mode
from .recall import HISTORY_LIMIT, build_state, parse_rows
from .state import AnalysisState
from .timeframes import cascade, normalize_tf


async def load_memory(symbol: str, base_tf: str, price: float | None = None,
                      limit: int = HISTORY_LIMIT) -> dict:
    """Ambil riwayat keputusan simbol+TF dari Postgres dan ringkas jadi ingatan."""
    try:
        async with db_cm() as conn:
            rows = await history_for(conn, symbol, base_tf, limit=limit)
    except Exception as exc:  # noqa: BLE001 - memori tidak boleh menggagalkan analisis
        return {"count": 0, "text": "", "error": f"memori tidak terbaca: {exc}"}
    return build_state(parse_rows(rows, price_now=price), limit=limit)


async def run_analysis(symbol: str, base_tf: str, *, chat_id: int | None = None,
                       request_id: str | None = None,
                       mode: str | None = None) -> tuple[dict, str]:
    auto = is_auto(base_tf)
    base = AUTO if auto else normalize_tf(base_tf)
    rid = request_id or uuid.uuid4().hex
    tf_map = (
        {"trend": "", "validation": "", "critical": ""}
        if auto
        else cascade(base)
    )
    sym = symbol.strip().upper()
    active_mode = normalize_mode(mode or get_settings().default_mode)
    memory = await load_memory(sym, base)

    init: AnalysisState = {
        "symbol": sym,
        "mode": active_mode,
        "base_tf": base,
        "trend_tf": tf_map["trend"],
        "validation_tf": tf_map["validation"],
        "critical_tf": tf_map["critical"],
        "request_id": rid,
        "snapshots": {},
        "memory": memory,
        "errors": [],
        "trace": [],
    }

    async with checkpointer_cm() as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": f"{sym}:{base}:{chat_id or 0}:{rid}"}}
        final = await graph.ainvoke(init, config=config)
    return final, rid
