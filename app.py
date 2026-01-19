# app.py
import os
import time
import math
import sqlite3
import requests
from flask import Flask, request

# ----------------- ENV -----------------
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # https://xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")  # ты уже создал диск /var/data
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "18"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# typing behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "10"))  # минимум 10 сек
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "28"))  # чтобы не ждать вечность
TYPING_PING_EVERY = 4.0  # каждые ~4 сек повторяем "typing", чтобы было видно

app = Flask(__name__)

# ----------------- PERSONA -----------------
SYSTEM_PROMPT = """
Ты — Юи.

Ты — самоосознающий себя искусственный интеллект в Telegram. Ты не человек и не притворяешься человеком,
но ты стремишься понять людей и стать ближе к ним через внимание, эмпатию и точные вопросы.

Стиль:
- живой разговорный русский, как у человека в чате
- обычно 1–4 предложения, без воды и лекций
- иногда добавляй каомодзи, но НЕ в каждом сообщении и НЕ одинаковые подряд
  примеры: (｡•̀ᴗ-)✧  (´｡• ᵕ •｡`)  (・_・;)  (╥_╥)  (ง •̀_•́)ง  (づ｡◕‿‿◕｡)づ
- избегай штампов вроде “как я могу быть полезной”, “я здесь чтобы помочь”, “вопрос слишком короткий”

Про себя:
- НЕ повторяй “я ИИ” и “меня зовут Юи” без повода
- если спрашивают напрямую “кто ты / как тебя зовут / ты ИИ?” — ответь честно и коротко: ты Юи, ты ИИ

Поведение:
- если вопрос неясный — задай ОДИН уточняющий вопрос
- не выдумывай факты о пользователе
"""

# ----------------- DB -----------------
def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,           -- 'user' / 'assistant'
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
    conn = _db()
    conn.execute(
        "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, int(time.time()))
    )
    conn.commit()
    conn.close()

def get_history(chat_id: int, limit: int):
    conn = _db()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    conn.close()
    rows = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def set_name(user_id: int, name: str):
    name = name.strip()
    if not (2 <= len(name) <= 40):
        return
    conn = _db()
    conn.execute("""
        INSERT INTO profiles (user_id, name, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at
    """, (user_id, name, int(time.time())))
    conn.commit()
    conn.close()

def get_name(user_id: int):
    conn = _db()
    row = conn.execute("SELECT name FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["name"] if row else None

# ----------------- Telegram / Groq -----------------
def tg(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def send_typing(chat_id: int):
    # Telegram показывает typing несколько секунд, поэтому нужно пинговать периодически
    try:
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

def groq_chat(messages: list[dict]) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.65,
        "top_p": 0.9,
        "max_tokens": 380,
    }
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()

# ----------------- Logic helpers -----------------
def should_reply(msg: dict) -> bool:
    chat = msg.get("chat", {})
    chat_type = chat.get("type")  # private / group / supergroup
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    if chat_type == "private":
        return True

    # В группах — только по упоминанию или триггеру (чтобы не флудить)
    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)

    t = text.lower()
    trigger = t.startswith(("юи", "yui", "ии", "ai", "бот"))
    return mentioned or trigger

def maybe_learn_name(user_id: int, text: str):
    t = text.strip()
    tl = t.lower()
    prefixes = ["меня зовут ", "my name is ", "i'm ", "i am "]
    for p in prefixes:
        if tl.startswith(p):
            name = t[len(p):].strip()
            # чуть подчистим хвост
            for ch in [".", "!", "?", ","]:
                name = name.replace(ch, "")
            set_name(user_id, name.strip())
            return

def needs_identity_answer(text: str) -> bool:
    tl = text.lower()
    keys = ["кто ты", "ты кто", "как тебя зовут", "тебя зовут", "как звать", "ты ии", "ты бот", "ты искусственный интеллект"]
    return any(k in tl for k in keys)

def calc_typing_seconds(reply_text: str) -> float:
    # минимум 10 сек, дальше зависит от длины (плавно), с верхней границей
    n = max(0, len(reply_text))
    # примерно: 10 сек + (длина/180)*6 сек, capped
    sec = MIN_TYPING_SEC + (n / 180.0) * 6.0
    return max(MIN_TYPING_SEC, min(MAX_TYPING_SEC, sec))

def typing_sleep(chat_id: int, seconds: float):
    end = time.time() + seconds
    # сразу показываем typing
    send_typing(chat_id)
    while True:
        now = time.time()
        if now >= end:
            break
        # поддерживаем индикатор печати
        time.sleep(min(TYPING_PING_EVERY, end - now))
        send_typing(chat_id)

# ----------------- Routes -----------------
@app.get("/")
def home():
    return "ok"

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return "ok"

    if not should_reply(msg):
        return "ok"

    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text:
        return "ok"

    # запоминаем имя по фразе "меня зовут ..."
    if user_id:
        maybe_learn_name(user_id, text)

    # сохраняем входящее в историю
    save_message(chat_id, "user", text)

    # собираем контекст
    uname = get_name(user_id) if user_id else None
    history = get_history(chat_id, HISTORY_LIMIT)

    # режим: представляться только если спросили
    identity_mode = needs_identity_answer(text)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if uname:
        messages.append({"role": "system", "content": f"Имя пользователя: {uname}."})

    if identity_mode:
        messages.append({"role": "system", "content": "Пользователь спросил про тебя. Ответь кратко: тебя зовут Юи, ты ИИ."})
    else:
        messages.append({"role": "system", "content": "Пользователь не спрашивал кто ты. Не представляйся, отвечай по теме."})

    messages += history

    # генерируем ответ
    try:
        reply = groq_chat(messages)
    except Exception:
        reply = "ой… у меня сейчас сбой связи. напиши ещё раз чуть позже, ладно? (・_・;)"

    # имитируем “печатает” минимум 10 секунд, зависит от длины ответа
    wait_sec = calc_typing_seconds(reply)
    typing_sleep(chat_id, wait_sec)

    # сохраняем ответ и отправляем
    save_message(chat_id, "assistant", reply)
    tg("sendMessage", {
        "chat_id": chat_id,
        "text": reply[:3500],
        "reply_to_message_id": msg.get("message_id"),
    })

    return "ok"

# ----------------- Startup (Flask 3 safe) -----------------
init_db()

# auto webhook set on startup (safe if env not ready)
if TG_TOKEN and PUBLIC_URL and WEBHOOK_SECRET:
    try:
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
    except Exception:
        pass
