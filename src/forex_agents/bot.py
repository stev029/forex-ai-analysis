"""Telegram bot: /analyze EURUSD m1, /history, /tf, /help."""
from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import get_settings
from .memory import db_cm, recent_analyses, save_analysis
from .render import render
from .runner import run_analysis
from .timeframes import cascade, normalize_tf

log = logging.getLogger(__name__)

HELP = (
    "<b>Forex cascade bot</b>\n"
    "/analyze [SYMBOL] [TF] — run the 4-stage cascade\n"
    "   e.g. <code>/analyze EURUSD m1</code>\n"
    "/tf m1 — show which timeframes a base TF maps to\n"
    "/symbols — supported symbols/aliases\n"
    "/history — last 5 runs\n"
    "/help — this message\n\n"
    "Stages: trend (base+2) → validation (base+1) → critical (base) → decision."
)


def _authorized(update: Update) -> bool:
    allowed = get_settings().allowed_user_ids
    if not allowed:
        return True
    user = update.effective_user
    return bool(user and user.id in allowed)


async def _guard(update: Update) -> bool:
    if _authorized(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("unauthorized")
    return False


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_tf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw = ctx.args[0] if ctx.args else get_settings().default_timeframe
    try:
        m = cascade(raw)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"base <code>{normalize_tf(raw)}</code>\n"
        f"trend: <code>{m['trend']}</code>\n"
        f"validation: <code>{m['validation']}</code>\n"
        f"critical: <code>{m['critical']}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    s = get_settings()
    args = ctx.args or []
    symbol = args[0] if args else s.default_symbol
    tf = args[1] if len(args) > 1 else s.default_timeframe
    chat = update.effective_chat
    msg = update.effective_message

    try:
        normalize_tf(tf)
    except ValueError as exc:
        await msg.reply_text(str(exc))
        return

    await chat.send_action(ChatAction.TYPING)
    await msg.reply_text(f"running cascade {symbol.upper()} {tf} …")

    try:
        final, rid = await asyncio.wait_for(
            run_analysis(symbol, tf, chat_id=chat.id), timeout=240
        )
    except asyncio.TimeoutError:
        await msg.reply_text("analysis timed out (240s)")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis failed")
        await msg.reply_text(f"analysis failed: {exc}")
        return

    try:
        async with db_cm() as conn:
            await save_analysis(conn, request_id=rid, chat_id=chat.id, state=final)
    except Exception as exc:  # noqa: BLE001 - persistence must not kill the reply
        log.exception("save_analysis failed")
        final.setdefault("errors", []).append(f"db: {exc}")

    await msg.reply_text(render(final), parse_mode=ParseMode.HTML)


async def cmd_symbols(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    from .market import SYMBOL_ALIASES

    pairs = " ".join(
        f"{k}→{v}" for k, v in sorted(SYMBOL_ALIASES.items())
    )
    await update.effective_message.reply_text(
        "<b>FX pairs</b>: any 6-letter code, e.g. <code>EURUSD</code> <code>GBPJPY</code> "
        "<code>USDIDR</code>\n\n"
        f"<b>Aliases</b>:\n<pre>{html.escape(pairs)}</pre>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_history(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    try:
        async with db_cm() as conn:
            rows = await recent_analyses(conn, 5)
    except Exception as exc:  # noqa: BLE001
        log.exception("history failed")
        await update.effective_message.reply_text(f"history failed: {exc}")
        return
    if not rows:
        await update.effective_message.reply_text("no history (or DATABASE_URL unset)")
        return
    body = "\n".join(
        f"{r['at'][:19]}  {r['symbol']} {r['tf']}  {r['action']} {r['confidence']}" for r in rows
    )
    await update.effective_message.reply_text(f"<pre>{body}</pre>", parse_mode=ParseMode.HTML)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled update error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"error: {ctx.error}")
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing")

    app = Application.builder().token(s.telegram_bot_token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("tf", cmd_tf))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_error_handler(on_error)
    log.info("bot polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
