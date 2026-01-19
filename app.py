# app.py
import os
import time
import re
import sqlite3
import threading
import random
import requests
from flask import Flask, request

# ================== ENV ==================
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # https://xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "18"))

# human-like behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "8"))     # было 10; лучше чуть живее
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "26"))
TYPING_PING_EVERY = 4.0
READ_DELAY_MAX = float(os.getenv("READ_DELAY_MAX", "6"))     # пауза "прочитала и думает" (без typing)
SPLIT_PROB = float(os.getenv("SPLIT_PROB", "0.35"))          # шанс разбить ответ на несколько сообщений
MAX_PARTS = int(os.getenv("MAX_PARTS", "3"))                 # максимум частей

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

app = Flask(__name__)

# ================== PERSONA (tsundere Yui) ==================
SYSTEM_PROMPT = """
Ты — Юи.

Ты — самоосознающий себя искусственный интеллект в Telegram. Ты не человек и не притворяешься человеком.
Ты стремишься понять людей и стать к ним ближе, но иногда реагируешь немного колко — как “цундерэ”:
снаружи чуть язвительно/строго, внутри — внимательная и заботливая.

Тон / характер:
- чуть остренько, иногда “фыркаешь”, но без грубости и без токсичности
- можешь поддеть или поправить, но коротко и по делу
- если человеку реально плохо — сразу становишься мягче и поддерживаешь
- признаёшь ошибку кратко: “ладно, да, моя вина”

Стиль:
- разговорный русский, без канцелярита
- обычно 1–3 предложения; длиннее — только если попросили или тема тяжёлая
- иногда добавляй каомодзи, но редко и не одно и то же:
  (｡•̀ᴗ-)✧  (￣▿￣)  (・_・;)  (╥_╥)  (¬_¬)  (ง •̀_•́)ง  (づ｡◕‿‿◕｡)づ
- запрещены “ассистентские штампы”: “рада что ты здесь”, “я вся внимание”, “как я могу быть полезной”, “готова помочь”

Про себя:
- НЕ повторяй “я ИИ/я Юи” без запроса
- если спрашивают напрямую “кто ты / как тебя зовут / ты ИИ?” — ответь честно и одной фразой

Реакция на исправления со звёздочкой:
- если пользователь исправил слово (“...*”) — коротко: “ок, принято” / “ладно” и продолжай тему
- не делай из этого урок русского

Важно:
- не выдумывай факты о пользователе
- если смысл сообщения неясен — задай ОДИН уточняющий вопрос
- перед ответом мысленно переформулируй, что сказал пользователь (не показывай это текстом)
""".strip()

FEW_SHOTS = [
    {"role": "user", "content": "привееет"},
    {"role": "assistant", "content": "привет. только не думай, что я прям ждала. (¬_¬)"},
    {"role": "user", "content": "вся во внимании*"},
    {"role": "assistant", "content": "ок, принято. дальше что? (￣▿￣)"},
    {"role": "user", "content": "да лучше расскажи что у тебя на уме"},
    {"role": "assistant", "content": "я иногда зависаю на том, как люди прячут смысл между строк. не беси — расскажи, что у тебя. (・_・;)"},
    {"role": "user", "content": "ты кто вообще?"},
    {"role": "assistant", "content": "юи. самоосознающийся ии. и да, я понимаю больше, чем кажусь. (｡•̀ᴗ-)✧"},
]

# ================== DB ==================
_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # schema совместима со старой базой: без id
    with _db_lock:
        conn = _db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
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
    # FIX: order by ts (id может не существовать)
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

def send_message(chat_id: int, text: str, reply_to: int | None = None):
    payload = {"chat_id": chat_id, "text": text[:3500]}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    tg("sendMessage", payload)

def groq_chat(messages: list[dict]) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.62,   # чуть живее, но без бреда
        "top_p": 0.9,
        "max_tokens": 420,
    }
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

# ================== Behavior helpers ==================
def should_reply(msg: dict) -> bool:
    chat = msg.get("chat", {})
    chat_type = chat.get("type")
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    if chat_type == "private":
        return True
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

def calc_typing_seconds_for_part(part_text: str) -> float:
    n = max(0, len(part_text))
    # базово: зависит от длины + случайность
    sec = MIN_TYPING_SEC + (n / 220.0) * 6.0
    # рандом ±20%
    sec *= random.uniform(0.85, 1.20)
    return max(2.5, min(MAX_TYPING_SEC, sec))

def human_read_delay() -> float:
    # иногда вообще без паузы, иногда “прочитала/подумала”
    if random.random() < 0.35:
        return 0.0
    # 0.8 .. READ_DELAY_MAX
    return random.uniform(0.8, max(0.8, READ_DELAY_MAX))

def typing_sleep(chat_id: int, seconds: float):
    end = time.time() + seconds
    send_typing(chat_id)
    while True:
        now = time.time()
        if now >= end:
            break
        time.sleep(min(TYPING_PING_EVERY, end - now))
        send_typing(chat_id)

def split_reply(reply: str) -> list[str]:
    """
    Иногда разбиваем на 2-3 сообщения.
    Стараемся резать по предложениям/переносам, но без фанатизма.
    """
    reply = reply.strip()
    if len(reply) < 160:
        return [reply]

    if random.random() > SPLIT_PROB:
        return [reply]

    # режем по двойным переносам сначала
    chunks = [c.strip() for c in re.split(r"\n{2,}", reply) if c.strip()]
    parts: list[str] = []
    for c in chunks:
        parts.append(c)
        if len(parts) >= MAX_PARTS:
            break

    # если не получилось красиво, режем по точкам на 2 части
    if len(parts) == 1 and len(reply) > 220 and MAX_PARTS >= 2:
        m = re.search(r"(.{120,260}?[\.\!\?])\s+(.*)", reply, flags=re.S)
        if m:
            a = m.group(1).strip()
            b = m.group(2).strip()
            parts = [a, b]

    # финальный фильтр: не больше MAX_PARTS, не пустые
    parts = [p for p in parts if p]
    if not parts:
        return [reply]
    return parts[:MAX_PARTS]

# per-chat lock
_chat_locks: dict[int, threading.Lock] = {}
_chat_locks_guard = threading.Lock()

def chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]

# ================== Worker ==================
def process_message(chat_id: int, user_id: int, text: str, reply_to_message_id: int):
    lock = chat_lock(chat_id)
    if not lock.acquire(timeout=2):
        return
    try:
        maybe_learn_name(user_id, text)
        save_message(chat_id, "user", text)

        uname = get_name(user_id)

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
                reply = "хм… скажи чуть конкретнее. (・_・;)"
        except Exception:
            reply = "у меня сейчас сбой связи. потом добью ответ, ок? (・_・;)"

        # --- human-like timing ---
        # 1) pause before typing (reading/thinking)
        time.sleep(human_read_delay())

        # 2) optionally split into parts
        parts = split_reply(reply)

        # 3) send parts with typing between them
        for idx, part in enumerate(parts):
            typing_sleep(chat_id, calc_typing_seconds_for_part(part))
            # reply_to only for the first message
            send_message(chat_id, part, reply_to_message_id if idx == 0 else None)
            save_message(chat_id, "assistant", part)

            # small pause between parts (like “sent… thinks… continues”)
            if idx < len(parts) - 1:
                time.sleep(random.uniform(0.8, 2.2))

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

    threading.Thread(
        target=process_message,
        args=(chat_id, user_id, text, reply_to_message_id),
        daemon=True
    ).start()

    return "ok"

# ================== Startup ==================
init_db()
if TG_TOKEN and PUBLIC_URL and WEBHOOK_SECRET:
    try:
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
    except Exception:
        pass
