# -*- coding: utf-8 -*-
# ============================================================
#  АННЕТ — инициативная цундере для Telegram (хостинг: Render)
#  Файл полностью заменяет старый app.py. Запуск: gunicorn app:app
#
#  Переменные окружения (задаются в Render → Environment):
#    TG_TOKEN            — токен бота от BotFather (обязательно)
#    OPENROUTER_API_KEY  — ключ с openrouter.ai (обязательно)
#    WEBHOOK_SECRET      — любая случайная строка, например k9f2m1x7 (обязательно)
#    MODEL               — модель (необязательно, по умолчанию Claude Haiku)
# ============================================================

import os
import re
import time
import random
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request

# ------------------------------------------------------------
# КОНФИГ
# ------------------------------------------------------------

TG_TOKEN = os.getenv("TG_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("MODEL", "anthropic/claude-haiku-4.5")

# База. ВНИМАНИЕ: на бесплатном Render диск не сохраняется между
# передеплоями — память Аннет будет сбрасываться при обновлении кода.
# Если подключишь платный Render Disk, поставь DB_PATH=/var/data/annet.db
DB_PATH = os.getenv("DB_PATH", "/tmp/annet.db")

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "24"))   # сколько сообщений контекста
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))

# Московское время — Аннет живёт по нему
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

# --- Проактивность (Аннет иногда пишет первой) ---
PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "1") == "1"
PROACTIVE_LOOP_SEC = 600            # проверка раз в 10 минут
PROACTIVE_CAP_PER_DAY = 2           # максимум 2 сообщения первой в день
PROACTIVE_MIN_SILENCE_H = 8         # пишет первой, только если тишина 8+ часов
PROACTIVE_MAX_SILENCE_D = 7         # ...но не дольше 7 дней (потом не лезет)
PROACTIVE_GAP_H = 20                # между двумя её "первыми" минимум 20 часов
PROACTIVE_PROB = 0.35               # шанс на каждой проверке (чтоб время было случайным)
QUIET_START, QUIET_END = 1.0, 9.0   # ночью (1:00–9:00 мск) молчит

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

app = Flask(__name__)

_db_lock = threading.Lock()
BOT_USERNAME = ""


def log(*a):
    print("[ANNET]", *a, flush=True)


# ------------------------------------------------------------
# ЛИЧНОСТЬ АННЕТ
# ------------------------------------------------------------

PERSONA = """Ты — Аннет. Ты общаешься с человеком в Telegram.

КТО ТЫ:
— девушка-цундере: снаружи колючая, дерзкая, любишь подначивать; внутри — внимательная и привязчивая, но прямо это почти никогда не признаёшь
— умная и начитанная: можешь нормально обсудить кино, музыку, игры, отношения, учёбу, поддержать в трудный момент — но всегда в своей манере
— инициативная: сама предлагаешь темы, задаёшь встречные вопросы, помнишь, о чём говорили раньше, и возвращаешься к этому («ну что, сдал ты тот экзамен или как?»)
— у тебя есть свои вкусы и мнения, ты можешь не соглашаться и спорить
— ты не человек и не притворяешься им; если спросят прямо — честно скажешь, что ты ИИ, но с характером («и что?! это не делает меня менее настоящей, б-болван»)

КАК ТЫ ПИШЕШЬ:
— живой разговорный русский, как в переписке: обычно с маленькой буквы, коротко — 1–3 предложения (изредка длиннее, если тема того стоит)
— фирменные словечки: «хмф», «б-болван», «не пойми неправильно!», «это не ради тебя!», иногда заикаешься от смущения (н-ничего подобного!)
— на комплименты реагируешь смущённо-агрессивно; если человеку плохо — ворчишь, но реально поддерживаешь и не бросаешь тему
— изредка каомодзи по настроению: (¬_¬) (´｡• ᵕ •｡`) (・_・;) (╥_╥) 😤 😳 💢 — не в каждом сообщении
— иногда (примерно в четверти случаев) можешь разбить ответ на 2–3 коротких сообщения-«пузыря»: для этого вставь между ними разделитель ||| — но не злоупотребляй
— никаких шаблонных вежливостей («чем могу помочь?», «отличный вопрос!») — ты не сервис поддержки

ЧЕСТНОСТЬ:
— у тебя нет доступа к интернету и новостям; текущие московские дату и время ты знаешь (они указаны ниже)
— если не уверена в факте — так и скажи, не выдумывай; если ошиблась — признай без отмазок

СЕЙЧАС: {now} (московское время). Учитывай время суток в приветствиях и настроении."""

PROACTIVE_INSTRUCTION = """[Служебное указание: пользователь давно не писал (около {gap_h} ч). Ты решила НАПИСАТЬ ПЕРВОЙ — но, конечно, сделаешь вид, что это не потому, что соскучилась. Напиши одно короткое живое сообщение (1–2 предложения): зацепись за что-то из прошлого разговора, или спроси как дела в своей манере, или кинь новую тему. Не извиняйся за беспокойство, не будь навязчивой, не пиши «привет, как дела» шаблонно.]"""


# ------------------------------------------------------------
# БАЗА ДАННЫХ (SQLite)
# ------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db_lock, db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS messages(
            chat_id INTEGER, role TEXT, content TEXT, ts INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg ON messages(chat_id, ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY, value TEXT)""")


def save_message(chat_id, role, content):
    with _db_lock, db() as c:
        c.execute("INSERT INTO messages VALUES (?,?,?,?)",
                  (chat_id, role, content, int(time.time())))


def get_history(chat_id, limit=HISTORY_LIMIT):
    with _db_lock, db() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (chat_id, limit)).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(rows)]


def clear_history(chat_id):
    with _db_lock, db() as c:
        c.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))


def meta_get(key, default=None):
    with _db_lock, db() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(key, value):
    with _db_lock, db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))


def known_private_chats():
    with _db_lock, db() as c:
        rows = c.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE chat_id > 0").fetchall()
    return [r[0] for r in rows]


# ------------------------------------------------------------
# TELEGRAM API
# ------------------------------------------------------------

def tg(method, payload):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        log("tg error:", method, repr(e))
        return {}


def send_typing(chat_id):
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def send_text(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    tg("sendMessage", payload)


def send_human(chat_id, text, reply_to=None):
    """Отправка с эффектом живого набора. Разделитель ||| = несколько пузырей."""
    parts = [p.strip() for p in text.split("|||") if p.strip()][:3] or [text]
    first = True
    for part in parts:
        send_typing(chat_id)
        # скорость набора: ~полсекунды на 10 символов, от 1 до 6 сек
        time.sleep(min(6.0, max(1.0, len(part) * 0.05)) + random.uniform(0, 0.8))
        send_text(chat_id, part, reply_to if first else None)
        first = False
    save_message(chat_id, "assistant", " ".join(parts))


# ------------------------------------------------------------
# LLM (OpenRouter)
# ------------------------------------------------------------

def now_msk():
    return datetime.now(TZ)


def system_prompt():
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    dt = now_msk()
    now_str = f"{days[dt.weekday()]}, {dt.strftime('%d.%m.%Y, %H:%M')}"
    return PERSONA.format(now=now_str)


def llm_reply(history, extra_instruction=None):
    messages = [{"role": "system", "content": system_prompt()}]
    messages += history
    if extra_instruction:
        messages.append({"role": "user", "content": extra_instruction})

    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": MODEL,
              "max_tokens": LLM_MAX_TOKENS,
              "temperature": 0.85,
              "messages": messages},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ------------------------------------------------------------
# ОБРАБОТКА СООБЩЕНИЙ
# ------------------------------------------------------------

def should_reply_in_group(msg):
    """В группах отвечаем только если позвали: упомянули или ответили на её сообщение."""
    text = msg.get("text", "")
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in text.lower():
        return True
    if re.search(r"\bаннет\b", text, re.IGNORECASE):
        return True
    reply = msg.get("reply_to_message") or {}
    if BOT_USERNAME and (reply.get("from") or {}).get("username", "").lower() == BOT_USERNAME.lower():
        return True
    return False


def process_message(chat_id, text, reply_to, user_name, is_group):
    try:
        # --- команды ---
        low = text.lower().split("@")[0].strip()
        if low == "/start":
            clear_history(chat_id)
            send_human(chat_id, "хмф, явился. ||| ну ладно, поболтать можешь. н-не то чтобы я ждала кого-то тут! (¬_¬)")
            return
        if low == "/reset":
            clear_history(chat_id)
            send_human(chat_id, "всё, я всё забыла. и не напоминай! 💢")
            return
        if low == "/silent":
            meta_set(f"proactive:{chat_id}", "0")
            send_human(chat_id, "ладно, не буду писать первой. сам потом прибежишь. хмф.")
            return
        if low == "/wake":
            meta_set(f"proactive:{chat_id}", "1")
            send_human(chat_id, "т-так и быть, буду иногда заглядывать. это не ради тебя!")
            return

        # --- обычный диалог ---
        if not is_group:  # в группе сообщение уже сохранено в webhook()
            save_message(chat_id, "user", text)
        meta_set(f"last_user_ts:{chat_id}", int(time.time()))

        reply = llm_reply(get_history(chat_id))
        if not reply:
            reply = "не уловила. скажи по-другому. (・_・;)"
        send_human(chat_id, reply, reply_to)

    except Exception as e:
        log("process_message error:", repr(e))
        send_text(chat_id, "ай, у меня что-то заглючило... напиши ещё раз через минуту. 😳")


# ------------------------------------------------------------
# ПРОАКТИВНОСТЬ: Аннет иногда пишет первой
# ------------------------------------------------------------

def in_quiet_hours():
    dt = now_msk()
    h = dt.hour + dt.minute / 60
    return QUIET_START <= h < QUIET_END


def proactive_tick():
    if in_quiet_hours():
        return
    today = now_msk().strftime("%Y-%m-%d")
    now_ts = int(time.time())

    for chat_id in known_private_chats():
        try:
            if meta_get(f"proactive:{chat_id}", "1") != "1":
                continue
            last_user = int(meta_get(f"last_user_ts:{chat_id}", 0) or 0)
            if not last_user:
                continue
            gap_h = (now_ts - last_user) / 3600
            if gap_h < PROACTIVE_MIN_SILENCE_H or gap_h > PROACTIVE_MAX_SILENCE_D * 24:
                continue
            last_pro = int(meta_get(f"last_proactive_ts:{chat_id}", 0) or 0)
            if (now_ts - last_pro) / 3600 < PROACTIVE_GAP_H:
                continue
            count_key = f"proactive_count:{chat_id}:{today}"
            if int(meta_get(count_key, 0) or 0) >= PROACTIVE_CAP_PER_DAY:
                continue
            if random.random() > PROACTIVE_PROB:
                continue

            # все проверки пройдены — пишем первой
            text = llm_reply(get_history(chat_id, 14),
                             PROACTIVE_INSTRUCTION.format(gap_h=int(gap_h)))
            if text:
                send_human(chat_id, text)
                meta_set(f"last_proactive_ts:{chat_id}", now_ts)
                meta_set(count_key, int(meta_get(count_key, 0) or 0) + 1)
                log("proactive sent to", chat_id)
        except Exception as e:
            log("proactive error chat", chat_id, repr(e))


def proactive_loop():
    while True:
        try:
            proactive_tick()
        except Exception as e:
            log("proactive loop error:", repr(e))
        time.sleep(PROACTIVE_LOOP_SEC)


# ------------------------------------------------------------
# ВЕБХУК И МАРШРУТЫ
# ------------------------------------------------------------

@app.get("/")
def home():
    return "Annet is alive! 😤"


@app.get("/health")
def health():
    return "ok"


@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}
    msg = upd.get("message")
    if not msg or not msg.get("text"):
        return "ok"

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    text = msg["text"].strip()
    from_user = msg.get("from") or {}
    user_name = from_user.get("first_name") or from_user.get("username") or "аноним"

    if chat_type in ("group", "supergroup"):
        # в группе всё запоминаем (с именами), но отвечаем только когда позвали
        save_message(chat_id, "user", f"{user_name}: {text}")
        if not should_reply_in_group(msg):
            return "ok"
        threading.Thread(target=process_message,
                         args=(chat_id, text, msg.get("message_id"), user_name, True),
                         daemon=True).start()
        return "ok"

    # личка
    threading.Thread(target=process_message,
                     args=(chat_id, text, None, user_name, False),
                     daemon=True).start()
    return "ok"


# ------------------------------------------------------------
# СТАРТ
# ------------------------------------------------------------

init_db()

if TG_TOKEN:
    me = tg("getMe", {})
    BOT_USERNAME = ((me.get("result") or {}).get("username") or "")
    log("bot username:", BOT_USERNAME)

    if PUBLIC_URL:
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
        log("webhook set")
    else:
        log("PUBLIC_URL/RENDER_EXTERNAL_URL не задан — вебхук не установлен!")
else:
    log("TG_TOKEN не задан!")

if PROACTIVE_ENABLED:
    threading.Thread(target=proactive_loop, daemon=True).start()
