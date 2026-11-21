"""Telegram bot: /analyze EURUSD m1, /schedule, /history, /tf, /help."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import get_settings
from .memory import (
    db_cm,
    delete_schedule,
    list_schedules,
    recent_analyses,
    save_analysis,
    save_schedule,
    touch_schedule,
)
from .render import render, split_message
from .runner import run_analysis
from .scheduler import (
    MAX_JOBS_PER_CHAT,
    Scheduler,
    auto_interval,
    human_interval,
    parse_interval,
    targets,
)
from .timeframes import cascade, normalize_tf

log = logging.getLogger(__name__)

# diset di main(); command handler memakainya bila scheduler aktif
SCHED: Scheduler | None = None

HELP = (
    "<b>Bot cascade forex</b>\n"
    "/analyze [SIMBOL] [TF] — jalankan cascade 4 tahap\n"
    "   contoh: <code>/analyze EURUSD m1</code>\n"
    "/schedule add [SIMBOL] [TF] [INTERVAL] — analisis otomatis berkala\n"
    "   contoh: <code>/schedule add XAUUSD m1</code> → tiap 15 menit\n"
    "   contoh: <code>/schedule add EURUSD m1 30m</code> → tiap 30 menit\n"
    "/schedule list — jadwal aktif\n"
    "/schedule del &lt;ID|all&gt; — hapus jadwal\n"
    "/schedule run &lt;ID&gt; — jalankan sekarang\n"
    "/tf m1 — lihat pemetaan timeframe dari TF dasar\n"
    "/symbols — daftar simbol/alias yang didukung\n"
    "/history — 5 analisis terakhir\n"
    "/help — pesan ini\n\n"
    "Tahapan: tren (base+2) → validasi (base+1) → area kritis (base) → keputusan.\n"
    "Interval jadwal mengikuti TF target terbesar: m1 → 15m, m5 → 30m, m15 → 1h, "
    "m30 → 4h, h1 → 1d, h4 → 1w."
)

INTERVAL_HELP = (
    "Format interval: <code>15</code> (menit), <code>15m</code>, <code>30mnt</code>, "
    "<code>2h</code>, <code>1d</code>, <code>1w</code>."
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
        await update.effective_message.reply_text("tidak diizinkan")
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
        f"dasar <code>{normalize_tf(raw)}</code>\n"
        f"tren: <code>{m['trend']}</code>\n"
        f"validasi: <code>{m['validation']}</code>\n"
        f"area kritis: <code>{m['critical']}</code>",
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
    await msg.reply_text(f"menjalankan cascade {symbol.upper()} {tf} …")

    try:
        final, rid = await asyncio.wait_for(
            run_analysis(symbol, tf, chat_id=chat.id), timeout=240
        )
    except asyncio.TimeoutError:
        await msg.reply_text("analisis melebihi batas waktu (240 detik)")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis failed")
        await msg.reply_text(f"analisis gagal: {exc}")
        return

    try:
        async with db_cm() as conn:
            await save_analysis(conn, request_id=rid, chat_id=chat.id, state=final)
    except Exception as exc:  # noqa: BLE001 - persistence must not kill the reply
        log.exception("save_analysis failed")
        final.setdefault("errors", []).append(f"db: {exc}")

    for part in split_message(render(final)):
        await msg.reply_text(part, parse_mode=ParseMode.HTML)


async def cmd_symbols(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    from .market import SYMBOL_ALIASES

    pairs = " ".join(
        f"{k}→{v}" for k, v in sorted(SYMBOL_ALIASES.items())
    )
    await update.effective_message.reply_text(
        "<b>Pasangan FX</b>: kode 6 huruf apa pun, mis. <code>EURUSD</code> <code>GBPJPY</code> "
        "<code>USDIDR</code>\n\n"
        f"<b>Alias</b>:\n<pre>{html.escape(pairs)}</pre>",
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
        await update.effective_message.reply_text(f"gagal membaca riwayat: {exc}")
        return
    if not rows:
        await update.effective_message.reply_text("belum ada riwayat (atau DATABASE_URL kosong)")
        return
    body = "\n".join(
        f"{r['at'][:19]}  {r['symbol']} {r['tf']}  {r['action']} {r['confidence']}" for r in rows
    )
    await update.effective_message.reply_text(f"<pre>{body}</pre>", parse_mode=ParseMode.HTML)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled update error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"kesalahan: {ctx.error}")
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------- schedule
async def _persist_save(job) -> None:
    try:
        async with db_cm() as conn:
            await save_schedule(conn, job)
    except Exception:  # noqa: BLE001 - jadwal tetap jalan tanpa DB
        log.exception("gagal simpan jadwal %s", job.id)


async def _persist_delete(job_id: str) -> None:
    try:
        async with db_cm() as conn:
            await delete_schedule(conn, job_id)
    except Exception:  # noqa: BLE001
        log.exception("gagal hapus jadwal %s", job_id)


async def _persist_touch(job, _event: str) -> None:
    try:
        async with db_cm() as conn:
            await touch_schedule(conn, job.id, failures=job.failures)
    except Exception:  # noqa: BLE001
        log.exception("gagal update jadwal %s", job.id)


async def _telegram_deliver(chat_id: int, text: str) -> None:
    app = SCHED_APP
    if app is None:
        raise RuntimeError("bot belum siap")
    await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def _scheduled_run(symbol: str, base_tf: str, *, chat_id: int | None = None):
    """run_analysis + simpan ke tabel analyses, dipakai scheduler."""
    final, rid = await run_analysis(symbol, base_tf, chat_id=chat_id)
    try:
        async with db_cm() as conn:
            await save_analysis(conn, request_id=rid, chat_id=chat_id, state=final)
    except Exception as exc:  # noqa: BLE001 - DB tidak boleh membatalkan kiriman
        log.exception("save_analysis jadwal gagal")
        final.setdefault("errors", []).append(f"db: {exc}")
    return final


SCHED_APP: Application | None = None


def _fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S UTC")


def _schedule_rows(chat_id: int) -> str:
    assert SCHED is not None
    jobs = SCHED.jobs(chat_id)
    if not jobs:
        return (
            "belum ada jadwal di chat ini.\n"
            "tambah: <code>/schedule add XAUUSD m1</code>"
        )
    lines = []
    for job in jobs:
        status = "aktif" if job.enabled else "nonaktif"
        lines.append(
            f"<code>{job.id}</code>  {job.symbol} {job.base_tf}"
            f"  tiap {human_interval(job.interval_min)}"
            f"  ({status}, {job.runs}x, gagal {job.failures})\n"
            f"    berikutnya {_fmt_ts(job.next_run)}"
        )
    return "\n".join(lines)


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    msg = update.effective_message
    chat = update.effective_chat

    if SCHED is None:
        await msg.reply_text("scheduler tidak aktif di proses ini")
        return

    args = list(ctx.args or [])
    sub = (args.pop(0).lower() if args else "list")

    if sub in ("add", "tambah", "+"):
        s = get_settings()
        symbol = args[0] if args else s.default_symbol
        tf = args[1] if len(args) > 1 else s.default_timeframe
        try:
            base = normalize_tf(tf)
        except ValueError as exc:
            await msg.reply_text(str(exc))
            return
        interval: int | None = None
        if len(args) > 2:
            try:
                interval = parse_interval(args[2])
            except ValueError as exc:
                await msg.reply_text(f"{exc}\n{INTERVAL_HELP}", parse_mode=ParseMode.HTML)
                return
        try:
            job = SCHED.add(chat.id, symbol, base, interval)
        except ValueError as exc:
            await msg.reply_text(str(exc))
            return
        await _persist_save(job)
        trend, validation = targets(base)
        await msg.reply_text(
            f"jadwal <code>{job.id}</code> dibuat: {job.symbol} {job.base_tf}\n"
            f"target TF: {validation} + {trend}\n"
            f"interval: tiap {human_interval(job.interval_min)} "
            f"(bar tutup {trend})\n"
            f"berikutnya: {_fmt_ts(job.next_run)}\n"
            f"maks {MAX_JOBS_PER_CHAT} jadwal per chat. hapus: "
            f"<code>/schedule del {job.id}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if sub in ("list", "ls", "daftar"):
        await msg.reply_text(_schedule_rows(chat.id), parse_mode=ParseMode.HTML)
        return

    if sub in ("del", "delete", "hapus", "rm", "-"):
        selector = args[0] if args else "all"
        removed = SCHED.remove(selector, chat_id=chat.id)
        for job in removed:
            await _persist_delete(job.id)
        if not removed:
            await msg.reply_text(f"jadwal '{selector}' tidak ditemukan di chat ini")
            return
        names = ", ".join(f"{j.id} ({j.symbol} {j.base_tf})" for j in removed)
        await msg.reply_text(f"dihapus: {names}")
        return

    if sub in ("run", "now", "sekarang"):
        if not args:
            await msg.reply_text("pakai: <code>/schedule run &lt;ID&gt;</code>", parse_mode=ParseMode.HTML)
            return
        job = SCHED.get(args[0].lower())
        if job is None or job.chat_id != chat.id:
            await msg.reply_text(f"jadwal '{args[0]}' tidak ditemukan")
            return
        await msg.reply_text(f"menjalankan {job.symbol} {job.base_tf} sekarang …")
        await SCHED._fire(job)
        return

    await msg.reply_text(
        "subperintah: <code>add</code> | <code>list</code> | <code>del</code> | <code>run</code>\n"
        "contoh: <code>/schedule add XAUUSD m1</code>"
        " — tiap 15 menit (otomatis dari TF)\n"
        "contoh: <code>/schedule add EURUSD m1 30m</code> — interval custom\n"
        f"{INTERVAL_HELP}",
        parse_mode=ParseMode.HTML,
    )


async def _post_init(app: Application) -> None:
    global SCHED, SCHED_APP
    SCHED_APP = app
    SCHED = Scheduler(_telegram_deliver, runner=_scheduled_run, persist=_persist_touch)
    try:
        async with db_cm() as conn:
            rows = await list_schedules(conn)
    except Exception:  # noqa: BLE001 - DB mati bukan alasan bot mati
        log.exception("gagal memuat jadwal dari DB")
        rows = []
    if rows:
        log.info("memulihkan %d jadwal", SCHED.restore(rows))
    SCHED.start()
    await app.bot.set_my_commands(
        [
            BotCommand("analyze", "cascade manual: /analyze EURUSD m1"),
            BotCommand("schedule", "jadwal otomatis: /schedule add XAUUSD m1 15m"),
            BotCommand("history", "5 analisis terakhir"),
            BotCommand("tf", "pemetaan timeframe"),
            BotCommand("symbols", "simbol & alias"),
            BotCommand("help", "bantuan"),
        ]
    )


async def _post_shutdown(app: Application) -> None:
    if SCHED is not None:
        await SCHED.stop()



def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx mencetak URL lengkap termasuk token bot -> naikkan ke WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    s = get_settings()
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diisi")

    app = (
        Application.builder()
        .token(s.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("tf", cmd_tf))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler(["schedule", "jadwal", "cron"], cmd_schedule))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_error_handler(on_error)
    log.info("bot polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
