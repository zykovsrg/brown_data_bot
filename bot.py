import os
import logging
import asyncio
from datetime import datetime, timezone

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEETS_WEBAPP_URL = os.getenv("SHEETS_WEBAPP_URL")
SHEETS_SECRET = os.getenv("SHEETS_SECRET")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Sheet1")


def keyboard_rate() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"score:{i}"))
        if i % 5 == 0:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton("🚨 Пукательная тревога", callback_data="anxiety")])
    return InlineKeyboardMarkup(rows)


def keyboard_next() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Оценить следующий покак", callback_data="next")]
    ])


def post_to_sheets(payload: dict) -> dict:
    if not SHEETS_WEBAPP_URL or not SHEETS_SECRET:
        raise RuntimeError("Missing SHEETS_WEBAPP_URL or SHEETS_SECRET")

    base = {
        "secret": SHEETS_SECRET,
        "sheetName": WORKSHEET_NAME,
    }
    base.update(payload)

    r = requests.post(SHEETS_WEBAPP_URL, json=base, timeout=20)
    logging.info("Sheets status=%s body=%s", r.status_code, r.text[:200])
    r.raise_for_status()

    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": "bad_json_response"}


def user_payload(user) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user.id,
        "username": user.username or "",
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
    }

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Оцени покак", reply_markup=keyboard_rate())


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ENV status\n"
        f"BOT_TOKEN set: {bool(BOT_TOKEN)}\n"
        f"SHEETS_WEBAPP_URL set: {bool(SHEETS_WEBAPP_URL)}\n"
        f"SHEETS_SECRET set: {bool(SHEETS_SECRET)}\n"
        f"WORKSHEET_NAME: {WORKSHEET_NAME}"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    def fetch_stats():
        return post_to_sheets({"action": "stats"})

    data = await asyncio.to_thread(fetch_stats)

    if not data.get("ok"):
        await update.message.reply_text("Не смог получить статистику. Попробуй позже.")
        return

    items = data.get("stats", [])
    if not items:
        await update.message.reply_text("Пока нет данных.")
        return

    lines = ["Статистика по людям:"]
    for u in items:
        label = u.get("name") or (("@" + u.get("username")) if u.get("username") else u.get("user_id"))
        cnt = u.get("score_count", 0)
        avg = u.get("score_avg")
        mn = u.get("score_min")
        mx = u.get("score_max")
        anx = u.get("anxiety_count", 0)
        last = u.get("last_score")

        avg_s = f"{avg:.2f}" if isinstance(avg, (int, float)) else "—"
        last_s = f"{last}/10" if isinstance(last, (int, float)) else "—"

        lines.append(
            f"\n{label}\n"
            f"Оценок: {cnt}, средняя: {avg_s}, мин: {mn or '—'}, макс: {mx or '—'}\n"
            f"Пукательная тревога: {anx}\n"
            f"Последняя оценка: {last_s}"
        )

    await update.message.reply_text("\n".join(lines))


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "next":
        await query.message.reply_text("Оцени покак", reply_markup=keyboard_rate())
        return

    if data == "anxiety":
        def send():
            payload = user_payload(query.from_user)
            payload.update({"anxiety": True})
            return post_to_sheets(payload)

        res = await asyncio.to_thread(send)
        if res.get("ok"):
            await query.edit_message_text("Записал: пукательная тревога 🚨")
            await query.message.reply_text("Готово.", reply_markup=keyboard_next())
        else:
            await query.edit_message_text("Не получилось записать. Попробуй ещё раз.")
        return

    if data.startswith("score:"):
        score = int(data.split(":", 1)[1])
        if not (1 <= score <= 10):
            await query.edit_message_text("Оценка должна быть от 1 до 10.")
            return

        def send():
            payload = user_payload(query.from_user)
            payload.update({"score": score})
            return post_to_sheets(payload)

        res = await asyncio.to_thread(send)
        if res.get("ok"):
            await query.edit_message_text(f"Записал: {score}/10 ✅")
            await query.message.reply_text("Готово.", reply_markup=keyboard_next())
        else:
            await query.edit_message_text("Не получилось записать. Попробуй ещё раз.")
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text.isdigit():
        score = int(text)
        if 1 <= score <= 10:
            def send():
                payload = user_payload(update.message.from_user)
                payload.update({"score": score})
                return post_to_sheets(payload)

            res = await asyncio.to_thread(send)
            if res.get("ok"):
                await update.message.reply_text(f"Записал: {score}/10 ✅", reply_markup=keyboard_next())
            else:
                await update.message.reply_text("Не получилось записать. Попробуй ещё раз.")
            return

    await update.message.reply_text("Пришли число 1–10 или жми /start.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
