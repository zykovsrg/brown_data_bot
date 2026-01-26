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

    try:
        r = requests.post(SHEETS_WEBAPP_URL, json=base, timeout=20)
        logging.info("Sheets status=%s body=%s", r.status_code, r.text[:200])
        r.raise_for_status()
    except requests.RequestException as e:
        logging.exception("Sheets request failed: %s", e)
        return {"ok": False, "error": "network"}

    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": "bad_json_response"}


def user_payload(user, chat_id: int) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user.id,
        "username": user.username or "",
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "chat_id": str(chat_id),
    }


async def notify_others(context: ContextTypes.DEFAULT_TYPE, current_chat_id: int, text: str) -> None:
    def fetch_chats():
        return post_to_sheets({"action": "chats"})

    data = await asyncio.to_thread(fetch_chats)
    if not data.get("ok"):
        logging.warning("Notify skipped: cannot fetch chats: %s", data)
        return

    chats = data.get("chats", [])
    for chat_id_str in chats:
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue

        if chat_id == current_chat_id:
            continue

        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logging.exception("Failed to notify chat_id=%s", chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Регистрируем chat_id в таблице (тихо), чтобы уведомления работали
    def register():
        payload = user_payload(update.effective_user, update.effective_chat.id)
        payload.update({"event": "start"})
        return post_to_sheets(payload)

    await asyncio.to_thread(register)

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
        if data.get("error") == "network":
            await update.message.reply_text("Не могу достучаться до Google. Попробуй позже.")
        else:
            await update.message.reply_text("Не смог получить статистику. Попробуй позже.")
        return

    items = data.get("stats", [])
    if not items:
        await update.message.reply_text("Пока нет данных.")
        return

    lines = ["Статистика: средняя оценка"]

    for u in items:
        label = u.get("name") or (("@" + u.get("username")) if u.get("username") else u.get("user_id"))

        avg7 = u.get("avg_7d")
        c7 = u.get("count_7d", 0)
        a7 = u.get("anxiety_7d", 0)

        avg30 = u.get("avg_30d")
        c30 = u.get("count_30d", 0)
        a30 = u.get("anxiety_30d", 0)

        avg7_s = f"{avg7:.2f}" if isinstance(avg7, (int, float)) else "—"
        avg30_s = f"{avg30:.2f}" if isinstance(avg30, (int, float)) else "—"

        lines.append(
            f"\n{label}\n"
            f"7 дней: средняя {avg7_s} (оценок {c7}), тревог {a7}\n"
            f"30 дней: средняя {avg30_s} (оценок {c30}), тревог {a30}"
        )

    await update.message.reply_text("\n".join(lines))


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    current_chat_id = query.message.chat_id

    if data == "next":
        await query.message.reply_text("Оцени покак", reply_markup=keyboard_rate())
        return

    if data == "anxiety":
        def send():
            payload = user_payload(query.from_user, current_chat_id)
            payload.update({"anxiety": True, "event": "anxiety"})
            return post_to_sheets(payload)

        res = await asyncio.to_thread(send)
        if res.get("ok"):
            await query.edit_message_text("Записал: пукательная тревога 🚨")
            await query.message.reply_text("Готово.", reply_markup=keyboard_next())
            await notify_others(context, current_chat_id, "Случилась пукательная тревога!")
        else:
            if res.get("error") == "network":
                await query.edit_message_text("Не могу достучаться до Google. Попробуй позже.")
            else:
                await query.edit_message_text("Не получилось записать. Попробуй ещё раз.")
        return

    if data.startswith("score:"):
        score = int(data.split(":", 1)[1])
        if not (1 <= score <= 10):
            await query.edit_message_text("Оценка должна быть от 1 до 10.")
            return

        def send():
            payload = user_payload(query.from_user, current_chat_id)
            payload.update({"score": score, "event": "score"})
            return post_to_sheets(payload)

        res = await asyncio.to_thread(send)
        if res.get("ok"):
            await query.edit_message_text(f"Записал: {score}/10 ✅")
            await query.message.reply_text("Готово.", reply_markup=keyboard_next())
            await notify_others(context, current_chat_id, f"Кое-кто покакал! Оценка: {score}")
        else:
            if res.get("error") == "network":
                await query.edit_message_text("Не могу достучаться до Google. Попробуй позже.")
            else:
                await query.edit_message_text("Не получилось записать. Попробуй ещё раз.")
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    current_chat_id = update.effective_chat.id

    if text.isdigit():
        score = int(text)
        if 1 <= score <= 10:
            def send():
                payload = user_payload(update.effective_user, current_chat_id)
                payload.update({"score": score, "event": "score"})
                return post_to_sheets(payload)

            res = await asyncio.to_thread(send)
            if res.get("ok"):
                await update.message.reply_text(f"Записал: {score}/10 ✅", reply_markup=keyboard_next())
                await notify_others(context, current_chat_id, f"Кое-кто покакал! Оценка: {score}")
            else:
                if res.get("error") == "network":
                    await update.message.reply_text("Не могу достучаться до Google. Попробуй позже.")
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
