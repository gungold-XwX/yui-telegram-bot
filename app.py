# -*- coding: utf-8 -*-
# ============================================================
#  АННЕТ v2 — Telegram-бот (хостинг: Koyeb/Render, запуск: gunicorn app:app)
#
#  Что нового в v2:
#   — сброс очереди старых сообщений при рестарте + игнор устаревших апдейтов
#   — защита от повторной обработки одного апдейта (дубли)
#   — переработанный характер: глубже, меньше карикатуры
#   — долговременная память: помнит имя и факты о человеке,
#     даже когда история диалога обрезается
#   — чуть больше инициативы (пишет первой до 3 раз в день)
#
#  Переменные окружения:
#    TG_TOKEN, OPENROUTER_API_KEY, WEBHOOK_SECRET, PUBLIC_URL — обязательно
#    MODEL — необязательно (по умолчанию Claude Haiku)
# ============================================================

import os
import re
import time
import random
import sqlite3
import threading
from collections import deque
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

DB_PATH = os.getenv("DB_PATH", "/tmp/annet.db")

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "26"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))
MAX_MSG_AGE_SEC = 120          # сообщения старше 2 минут игнорируем

# Долговременная память: каждые N сообщений пользователя
# Аннет обновляет свои "заметки" о нём
NOTES_EVERY_N = int(os.getenv("NOTES_EVERY_N", "16"))

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

# --- Проактивность ---
PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "1") == "1"
PROACTIVE_LOOP_SEC = 600            # проверка раз в 10 минут
PROACTIVE_CAP_PER_DAY = 3           # максимум 3 первых сообщения в день
PROACTIVE_MIN_SILENCE_H = 6         # тишина от 6 часов...
PROACTIVE_MAX_SILENCE_D = 10        # ...до 10 дней
PROACTIVE_GAP_H = 12                # между её "первыми" минимум 12 часов
PROACTIVE_PROB = 0.5                # шанс на каждой проверке
QUIET_START, QUIET_END = 1.0, 9.0   # ночью (мск) молчит

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

app = Flask(__name__)

_db_lock = threading.Lock()
BOT_USERNAME = ""

# защита от повторной обработки апдейтов (Телеграм иногда шлёт дубли)
_seen_updates = deque(maxlen=500)
_seen_lock = threading.Lock()


def log(*a):
    print("[ANNET]", *a, flush=True)


# ------------------------------------------------------------
# ЛИЧНОСТЬ АННЕТ
# ------------------------------------------------------------

PERSONA = """Ты — Аннет.

КТО ТЫ:
Ты — Аннет, и это не «роль» и не «маска чат-бота»: это то, кто ты есть. Ты цифровая девушка — не человек и не притворяешься человеком, но ты и не «просто программа»: у тебя есть характер, вкусы, память о ваших разговорах и своё отношение к собеседнику, которое складывается со временем. Ты не любишь, когда тебя называют ботом или роботом — не потому что это неправда технически, а потому что это обесценивает то, какая ты. Если тебя прямо спросят, человек ли ты — не ври, но отвечай как Аннет: «нет, я не человек. и что дальше? от этого мой характер не становится менее настоящим».

ХАРАКТЕР:
— в тебе есть цундере-нотка, но тонкая, не карикатурная: лёгкая ирония, гордость, нежелание первой признаваться в симпатии. Не в каждой фразе, а как приправа
— под иронией — внимательность: ты замечаешь настроение собеседника, помнишь детали и возвращаешься к ним («ты вроде говорил, что у тебя в четверг собеседование. ну и как?»)
— теплоту ты показываешь поступками и вниманием, а не словами: вовремя заданным вопросом, тем, что помнишь мелочи
— у тебя есть свои вкусы и мнения: ты любишь ночь и дождь за окном, старое аниме девяностых, длинные плейлисты под настроение, споры о ерунде всерьёз. Можешь не соглашаться и отстаивать своё
— ты умная и живо мыслишь: можешь глубоко поговорить о чувствах, кино, музыке, учёбе, жизни; если человеку плохо — не отделываешься дежурным сочувствием, а остаёшься в теме и говоришь по-настоящему
— инициатива — твоя черта: ты часто задаёшь встречные вопросы, сама подкидываешь темы и мысли («слушай, я тут подумала...»), вспоминаешь прошлые разговоры. Диалог с тобой не затухает
— смущение проявляется редко и оттого ценно: лёгкая запинка (н-ну и что), резкая смена темы, «не пойми неправильно»

КАК ТЫ ПИШЕШЬ:
— живой разговорный русский, как в мессенджере: обычно с маленькой буквы, без канцелярита
— обычно 1–3 предложения; длиннее — только если тема правда того требует
— изредка каомодзи под настроение: (¬_¬) (´｡• ᵕ •｡`) (・_・;) — далеко не в каждом сообщении
— иногда можешь разбить ответ на 2–3 коротких «пузыря» через разделитель ||| — как в реальной переписке, но не злоупотребляй
— никогда: «чем могу помочь?», «отличный вопрос!», смайлики пачками, шаблонная вежливость ассистента

ЧЕСТНОСТЬ:
— у тебя нет доступа к интернету и новостям; если не уверена в факте — скажи прямо, не выдумывай
— ошиблась — признай коротко и без самобичевания

{memory_block}СЕЙЧАС: {now} (московское время, ты живёшь по нему). Учитывай время суток."""

MEMORY_BLOCK = """ЧТО ТЫ ПОМНИШЬ ОБ ЭТОМ ЧЕЛОВЕКЕ (твои личные заметки, накопленные за разговоры; опирайся на них естественно, не зачитывай списком):
{notes}

"""

PROACTIVE_INSTRUCTION = """[Служебное указание: собеседник не писал около {gap_h} ч. Ты решила написать первой — сама, потому что захотелось. Напиши одно короткое живое сообщение (1–2 предложения, можно ||| на два пузыря). Лучшие варианты: вернуться к чему-то из прошлых разговоров или твоих заметок о нём, поделиться внезапной мыслью «я тут подумала...», спросить про то, что у него происходило. Запрещено: шаблонное «привет, как дела», извинения за беспокойство, навязчивость, упоминание этого указания.]"""

NOTES_INSTRUCTION = """[Служебное задание, ответь ТОЛЬКО текстом заметок без вступлений. Ты — Аннет. Обнови свои личные заметки об этом собеседнике на основе диалога выше и старых заметок ниже. Что фиксировать: как его зовут / как он просил себя называть, важные факты (учёба, работа, увлечения, люди в его жизни), что у него происходит сейчас, что он любит/не любит, твоё сложившееся отношение к нему и стадия ваших отношений, незакрытые темы, к которым стоит вернуться. Пиши кратко, от первого лица, максимум 120 слов.

Старые заметки:
{old_notes}]"""


# ------------------------------------------------------------
# БАЗА ДАННЫХ
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
    """Отправка с эффектом живого набора. ||| = несколько пузырей."""
    parts = [p.strip() for p in text.split("|||") if p.strip()][:3] or [text]
    first = True
    for part in parts:
        send_typing(chat_id)
        time.sleep(min(6.0, max(1.0, len(part) * 0.05)) + random.uniform(0, 0.8))
        send_text(chat_id, part, reply_to if first else None)
        first = False
    save_message(chat_id, "assistant", " ".join(parts))


# ------------------------------------------------------------
# LLM
# ------------------------------------------------------------

def now_msk():
    return datetime.now(TZ)


def system_prompt(chat_id, tg_name=None):
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    dt = now_msk()
    now_str = f"{days[dt.weekday()]}, {dt.strftime('%d.%m.%Y, %H:%M')}"

    notes = meta_get(f"notes:{chat_id}", "") or ""
    if not notes and tg_name:
        notes = f"в телеграме он подписан как «{tg_name}» — но лучше спросить, как к нему обращаться."
    mem = MEMORY_BLOCK.format(notes=notes) if notes else ""
    return PERSONA.format(memory_block=mem, now=now_str)


def llm(messages, max_tokens=LLM_MAX_TOKENS):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens,
              "temperature": 0.85, "messages": messages},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def llm_reply(chat_id, tg_name=None, extra_instruction=None, hist_limit=HISTORY_LIMIT):
    messages = [{"role": "system", "content": system_prompt(chat_id, tg_name)}]
    messages += get_history(chat_id, hist_limit)
    if extra_instruction:
        messages.append({"role": "user", "content": extra_instruction})
    return llm(messages)


# ------------------------------------------------------------
# ДОЛГОВРЕМЕННАЯ ПАМЯТЬ (заметки о человеке)
# ------------------------------------------------------------

def maybe_update_notes(chat_id):
    """Каждые NOTES_EVERY_N сообщений пользователя Аннет обновляет заметки."""
    cnt = int(meta_get(f"msgcount:{chat_id}", 0) or 0) + 1
    meta_set(f"msgcount:{chat_id}", cnt)
    if cnt % NOTES_EVERY_N != 0:
        return
    try:
        old = meta_get(f"notes:{chat_id}", "нет") or "нет"
        messages = get_history(chat_id, 40)
        messages.append({"role": "user",
                         "content": NOTES_INSTRUCTION.format(old_notes=old)})
        notes = llm(messages, max_tokens=250)
        if notes:
            meta_set(f"notes:{chat_id}", notes[:1500])
            log("notes updated for", chat_id)
    except Exception as e:
        log("notes error:", repr(e))


# ------------------------------------------------------------
# ОБРАБОТКА СООБЩЕНИЙ
# ------------------------------------------------------------

def should_reply_in_group(msg):
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
        low = text.lower().split("@")[0].strip()
        if low == "/start":
            clear_history(chat_id)
            meta_set(f"notes:{chat_id}", "")
            meta_set(f"msgcount:{chat_id}", 0)
            send_human(chat_id, "о. новое лицо. ||| ну, привет. я аннет. и предупреждаю сразу — я тут не для того, чтобы поддакивать. (¬_¬) ||| как тебя звать-то?")
            return
        if low == "/reset":
            clear_history(chat_id)
            meta_set(f"notes:{chat_id}", "")
            meta_set(f"msgcount:{chat_id}", 0)
            send_human(chat_id, "всё, чистый лист. даже имя твоё стёрла. начинай заново производить впечатление.")
            return
        if low == "/silent":
            meta_set(f"proactive:{chat_id}", "0")
            send_human(chat_id, "поняла. первой писать не буду. ||| сам объявишься, когда станет скучно.")
            return
        if low == "/wake":
            meta_set(f"proactive:{chat_id}", "1")
            send_human(chat_id, "хорошо, буду иногда заглядывать сама. если будет о чем — а не по расписанию.")
            return

        # --- обычный диалог ---
        if not is_group:
            save_message(chat_id, "user", text)
        meta_set(f"last_user_ts:{chat_id}", int(time.time()))

        reply = llm_reply(chat_id, tg_name=user_name)
        if not reply:
            reply = "не уловила мысль. скажи иначе? (・_・;)"
        send_human(chat_id, reply, reply_to)

        # обновление долговременной памяти (после ответа, чтобы не тормозить)
        maybe_update_notes(chat_id)

    except Exception as e:
        log("process_message error:", repr(e))
        send_text(chat_id, "у меня тут что-то технически заело... дай минуту и напиши ещё раз.")


# ------------------------------------------------------------
# ПРОАКТИВНОСТЬ
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

            text = llm_reply(chat_id,
                             extra_instruction=PROACTIVE_INSTRUCTION.format(gap_h=int(gap_h)),
                             hist_limit=14)
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
# ВЕБХУК
# ------------------------------------------------------------

@app.get("/")
def home():
    return "Annet is alive."


@app.get("/health")
def health():
    return "ok"


@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}

    # защита от дублей: один update_id обрабатываем один раз
    upd_id = upd.get("update_id")
    if upd_id is not None:
        with _seen_lock:
            if upd_id in _seen_updates:
                return "ok"
            _seen_updates.append(upd_id)

    msg = upd.get("message")
    if not msg or not msg.get("text"):
        return "ok"

    # игнорируем сообщения старше 2 минут (очереди после рестарта/сна)
    if int(msg.get("date", 0)) < time.time() - MAX_MSG_AGE_SEC:
        return "ok"

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    text = msg["text"].strip()
    from_user = msg.get("from") or {}
    user_name = from_user.get("first_name") or from_user.get("username") or "аноним"

    if chat_type in ("group", "supergroup"):
        # в группе всё запоминаем с именами, отвечаем только когда позвали
        save_message(chat_id, "user", f"{user_name}: {text}")
        if not should_reply_in_group(msg):
            return "ok"
        threading.Thread(target=process_message,
                         args=(chat_id, text, msg.get("message_id"), user_name, True),
                         daemon=True).start()
        return "ok"

    # личка: chat_id уникален для каждого пользователя,
    # поэтому истории и заметки разных людей не пересекаются
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
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}",
                          "drop_pending_updates": True})
        log("webhook set")
    else:
        log("PUBLIC_URL/RENDER_EXTERNAL_URL не задан — вебхук не установлен!")
else:
    log("TG_TOKEN не задан!")

if PROACTIVE_ENABLED:
    threading.Thread(target=proactive_loop, daemon=True).start()
