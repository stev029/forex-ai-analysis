"""CLI: python -m forex_agents.cli EURUSD m1"""
from __future__ import annotations

import argparse
import asyncio
import json

from .memory import db_cm, save_analysis
from .render import render
from .runner import run_analysis


async def _main() -> None:
    ap = argparse.ArgumentParser(prog="forex-agents")
    ap.add_argument("symbol", nargs="?", default="EURUSD")
    ap.add_argument("timeframe", nargs="?", default="m1")
    ap.add_argument("--json", action="store_true", help="dump raw final state")
    args = ap.parse_args()

    final, rid = await run_analysis(args.symbol, args.timeframe)
    async with db_cm() as conn:
        await save_analysis(conn, request_id=rid, chat_id=None, state=final)

    if args.json:
        print(json.dumps(final, indent=2, default=str))
    else:
        print(render(final))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
