"""Serverless Postgres persistence: LangGraph checkpoints + analysis history."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .config import get_settings

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyses (
    id           BIGSERIAL PRIMARY KEY,
    request_id   TEXT UNIQUE NOT NULL,
    chat_id      BIGINT,
    symbol       TEXT NOT NULL,
    base_tf      TEXT NOT NULL,
    trend_tf     TEXT NOT NULL,
    validation_tf TEXT NOT NULL,
    critical_tf  TEXT NOT NULL,
    action       TEXT,
    confidence   INT,
    price        DOUBLE PRECISION,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS analyses_symbol_tf_idx ON analyses (symbol, base_tf, created_at DESC);
CREATE INDEX IF NOT EXISTS analyses_chat_idx ON analyses (chat_id, created_at DESC);
"""


@asynccontextmanager
async def checkpointer_cm() -> AsyncIterator[object | None]:
    """Yield a PostgresSaver, or None when DATABASE_URL is unset (memory-only run)."""
    url = get_settings().database_url
    if not url:
        log.warning("DATABASE_URL unset -> running without checkpoint persistence")
        yield None
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        conninfo=url, max_size=5, kwargs={"autocommit": True, "prepare_threshold": 0}, open=False
    ) as pool:
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver


@asynccontextmanager
async def db_cm() -> AsyncIterator[object | None]:
    url = get_settings().database_url
    if not url:
        yield None
        return
    import psycopg

    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
        yield conn
    finally:
        await conn.close()


async def save_analysis(conn, *, request_id: str, chat_id: int | None, state: dict) -> None:
    if conn is None:
        return
    import json

    decision = state.get("decision") or {}
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO analyses (request_id, chat_id, symbol, base_tf, trend_tf, validation_tf,
                                  critical_tf, action, confidence, price, payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (request_id) DO NOTHING
            """,
            (
                request_id,
                chat_id,
                state.get("symbol"),
                state.get("base_tf"),
                state.get("trend_tf"),
                state.get("validation_tf"),
                state.get("critical_tf"),
                decision.get("action"),
                decision.get("confidence"),
                state.get("price"),
                json.dumps(state, default=str),
            ),
        )


async def recent_analyses(conn, limit: int = 5) -> list[dict]:
    if conn is None:
        return []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT symbol, base_tf, action, confidence, created_at FROM analyses "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {"symbol": r[0], "tf": r[1], "action": r[2], "confidence": r[3], "at": r[4].isoformat()}
        for r in rows
    ]
