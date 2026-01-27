import os
import json
import logging
import asyncio
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
import html

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest
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

DATA_DIR = "/app/data"
QUEUE_PATH = os.path.join(DATA_DIR, "queue.jsonl")

queue_lock = asyncio.Lock()


# ---------- Keyboards ----------
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
    return InlineKeyboardMarkup([[InlineKeyboardButton("Оценить покак", callback_data="next")]])


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


# ---------- Safe Telegram wrappers ----------
async def _retry_sleep(attempt: int, base: float = 0.7) -> None:
    await asyncio.sleep(base * (2 ** attempt))


async def safe_answer(query, text: str | None = None) -> None:
    for attempt in range(4):
        try:
            await query.answer(text=text)
            return
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)))
        except (TimedOut, NetworkError) as e:
            logging.warning("answerCallbackQuery network error: %s", e)
            await _retry_sleep(attempt)
        except Exception as e:
            logging.exception("answerCallbackQuery failed: %s", e)
            return


async def safe_edit_or_send(query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> None:
    for attempt in range(4):
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        except BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                return
            logging.warning("edit_message_text bad request: %s", e)
            break
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)))
        except (TimedOut, NetworkError) as e:
            logging.warning("edit_message_text network error: %s", e)
            await _retry_sleep(attempt)
        except Exception as e:
            logging.exception("edit_message_text failed: %s", e)
            break

    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup)
    except Exception as e:
        logging.exception("fallback send_message failed: %s", e)


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for attempt in range(4):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            return
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)))
        except (TimedOut, NetworkError) as e:
            logging.warning("send_message network error: %s", e)
            await _retry_sleep(attempt)
        except Exception as e:
            logging.exception("send_message failed: %s", e)
            return


# ---------- Sheets ----------
def post_to_sheets(payload: dict) -> dict:
    if not SHEETS_WEBAPP_URL or not SHEETS_SECRET:
        raise RuntimeError("Missing SHEETS_WEBAPP_URL or SHEETS_SECRET")

    base = {"secret": SHEETS_SECRET, "sheetName": WORKSHEET_NAME}
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


def display_name(user) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


# ---------- Queue ----------
def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(QUEUE_PATH):
        with open(QUEUE_PATH, "a", encoding="utf-8"):
            pass


async def enqueue_event(event_payload: dict) -> None:
    ensure_data_dir()
    line = json.dumps(event_payload, ensure_ascii=False)
    async with queue_lock:
        await asyncio.to_thread(_append_line, QUEUE_PATH, line)


def _append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def queue_status() -> dict:
    ensure_data_dir()
    async with queue_lock:
        lines = await asyncio.to_thread(_read_lines, QUEUE_PATH)

    items = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            items.append(json.loads(ln))
        except Exception:
            continue

    count = len(items)
    oldest = None
    if count:
        oldest = items[0].get("timestamp")
    return {"count": count, "oldest": oldest}


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


async def flush_queue_once() -> dict:
    """
    Пытается отправить очередь в Google.
    Удаляет из файла только успешно отправленные.
    Останавливается на первом сетевом фейле.
    """
    ensure_data_dir()

    async with queue_lock:
        lines = await asyncio.to_thread(_read_lines, QUEUE_PATH)

        items = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                items.append(json.loads(ln))
            except Exception:
                # битая строка — пропускаем
                continue

        if not items:
            return {"ok": True, "sent": 0, "left": 0}

        sent = 0
        remaining = []

        for payload in items:
            res = await asyncio.to_thread(post_to_sheets, payload)
            if res.get("ok"):
                sent += 1
                continue
            # если сеть упала — оставляем это и всё после
            remaining.append(payload)
            remaining.extend(items[items.index(payload) + 1:])
            break

        # переписываем файл очереди
        await asyncio.to_thread(_rewrite_queue, QUEUE_PATH, remaining)

    return {"ok": True, "sent": sent, "left": len(remaining)}


def _rewrite_queue(path: str, items: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


async def send_or_queue(event_payload: dict) -> dict:
    """
    1) сначала пытаемся догнать старое
    2) потом отправить текущее
    3) если не получилось — кладём текущее в очередь
    """
    await flush_queue_once()

    res = await asyncio.to_thread(post_to_sheets, event_payload)
    if res.get("ok"):
        # если получилось — на всякий случай ещё раз догоняем хвост
        await flush_queue_once()
        return {"ok": True, "queued": False}

    await enqueue_event(event_payload)
    return {"ok": False, "queued": True, "error": res.get("error")}


# ---------- Sheets helpers ----------
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
        await safe_send(context, chat_id, text)


# ---------- Jobs ----------
async def alarm_job(context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await safe_send(context, chat_id, ALARM_TEXT)
    except Exception:
        logging.exception("alarm_job failed")


async def flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        st = await flush_queue_once()
        if st.get("sent"):
            logging.info("Queue flushed: sent=%s left=%s", st.get("sent"), st.get("left"))
    except Exception:
        logging.exception("flush_job failed")


# ---------- Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Оцени покак:", reply_markup=keyboard_rate())


async def react(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Выбери реакцию:", reply_markup=keyboard_react())


async def alarm_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = await set_alarm(update.effective_chat.id, True)
    await update.message.reply_text("Ок. Напоминания включены." if ok else "Не получилось включить. Попробуй позже.")


async def alarm_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telegram ok?
    telegram_ok = False
    telegram_err = None
    try:
        await context.bot.get_me()
        telegram_ok = True
    except Exception as e:
        telegram_err = str(e)

    # Google ok? (пробуем лёгкий action=chats)
    google_ok = False
    google_err = None
    try:
        data = await asyncio.to_thread(post_to_sheets, {"action": "chats"})
        google_ok = bool(data.get("ok"))
        if not google_ok:
            google_err = data.get("error") or "unknown"
    except Exception as e:
        google_err = str(e)

    q = await queue_status()

    msg = (
        "health\n"
        f"Telegram: {'ok' if telegram_ok else 'fail'}\n"
        f"Google: {'ok' if google_ok else 'fail'}\n"
        f"Queue: {q['count']} item(s)"
    )
    if not telegram_ok and telegram_err:
        msg += f"\nTelegram err: {telegram_err[:120]}"
    if not google_ok and google_err:
        msg += f"\nGoogle err: {google_err[:120]}"
    if q.get("oldest"):
        msg += f"\nOldest: {q['oldest']}"

    await update.message.reply_text(msg)


async def queue_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = await queue_status()
    msg = f"Очередь: {q['count']} item(s)"
    if q.get("oldest"):
        msg += f"\nСамое старое: {q['oldest']}"
    await update.message.reply_text(msg)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    def fetch_stats():
        return post_to_sheets({"action": "stats"})

    data = await asyncio.to_thread(fetch_stats)

    if not data.get("ok"):
        await update.message.reply_text(
            "Не могу достучаться до Google. Попробуй позже."
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


# ---------- Callback buttons ----------
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    data = query.data or ""
    current_chat_id = query.message.chat_id

    if data == "next":
        await safe_edit_or_send(query, context, "Оцени покак:", reply_markup=keyboard_rate())
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
            await safe_edit_or_send(query, context, "Не понял реакцию. Попробуй ещё раз.")
            return

        await safe_edit_or_send(query, context, f"Отправил реакцию: {label}")
        await notify_others(context, current_chat_id, text)
        return

    if data == "anxiety":
        payload = user_payload(query.from_user, current_chat_id)
        payload.update({"anxiety": True, "event": "anxiety"})

        res = await send_or_queue(payload)

        if res.get("ok"):
            await safe_edit_or_send(query, context, "Записал: пукательная тревога ✅", reply_markup=keyboard_next())
        else:
            await safe_edit_or_send(
                query,
                context,
                "Записал: пукательная тревога ✅\nВ таблицу отправлю, когда появится связь.",
                reply_markup=keyboard_next(),
            )

        await notify_others(context, current_chat_id, "Случилась пукательная тревога!")
        return

    if data.startswith("score:"):
        score = int(data.split(":", 1)[1])
        if not (1 <= score <= 10):
            await safe_edit_or_send(query, context, "Оценка должна быть от 1 до 10.")
            return

        payload = user_payload(query.from_user, current_chat_id)
        payload.update({"score": score, "event": "score"})

        res = await send_or_queue(payload)

        if res.get("ok"):
            await safe_edit_or_send(query, context, f"Записал: {score}/10 ✅", reply_markup=keyboard_next())
        else:
            await safe_edit_or_send(
                query,
                context,
                f"Записал: {score}/10 ✅\nВ таблицу отправлю, когда появится связь.",
                reply_markup=keyboard_next(),
            )

        await notify_others(context, current_chat_id, f"Кое-кто покакал! Оценка: {score}")
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    current_chat_id = update.effective_chat.id

    if text.isdigit():
        score = int(text)
        if 1 <= score <= 10:
            payload = user_payload(update.effective_user, current_chat_id)
            payload.update({"score": score, "event": "score"})

            res = await send_or_queue(payload)

            if res.get("ok"):
                await update.message.reply_text(f"Записал: {score}/10 ✅", reply_markup=keyboard_next())
            else:
                await update.message.reply_text(
                    f"Записал: {score}/10 ✅\nВ таблицу отправлю, когда появится связь.",
                    reply_markup=keyboard_next(),
                )

            await notify_others(context, current_chat_id, f"Кое-кто покакал! Оценка: {score}")
            return

    await update.message.reply_text("Пришли число 1–10 или жми /start.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    ensure_data_dir()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("react", react))
    app.add_handler(CommandHandler("alarm_on", alarm_on))
    app.add_handler(CommandHandler("alarm_off", alarm_off))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("queue_status", queue_status_cmd))

    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ежедневное напоминание в 22:00 по Москве
    if app.job_queue:
        app.job_queue.run_daily(
            alarm_job,
            time=time(hour=22, minute=0, tzinfo=TZ),
            name="daily_alarm_22_msk",
        )
        # догонялка очереди каждые 5 минут
        app.job_queue.run_repeating(
            flush_job,
            interval=300,
            first=30,
            name="flush_queue_5min",
        )
    else:
        logging.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
