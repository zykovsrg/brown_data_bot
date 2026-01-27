import os
import logging
import asyncio
from datetime import datetime, timezone, time, timedelta
from zoneinfo import ZoneInfo
import html

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

TZ = ZoneInfo("Europe/Moscow")
ALARM_TEXT = "Привет! Коричневая тишина — это подозрительно. Не забудь добавить покаки!"


def keyboard_rate() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"score:{i}"))
        if i % 4 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("Пукательная тревога", callback_data="anxiety")])
    return InlineKeyboardMarkup(rows)


def keyboard_next() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Оценить покак", callback_data="next")]
    ])


def keyboard_react() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💛 Радость", callback_data="react:joy"),
            InlineKeyboardButton("🤍 Белая зависть", callback_data="react:white_envy"),
        ],
        [
            InlineKeyboardButton("🖤 Чёрная зависть", callback_data="react:black_envy"),
            InlineKeyboardButton("💜 Сочувствие", callback_data="react:empathy"),
        ],
        [
            InlineKeyboardButton("💩 Злорадство", callback_data="react:schadenfreude"),
        ],
    ])


def post_to_sheets(payload: dict) -> dict:
    if not SHEETS_WEBAPP_URL or not SHEETS_SECRET:
        raise RuntimeError("Missing SHEETS_WEBAPP_URL or SHEETS_SECRET")

    base = {"secret": SHEETS_SECRET, "sheetName": WORKSHEET_NAME}
    base.update(payload)

    try:
        r = requests.post(SHEETS_WEBAPP_URL, json=base, timeout=20)
        # секреты/токены не логируем
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


def display_name(user) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


async def register_chat(update: Update) -> None:
    def register():
        payload = user_payload(update.effective_user, update.effective_chat.id)
        payload.update({"event": "start"})
        return post_to_sheets(payload)

    await asyncio.to_thread(register)


async def fetch_all_chats() -> list[str]:
    def f():
        return post_to_sheets({"action": "chats"})
    data = await asyncio.to_thread(f)
    if not data.get("ok"):
        return []
    return data.get("chats", [])


async def fetch_alarm_chats() -> list[str]:
    def f():
        return post_to_sheets({"action": "alarm_chats"})
    data = await asyncio.to_thread(f)
    if not data.get("ok"):
        return []
    return data.get("chats", [])


async def set_alarm(chat_id: int, enabled: bool) -> bool:
    def f():
        return post_to_sheets({"action": "alarm_set", "chat_id": str(chat_id), "enabled": enabled})
    data = await asyncio.to_thread(f)
    return bool(data.get("ok"))


async def has_recent_activity(hours: int = 24) -> bool:
    def f():
        return post_to_sheets({"action": "has_recent_activity", "hours": hours})
    data = await asyncio.to_thread(f)
    return bool(data.get("ok")) and bool(data.get("has_recent"))


async def notify_others(context: ContextTypes.DEFAULT_TYPE, current_chat_id: int, text: str) -> None:
    chats = await fetch_all_chats()
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


# ===== Alarm job (22:00 MSK) =====
async def alarm_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # если за последние 24 часа нет оценок/тревог — шлём напоминание в чаты, где alarm_enabled=true
    try:
        recent = await has_recent_activity(24)
        if recent:
            return

        chats = await fetch_alarm_chats()
        for chat_id_str in chats:
            try:
                chat_id = int(chat_id_str)
            except Exception:
                continue
            try:
                await context.bot.send_message(chat_id=chat_id, text=ALARM_TEXT)
            except Exception:
                logging.exception("Failed to send alarm to chat_id=%s", chat_id)
    except Exception:
        logging.exception("alarm_job failed")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_chat(update)
    await update.message.reply_text("Оцени покак:", reply_markup=keyboard_rate())


async def react(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_chat(update)
    await update.message.reply_text("Выбери реакцию:", reply_markup=keyboard_react())


async def alarm_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_chat(update)
    ok = await set_alarm(update.effective_chat.id, True)
    await update.message.reply_text("Ок. Напоминания включены." if ok else "Не получилось включить. Попробуй позже.")


async def alarm_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_chat(update)
    ok = await set_alarm(update.effective_chat.id, False)
    await update.message.reply_text("Ок. Напоминания выключены." if ok else "Не получилось выключить. Попробуй позже.")


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
        await update.message.reply_text(
            "Не могу достучаться до Google. Попробуй позже." if data.get("error") == "network"
            else "Не смог получить статистику. Попробуй позже."
        )
        return

    items = data.get("stats", [])
    if not items:
        await update.message.reply_text("Пока нет данных.")
        return

    parts = ["Статистика покаков за последнее время:"]

    for u in items:
        label_raw = u.get("name") or (("@" + u.get("username")) if u.get("username") else str(u.get("user_id")))
        label = html.escape(label_raw)

        avg7 = u.get("avg_7d")
        c7 = u.get("count_7d", 0)
        a7 = u.get("anxiety_7d", 0)

        avg30 = u.get("avg_30d")
        c30 = u.get("count_30d", 0)
        a30 = u.get("anxiety_30d", 0)

        avg7_s = f"{avg7:.1f}" if isinstance(avg7, (int, float)) else "—"
        avg30_s = f"{avg30:.1f}" if isinstance(avg30, (int, float)) else "—"

        parts.append(
            f"\n<b>{label}</b>\n\n"
            f"7 дней\n"
            f"Средняя оценка: {avg7_s}\n"
            f"Количество успешных покаков: {c7}\n"
            f"Пукательных тревог: {a7}\n\n"
            f"30 дней\n"
            f"Средняя оценка: {avg30_s}\n"
            f"Количество успешных покаков: {c30}\n"
            f"Пукательных тревог: {a30}"
        )

    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    current_chat_id = query.message.chat_id

    if data == "next":
        await query.edit_message_text("Оцени покак:", reply_markup=keyboard_rate())
        return

    if data.startswith("react:"):
        key = data.split(":", 1)[1]
        name = display_name(query.from_user)

        notify_map = {
            "joy": f"Отлично покакано! {name} радуется!",
            "white_envy": f"{name} завидует",
            "black_envy": f"{name} завидует по-чёрному",
            "empathy": f"{name} сочувствует!",
            "schadenfreude": f"{name} считает, что это полностью заслуженно",
        }
        label_map = {
            "joy": "💛 Радость",
            "white_envy": "🤍 Белая зависть",
            "black_envy": "🖤 Чёрная зависть",
            "empathy": "💜 Сочувствие",
            "schadenfreude": "💩 Злорадство",
        }

        text = notify_map.get(key)
        label = label_map.get(key, "Реакция")
        if not text:
            await query.edit_message_text("Не понял реакцию. Попробуй ещё раз.")
            return

        await query.edit_message_text(f"Отправил реакцию: {label}")
        await notify_others(context, current_chat_id, text)
        return

    if data == "anxiety":
        def send():
            payload = user_payload(query.from_user, current_chat_id)
            payload.update({"anxiety": True, "event": "anxiety"})
            return post_to_sheets(payload)

        res = await asyncio.to_thread(send)
        if res.get("ok"):
            await query.edit_message_text("Записал: пукательная тревога ✅", reply_markup=keyboard_next())
            await notify_others(context, current_chat_id, "Случилась пукательная тревога!")
        else:
            await query.edit_message_text(
                "Не могу достучаться до Google. Попробуй позже." if res.get("error") == "network"
                else "Не получилось записать. Попробуй ещё раз."
            )
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
            await query.edit_message_text(f"Записал: {score}/10 ✅", reply_markup=keyboard_next())
            await notify_others(context, current_chat_id, f"Кое-кто покакал! Оценка: {score}")
        else:
            await query.edit_message_text(
                "Не могу достучаться до Google. Попробуй позже." if res.get("error") == "network"
                else "Не получилось записать. Попробуй ещё раз."
            )
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
                await update.message.reply_text(
                    "Не могу достучаться до Google. Попробуй позже." if res.get("error") == "network"
                    else "Не получилось записать. Попробуй ещё раз."
                )
            return

    await update.message.reply_text("Пришли число 1–10 или жми /start.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("react", react))
    app.add_handler(CommandHandler("alarm_on", alarm_on))
    app.add_handler(CommandHandler("alarm_off", alarm_off))
    app.add_handler(CommandHandler("debug", debug))

    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ежедневное напоминание в 22:00 по Москве
    if app.job_queue:
        app.job_queue.run_daily(
            alarm_job,
            time=time(hour=22, minute=0, tzinfo=TZ),
            name="daily_alarm_22_msk",
        )
    else:
        logging.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
