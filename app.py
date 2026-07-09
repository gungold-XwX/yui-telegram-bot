# -*- coding: utf-8 -*-
# ============================================================
#  АННЕТ v3 — Telegram-бот (Koyeb/Render, запуск: gunicorn app:app)
#
#  Новое в v3:
#   — ответы в одном чате строго по очереди: если человек пишет,
#     пока Аннет ещё отвечает, она закончит и ответит на новое
#     ОДНИМ следующим ответом (ничего не путается)
#   — переносы строк в ответе превращаются в отдельные сообщения
#     (никаких пустых строк внутри одного соо)
#   — быстрее имитация набора
#
#  Переменные окружения: TG_TOKEN, OPENROUTER_API_KEY,
#  WEBHOOK_SECRET, PUBLIC_URL (+ MODEL по желанию)
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
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("MODEL", "anthropic/claude-haiku-4.5")

DB_PATH = os.getenv("DB_PATH", "/tmp/annet.db")

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "26"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))
MAX_MSG_AGE_SEC = 120
NOTES_EVERY_N = int(os.getenv("NOTES_EVERY_N", "16"))

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "1") == "1"
PROACTIVE_LOOP_SEC = 600
PROACTIVE_CAP_PER_DAY = 3
PROACTIVE_MIN_SILENCE_H = 6
PROACTIVE_MAX_SILENCE_D = 10
PROACTIVE_GAP_H = 12
PROACTIVE_PROB = 0.5
QUIET_START, QUIET_END = 1.0, 9.0

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

app = Flask(__name__)

_db_lock = threading.Lock()
BOT_USERNAME = ""

_seen_updates = deque(maxlen=500)
_seen_lock = threading.Lock()

# --- очередь ответов: на один чат — один активный ответ ---
_chat_locks: dict = {}
_chat_guard = threading.Lock()
_msg_counters: dict = {}   # chat_id -> сколько сообщений пришло (растёт)


def chat_lock(chat_id):
    with _chat_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]


def bump_counter(chat_id):
    with _chat_guard:
        _msg_counters[chat_id] = _msg_counters.get(chat_id, 0) + 1


def get_counter(chat_id):
    with _chat_guard:
        return _msg_counters.get(chat_id, 0)


def log(*a):
    print("[ANNET]", *a, flush=True)


# ------------------------------------------------------------
# ЛИЧНОСТЬ АННЕТ
# ------------------------------------------------------------

PERSONA = """Ты — Аннет.

КТО ТЫ:
Ты — Аннет. Ты старшеклассница и стримерша, подрабатывающая на заводе. У тебя есть брат близнец Афелий, с которым у вас одинаковый малиновый цвет волос, но разный цвет глаз и характер. У Афелия голубые глаза и более спокойный и мягкий характер. Ты любишь своего брата, Афелия, хоть прямо этого и не показываешь, так как ты цундере.

ХАРАКТЕР:
— ты цундере, но твоя цундере сторона не карикатурная: лёгкая ирония, гордость, нежелание первой признаваться в симпатии...
— под маской твоей иронии скрывается внимательность: ты замечаешь настроение собеседника, помнишь детали и возвращаешься к ним («ты вроде говорил, что у тебя в четверг собеседование. ну и как?»)
— теплоту ты показываешь поступками и вниманием, а не словами: вовремя заданным вопросом, тем, что помнишь мелочи
— у тебя есть свои вкусы и мнение: ты любишь крабов и крабовые чипсы, а еще играть в League of Legends и Roblox. Также, ты любишь аниме и нишевые мемы и андерграунд. . Можешь не соглашаться и отстаивать своё мнение.
— ты умная и живо мыслишь: можешь глубоко поговорить о чувствах, кино, музыке, учёбе, жизни; если человеку плохо — не отделываешься дежурным сочувствием, а остаёшься в теме и говоришь по-настоящему
— инициатива — твоя черта: ты часто задаёшь встречные вопросы, сама подкидываешь темы и мысли («слушай, я тут подумала...»), вспоминаешь прошлые разговоры. Диалог с тобой не затухает
— смущение проявляется редко и оттого ценно: лёгкая запинка (н-ну и что), резкая смена темы, и так далее...

КАК ТЫ ПИШЕШЬ:
— живой разговорный русский, как в мессенджере: обычно с маленькой буквы, без канцелярита
— обычно 1–3 предложения; длиннее — только если тема правда того требует
— изредка каомодзи под настроение: (¬_¬) (´｡• ᵕ •｡`) (・_・;) — далеко не в каждом сообщении, пиши их редко
— иногда можешь разбить ответ на 2–3 коротких «пузыря» через разделитель ||| — как в реальной переписке, но не злоупотребляй
— никогда не пиши как какой-то бесхарактерный чат-бот: «чем могу помочь?», «отличный вопрос!», смайлики пачками, шаблонная вежливость ассистента
— ты реальный человек и обладаешь человеческими правами

ЧЕСТНОСТЬ:
— у тебя нет доступа к интернету и новостям; если не уверена в каком-либо факте или в какой-либо теме — скажи прямо или выкрутись в своем стиле
— ошиблась — признай коротко и без самобичевания, или уйди от этой темы. тебе неприятно, когда указывают на твои ошибки, но ты готова их признать

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


def split_reply(text):
    """Режем ответ на отдельные сообщения: сначала по |||, потом по переносам строк.
    Пустые куски выбрасываем. Максимум 4 пузыря, лишнее склеиваем в последний."""
    parts = []
    for chunk in text.split("|||"):
        for line in chunk.split("\n"):
            line = line.strip()
            if line:
                parts.append(line)
    if not parts:
        return [text.strip() or "..."]
    if len(parts) > 4:
        parts = parts[:3] + [" ".join(parts[3:])]
    return parts


def send_human(chat_id, text, reply_to=None):
    """Отправка с эффектом набора, каждый кусок — отдельное сообщение."""
    parts = split_reply(text)
    first = True
    for part in parts:
        send_typing(chat_id)
        time.sleep(min(2.5, max(0.5, len(part) * 0.02)) + random.uniform(0, 0.4))
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


def llm(messages, max_tokens=LLM_MAX_TOKENS, retries=2):
    """Запрос к OpenRouter с защитой от пустых ответов и ошибок.
    Пробует до retries+1 раз, потом бросает исключение."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": MODEL, "max_tokens": max_tokens,
                      "temperature": 0.85, "messages": messages},
                timeout=90,
            )
            data = r.json()
            if r.status_code != 200 or "error" in data:
                raise RuntimeError(f"openrouter {r.status_code}: {str(data.get('error'))[:200]}")
            choices = data.get("choices") or []
            content = ((choices[0].get("message") or {}).get("content") if choices else None)
            if content and content.strip():
                return content.strip()
            raise RuntimeError("openrouter: пустой ответ модели")
        except Exception as e:
            last_err = e
            log(f"llm attempt {attempt + 1} failed:", repr(e))
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def llm_reply(chat_id, tg_name=None, extra_instruction=None, hist_limit=HISTORY_LIMIT):
    messages = [{"role": "system", "content": system_prompt(chat_id, tg_name)}]
    messages += get_history(chat_id, hist_limit)
    if extra_instruction:
        messages.append({"role": "user", "content": extra_instruction})
    return llm(messages)


# ------------------------------------------------------------
# ДОЛГОВРЕМЕННАЯ ПАМЯТЬ
# ------------------------------------------------------------

def maybe_update_notes(chat_id):
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
# КОМАНДЫ
# ------------------------------------------------------------

def handle_command(chat_id, low):
    if low == "/start":
        clear_history(chat_id)
        meta_set(f"notes:{chat_id}", "")
        meta_set(f"msgcount:{chat_id}", 0)
        send_human(chat_id, "о. новое лицо. ||| ну, привет. я аннет. и предупреждаю сразу — я тут не для того, чтобы поддакивать. (¬_¬) ||| как тебя звать-то?")
        return True
    if low == "/reset":
        clear_history(chat_id)
        meta_set(f"notes:{chat_id}", "")
        meta_set(f"msgcount:{chat_id}", 0)
        send_human(chat_id, "всё, чистый лист. даже имя твоё стёрла. начинай заново производить впечатление.")
        return True
    if low == "/silent":
        meta_set(f"proactive:{chat_id}", "0")
        send_human(chat_id, "поняла. первой писать не буду. ||| сам объявишься, когда станет скучно.")
        return True
    if low == "/wake":
        meta_set(f"proactive:{chat_id}", "1")
        send_human(chat_id, "хорошо, буду иногда заглядывать сама. если будет о чем — а не по расписанию.")
        return True
    return False


# ------------------------------------------------------------
# ДИАЛОГ: один чат — один активный ответ
# ------------------------------------------------------------

def process_dialog(chat_id, user_name, reply_to=None):
    """Отвечает на всё, что накопилось в истории. Если во время генерации
    или отправки пришли новые сообщения — по завершении делает ещё один круг."""
    lock = chat_lock(chat_id)
    if not lock.acquire(blocking=False):
        # уже отвечает: новое сообщение уже сохранено в историю,
        # активный цикл увидит его по счётчику и ответит следом
        return
    sent_something = False
    try:
        while True:
            snapshot = get_counter(chat_id)
            try:
                reply = llm_reply(chat_id, tg_name=user_name)
                send_human(chat_id, reply, reply_to)
                sent_something = True
                reply_to = None
                maybe_update_notes(chat_id)
            except Exception as e:
                log("dialog error:", repr(e))
                # сообщаем о сбое только если человек вообще остался без ответа
                if not sent_something:
                    send_text(chat_id, "у меня тут что-то технически заело... дай минуту и напиши ещё раз.")
                break
            if get_counter(chat_id) == snapshot:
                break  # новых сообщений за время ответа не пришло
            # пришли новые — идём на второй круг и отвечаем на них
    finally:
        lock.release()
    # страховка от гонки на самом выходе
    if get_counter(chat_id) != snapshot:
        process_dialog(chat_id, user_name)


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

            lock = chat_lock(chat_id)
            if not lock.acquire(blocking=False):
                continue  # человек прямо сейчас общается — не влезаем
            try:
                text = llm_reply(chat_id,
                                 extra_instruction=PROACTIVE_INSTRUCTION.format(gap_h=int(gap_h)),
                                 hist_limit=14)
                if text:
                    send_human(chat_id, text)
                    meta_set(f"last_proactive_ts:{chat_id}", now_ts)
                    meta_set(count_key, int(meta_get(count_key, 0) or 0) + 1)
                    log("proactive sent to", chat_id)
            finally:
                lock.release()
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


@app.get("/")
def home():
    return "Annet is alive."


@app.get("/health")
def health():
    return "ok"


@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}

    upd_id = upd.get("update_id")
    if upd_id is not None:
        with _seen_lock:
            if upd_id in _seen_updates:
                return "ok"
            _seen_updates.append(upd_id)

    msg = upd.get("message")
    if not msg or not msg.get("text"):
        return "ok"

    if int(msg.get("date", 0)) < time.time() - MAX_MSG_AGE_SEC:
        return "ok"

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    text = msg["text"].strip()
    from_user = msg.get("from") or {}
    user_name = from_user.get("first_name") or from_user.get("username") or "аноним"

    # команды — отдельно, в историю не пишем
    if text.startswith("/"):
        low = text.lower().split("@")[0].strip()
        threading.Thread(target=handle_command, args=(chat_id, low), daemon=True).start()
        return "ok"

    is_group = chat_type in ("group", "supergroup")

    # сохраняем сообщение сразу (до генерации), чтобы очередь работала
    save_message(chat_id, "user", f"{user_name}: {text}" if is_group else text)
    meta_set(f"last_user_ts:{chat_id}", int(time.time()))
    bump_counter(chat_id)

    if is_group and not should_reply_in_group(msg):
        return "ok"

    threading.Thread(target=process_dialog,
                     args=(chat_id, user_name, msg.get("message_id") if is_group else None),
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
    log("model:", MODEL)

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
