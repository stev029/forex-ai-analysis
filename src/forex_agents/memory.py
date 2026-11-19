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
    request_id   TEXT UNIQUE,
    chat_id      BIGINT,
    symbol       TEXT NOT NULL,
    base_tf      TEXT,
    trend_tf     TEXT,
    validation_tf TEXT,
    critical_tf  TEXT,
    action       TEXT,
    confidence   INT,
    price        DOUBLE PRECISION,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- migrate pre-existing tables (older schema: decision/entry/sl/tp, no request_id/base_tf)
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS request_id    TEXT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS base_tf       TEXT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS trend_tf      TEXT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS validation_tf TEXT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS critical_tf   TEXT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS action        TEXT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS confidence    INT;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS price         DOUBLE PRECISION;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS payload       JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE analyses ALTER COLUMN payload SET DEFAULT '{}'::jsonb;

-- legacy NOT NULL columns must not block new inserts, and legacy numeric(5,4) price columns
-- overflow for gold/indices/crypto (e.g. 3693.8) - widen them.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analyses' AND column_name = 'decision') THEN
        ALTER TABLE analyses ALTER COLUMN decision DROP NOT NULL;
        UPDATE analyses SET action = decision WHERE action IS NULL AND decision IS NOT NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analyses' AND column_name = 'confidence'
                 AND data_type = 'numeric') THEN
        ALTER TABLE analyses ALTER COLUMN confidence TYPE DOUBLE PRECISION;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analyses' AND column_name = 'entry'
                 AND data_type = 'numeric') THEN
        ALTER TABLE analyses ALTER COLUMN entry TYPE DOUBLE PRECISION;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analyses' AND column_name = 'sl'
                 AND data_type = 'numeric') THEN
        ALTER TABLE analyses ALTER COLUMN sl TYPE DOUBLE PRECISION;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analyses' AND column_name = 'tp'
                 AND data_type = 'numeric') THEN
        ALTER TABLE analyses ALTER COLUMN tp TYPE DOUBLE PRECISION;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'analyses' AND column_name = 'price'
                 AND data_type = 'numeric') THEN
        ALTER TABLE analyses ALTER COLUMN price TYPE DOUBLE PRECISION;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS analyses_request_id_key ON analyses (request_id);
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
        conninfo=url, max_size=5, kwargs={"autocommit": True, "prepare_threshold": None}, open=False
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
            "SELECT symbol, coalesce(base_tf, payload->>'base_tf', '?'), "
            "coalesce(action, payload->'decision'->>'action', '?'), confidence, created_at "
            "FROM analyses ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {"symbol": r[0], "tf": r[1], "action": r[2], "confidence": r[3], "at": r[4].isoformat()}
        for r in rows
    ]
