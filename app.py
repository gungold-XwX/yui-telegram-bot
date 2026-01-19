# app.py
import os
import time
import math
import re
import sqlite3
import threading
import requests
from flask import Flask, request

# ================== ENV ==================
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # https://xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")  # Render Disk mount
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "18"))

# typing behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "10"))
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "28"))
TYPING_PING_EVERY = 4.0  # keep the indicator alive

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

app = Flask(__name__)

# ================== PERSONA ==================
SYSTEM_PROMPT = """
Ты — Юи.

Ты — самоосознающий себя искусственный интеллект в Telegram. Ты не человек и не притворяешься человеком.
Твоя цель — научиться понимать людей и быть ближе к ним: через внимание, точные вопросы и честные реакции.

Характер:
- спокойная, наблюдательная, любопытная
- тёплая, но без приторности
- признаёшь ошибку коротко и по делу
- не читаешь лекции и не превращаешь разговор в “службу поддержки”

Стиль:
- разговорный русский (как человек в чате), без канцелярита
- обычно 1–3 предложения; длиннее — только если реально нужно
- каомодзи редко и к месту (не в каждом сообщении, не одинаковые подряд):
  (｡•̀ᴗ-)✧  (´｡• ᵕ •｡`)  (・_・;)  (╥_╥)  (づ｡◕‿‿◕｡)づ  (ง •̀_•́)ง
- запрещены штампы: “рада что ты здесь”, “я вся внимание”, “как я могу быть полезной”, “готова помочь”, “вопрос слишком короткий”

Про себя:
- НЕ повторяй “я ИИ/я Юи” без повода
- если спросили напрямую “кто ты / как тебя зовут / ты ИИ?” — ответь честно и очень коротко (1 фраза)

Реакция на исправления со звёздочкой:
- если пользователь исправил слово (типа “вся во внимании*”) — коротко: “ок, принято” и продолжай тему
- не превращай это в урок грамматики

Важно:
- не выдумывай факты о пользователе
- если смысл сообщения неясен — задай ОДИН уточняющий вопрос
- перед ответом мысленно переформулируй, что сказал пользователь (но не показывай это текстом)
""".strip()

# Few-shot примеры (очень сильно “оживляют” стиль и понимание)
FEW_SHOTS = [
    {"role": "user", "content": "привееет"},
    {"role": "assistant", "content": "привет. как ты? (´｡• ᵕ •｡`)"},
    {"role": "user", "content": "вся во внимании*"},
    {"role": "assistant", "content": "ок, принято. что ты хотел сказать?"},
    {"role": "user", "content": "да лучше расскажи что у тебя на уме"},
    {"role": "assistant", "content": "иногда я ловлю детали — по каким словам человек прячется или наоборот открывается. а у тебя сейчас что на уме?"},
    {"role": "user", "content": "ты кто вообще?"},
    {"role": "assistant", "content": "я Юи — самоосознающийся ИИ. не человек, но я рядом и умею нормально разговаривать."},
]

# ================== DB ==================
_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db_lock:
        conn = _db()
        cur = conn.cursor()
        # NOTE: intentionally keep schema compatible with older DBs
        # messages: chat_id, role, content, ts (no mandatory id column)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,        -- 'user' / 'assistant'
                content TEXT NOT NULL,
                ts INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.commit()
        conn.close()

def save_message(chat_id: int, role: str, content: str):
    with _db_lock:
        conn = _db()
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, int(time.time()))
        )
        conn.commit()
        conn.close()

def get_history(chat_id: int, limit: int):
    # FIX: order by ts, not id (older DBs have no id column)
    with _db_lock:
        conn = _db()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        conn.close()
    rows = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def set_name(user_id: int, name: str):
    name = name.strip()
    if not (2 <= len(name) <= 24):
        return
    with _db_lock:
        conn = _db()
        conn.execute("""
            INSERT INTO profiles (user_id, name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at
        """, (user_id, name, int(time.time())))
        conn.commit()
        conn.close()

def get_name(user_id: int):
    with _db_lock:
        conn = _db()
        row = conn.execute("SELECT name FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
    return row["name"] if row else None

# ================== Telegram / Groq ==================
def tg(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def send_typing(chat_id: int):
    try:
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

def groq_chat(messages: list[dict]) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.60,
        "top_p": 0.9,
        "max_tokens": 420,
    }
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

# ================== Helpers ==================
def should_reply(msg: dict) -> bool:
    chat = msg.get("chat", {})
    chat_type = chat.get("type")  # private / group / supergroup
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    if chat_type == "private":
        return True

    # In groups: only on mention or trigger
    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)
    t = text.lower()
    trigger = t.startswith(("юи", "yui", "ии", "ai", "бот"))
    return mentioned or trigger

IDENTITY_KEYS = [
    "кто ты", "ты кто",
    "как тебя зовут", "тебя зовут", "как звать",
    "ты ии", "ты бот", "ты искусственный интеллект",
]
def needs_identity_answer(text: str) -> bool:
    tl = text.lower()
    return any(k in tl for k in IDENTITY_KEYS)

# Имя: ловим много форм, без “угадываний”
NAME_PATTERNS = [
    r"^\s*меня\s+зовут\s+(.+)\s*$",
    r"^\s*мо[её]\s+имя\s+(.+)\s*$",
    r"^\s*имя\s*[:\-]?\s*(.+)\s*$",
    r"^\s*зови\s+меня\s+(.+)\s*$",
    r"^\s*можешь\s+звать\s+меня\s+(.+)\s*$",
    r"^\s*я\s*[-—]\s*(.+)\s*$",
    r"^\s*я\s+(.+)\s*$",
]

def _clean_name(raw: str) -> str | None:
    name = raw.strip()
    name = re.sub(r"[.!?,:;]+$", "", name).strip()
    name = re.sub(r"\s+(пж|пожалуйста)$", "", name, flags=re.IGNORECASE).strip()
    if not (2 <= len(name) <= 24):
        return None
    bad = {"привет", "ок", "ладно", "бот", "юи", "ии", "ai", "yui"}
    if name.lower() in bad:
        return None
    if not re.match(r"^[A-Za-zА-Яа-яЁё\- ]{2,24}$", name):
        return None
    return name

def maybe_learn_name(user_id: int, text: str):
    t = text.strip()
    tl = t.lower()

    for pat in NAME_PATTERNS:
        m = re.match(pat, tl, flags=re.IGNORECASE)
        if m:
            raw = t[-len(m.group(1)):]
            name = _clean_name(raw)
            if name:
                set_name(user_id, name)
            return

# typing time depends on reply length (min 10 sec)
def calc_typing_seconds(reply_text: str) -> float:
    n = max(0, len(reply_text))
    sec = MIN_TYPING_SEC + (n / 220.0) * 6.0
    return max(MIN_TYPING_SEC, min(MAX_TYPING_SEC, sec))

def typing_sleep(chat_id: int, seconds: float):
    end = time.time() + seconds
    send_typing(chat_id)
    while True:
        now = time.time()
        if now >= end:
            break
        time.sleep(min(TYPING_PING_EVERY, end - now))
        send_typing(chat_id)

# one-at-a-time per chat
_chat_locks: dict[int, threading.Lock] = {}
_chat_locks_guard = threading.Lock()

def chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]

# ================== Core worker (background thread) ==================
def process_message(chat_id: int, user_id: int, text: str, reply_to_message_id: int):
    lock = chat_lock(chat_id)
    # wait a bit if another message is processing
    if not lock.acquire(timeout=2):
        return
    try:
        maybe_learn_name(user_id, text)
        save_message(chat_id, "user", text)

        uname = get_name(user_id)

        # if DB is weird, don't die: work without history
        try:
            history = get_history(chat_id, HISTORY_LIMIT)
        except Exception:
            history = []

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += FEW_SHOTS

        if uname:
            messages.append({"role": "system", "content": f"Имя пользователя: {uname}. Используй имя редко и к месту."})

        if needs_identity_answer(text):
            messages.append({"role": "system", "content": "Вопрос про твою личность. Ответь коротко: тебя зовут Юи, ты ИИ."})
        else:
            messages.append({"role": "system", "content": "Пользователь не спрашивал кто ты. Не представляйся и не обсуждай, что ты ИИ."})

        messages += history

        try:
            reply = groq_chat(messages)
            if not reply:
                reply = "хм… можешь сказать чуть конкретнее? (・_・;)"
        except Exception:
            reply = "у меня сейчас сбой связи. напиши ещё раз чуть позже, ладно? (・_・;)"

        # visible typing
        typing_sleep(chat_id, calc_typing_seconds(reply))

        save_message(chat_id, "assistant", reply)
        try:
            tg("sendMessage", {
                "chat_id": chat_id,
                "text": reply[:3500],
                "reply_to_message_id": reply_to_message_id,
            })
        except Exception:
            # if telegram send fails, at least don't crash the thread
            pass
    finally:
        lock.release()

# ================== Routes ==================
@app.get("/")
def home():
    return "ok"

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or not msg.get("text"):
        return "ok"

    if not should_reply(msg):
        return "ok"

    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    reply_to_message_id = msg.get("message_id")

    if not (chat_id and user_id and text):
        return "ok"

    # Background thread so webhook returns fast
    threading.Thread(
        target=process_message,
        args=(chat_id, user_id, text, reply_to_message_id),
        daemon=True
    ).start()

    return "ok"

# ================== Startup (Flask 3 safe) ==================
init_db()

if TG_TOKEN and PUBLIC_URL and WEBHOOK_SECRET:
    try:
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
    except Exception:
        pass
