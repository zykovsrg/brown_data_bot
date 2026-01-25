import os
import logging
import asyncio
from datetime import datetime, timezone

import json
from pathlib import Path

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

def load_settings():
    p = Path("settings.json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}

_settings = load_settings()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEETS_WEBAPP_URL = os.getenv("SHEETS_WEBAPP_URL") or _settings.get("SHEETS_WEBAPP_URL")
SHEETS_SECRET = os.getenv("SHEETS_SECRET") or _settings.get("SHEETS_SECRET")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME") or _settings.get("WORKSHEET_NAME", "Sheet1")


def build_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"score:{i}"))
        if i % 5 == 0:
            rows.append(row)
            row = []
    return InlineKeyboardMarkup(rows)


def send_score(user, score: int) -> None:
    if not SHEETS_WEBAPP_URL or not SHEETS_SECRET:
        raise RuntimeError("Нет SHEETS_WEBAPP_URL или SHEETS_SECRET")

    ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "secret": SHEETS_SECRET,
        "sheetName": WORKSHEET_NAME,
        "timestamp": ts,
        "user_id": user.id,
        "username": user.username or "",
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "score": score,
    }

    r = requests.post(SHEETS_WEBAPP_URL, json=payload, timeout=10)
    r.raise_for_status()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Оцени покак: 1–10", reply_markup=build_keyboard())


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("score:"):
        return

    score = int(data.split(":", 1)[1])
    if score < 1 or score > 10:
        await query.edit_message_text("Оценка должна быть от 1 до 10.")
        return

    await asyncio.to_thread(send_score, query.from_user, score)
    await query.edit_message_text(f"Записал: {score}/10 ✅")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text.isdigit():
        score = int(text)
        if 1 <= score <= 10:
            await asyncio.to_thread(send_score, update.message.from_user, score)
            await update.message.reply_text(f"Записал: {score}/10 ✅")
            return

    await update.message.reply_text("Пришли число от 1 до 10 или жми /start.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Нет переменной окружения BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
