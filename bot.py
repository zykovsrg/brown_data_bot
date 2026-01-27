import os
import json
import logging
import asyncio
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
import html

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
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
    # 5 строк, по одной кнопке
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💛 Радость", callback_data="react:joy")],
        [InlineKeyboardButton("🤍 Белая зависть", callback_data="react:white_envy")],
        [InlineKeyboardButton("🖤 Чёрная зависть", callback_data="react:black_envy")],
        [InlineKeyboardButton("💜 Сочувствие", callback_data="react:empathy")],
        [InlineKeyboardButton("💩 Злорадство", callback_data="react:schadenfreude")],
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


async def remove_reply_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    # Убирает ReplyKeyboardMarkup (те две большие кнопки снизу)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=" ",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass


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


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def _rewrite_queue(path: str, items: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


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
    oldest = items[0].get("timestamp") if count else None
    return {"count": count, "oldest": oldest}


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
                continue

        if not items:
            return {"ok": True, "sent": 0, "left": 0}

        sent = 0
        remaining: list[dict] = []

        for idx, payload in enumerate(items):
            res = await asyncio.to_thread(post_to_sheets, payload)
            if res.get("ok"):
                sent += 1
                continue

            remaining = items[idx:]
            break

        await asyncio.to_thread(_rewrite_queue, QUEUE_PATH, remaining)

    return {"ok": True, "sent": sent, "left": len(remaining)}


async def send_or_queue(event_payload: dict) -> dict:
    """
    1) сначала пытаемся догнать старое
    2) потом отправить текущее
    3) если не получилось — кладём текущее в очередь
    """
    await flush_queue_once()

    res = await asyncio.to_thread(post_to_sheets, event_payload)
    if res.get("ok"):
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
    await remove_reply_keyboard(context, update.effective_chat.id)
    await update.message.reply_text("Я на связи.\nОценка: /pokak\nСтатистика: /stats\nРеакции: /react")


async def pokak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await remove_reply_keyboard(context, update.effective_chat.id)
    await update.message.reply_text("Оц
