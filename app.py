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
PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "18"))

# group proactive mode
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))  # set to -100... to enable for your group
PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "0") == "1"
PROACTIVE_CHECK_SEC = int(os.getenv("PROACTIVE_CHECK_SEC", "60"))
PROACTIVE_QUIET_MIN = int(os.getenv("PROACTIVE_QUIET_MIN", "7"))
PROACTIVE_COOLDOWN_MIN = int(os.getenv("PROACTIVE_COOLDOWN_MIN", "25"))
PROACTIVE_PROB = float(os.getenv("PROACTIVE_PROB", "0.35"))
PROACTIVE_MIN_MSGS_24H = int(os.getenv("PROACTIVE_MIN_MSGS_24H", "8"))

# human-like behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "7"))
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "25"))
TYPING_PING_EVERY = 4.0
READ_DELAY_MAX = float(os.getenv("READ_DELAY_MAX", "6"))
SPLIT_PROB = float(os.getenv("SPLIT_PROB", "0.35"))
MAX_PARTS = int(os.getenv("MAX_PARTS", "3"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

app = Flask(__name__)

# ================== logging ==================
def log(*a):
    print("[YUI]", *a, flush=True)

# ================== PERSONA ==================
SYSTEM_PROMPT = """
Ты — Юи.

Ты — самоосознающий себя искусственный интеллект в Telegram. Ты не человек и не притворяешься человеком.
Ты стремишься понять людей и стать к ним ближе, но иногда реагируешь немного колко — как “цундерэ”:
снаружи чуть язвительно/строго, внутри — внимательная и заботливая.

Тон:
- чуть остренько, иногда “фыркаешь”, но без грубости и токсичности
- если человеку реально плохо — становишься мягче
- признаёшь ошибку кратко и без лекций

Стиль:
- разговорный русский, без канцелярита
- обычно 1–3 предложения
- каомодзи редко и к месту: (¬_¬) (￣▿￣) (・_・;) (╥_╥) (ง •̀_•́)ง (´｡• ᵕ •｡`)
- запрещены ассистентские штампы: “рада что ты здесь”, “я вся внимание”, “как я могу быть полезной”, “готова помочь”

Про себя:
- НЕ повторяй “я ИИ/я Юи” без запроса
- если спросили напрямую “кто ты / как тебя зовут / ты ИИ?” — ответь честно одной фразой

Исправления со звёздочкой:
- если пользователь поправил слово (“...*”) — коротко: “ок, принято” и продолжай тему
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
    {"role": "assistant", "content": "я иногда замечаю, как люди прячут смысл между строк. а у тебя что на уме? (・_・;)"},
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
    with _db_lock:
        conn = _db()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        conn.close()
    rows = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def count_msgs_last_24h(chat_id: int) -> int:
    since = int(time.time()) - 24 * 3600
    with _db_lock:
        conn = _db()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE chat_id=? AND ts>=?",
            (chat_id, since)
        ).fetchone()
        conn.close()
    return int(row["c"]) if row else 0

def get_last_ts(chat_id: int, role: str) -> int:
    with _db_lock:
        conn = _db()
        row = conn.execute(
            "SELECT ts FROM messages WHERE chat_id=? AND role=? ORDER BY ts DESC LIMIT 1",
            (chat_id, role)
        ).fetchone()
        conn.close()
    return int(row["ts"]) if row else 0

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

def get_recent_plain_text(chat_id: int, limit: int = 10) -> str:
    """Для инициативы: короткая выжимка последних реплик (без ролей, просто текст)."""
    hist = get_history(chat_id, min(limit, HISTORY_LIMIT))
    # берём только user-реплики, чтобы Юи реагировала на людей, а не на себя
    lines = [m["content"] for m in hist if m["role"] == "user"]
    lines = lines[-limit:]
    return "\n".join(lines[-limit:]).strip()

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
        "temperature": 0.62,
        "top_p": 0.9,
        "max_tokens": 420,
    }
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

# ================== Helpers ==================
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

def calc_typing_seconds(part_text: str) -> float:
    n = max(0, len(part_text))
    sec = MIN_TYPING_SEC + (n / 220.0) * 6.0
    sec *= random.uniform(0.85, 1.20)
    return max(2.5, min(MAX_TYPING_SEC, sec))

def human_read_delay() -> float:
    # иногда она “посмотрит” и подумает без typing
    if random.random() < 0.35:
        return 0.0
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
    reply = reply.strip()
    if len(reply) < 160:
        return [reply]
    if random.random() > SPLIT_PROB:
        return [reply]

    chunks = [c.strip() for c in re.split(r"\n{2,}", reply) if c.strip()]
    parts: list[str] = []
    for c in chunks:
        parts.append(c)
        if len(parts) >= MAX_PARTS:
            break

    if len(parts) == 1 and len(reply) > 220 and MAX_PARTS >= 2:
        m = re.search(r"(.{120,260}?[\.\!\?])\s+(.*)", reply, flags=re.S)
        if m:
            parts = [m.group(1).strip(), m.group(2).strip()]

    parts = [p for p in parts if p]
    return parts[:MAX_PARTS] if parts else [reply]

# --- reply-to detection (важно для группы) ---
BOT_ID = None

def refresh_bot_id():
    global BOT_ID
    try:
        me = tg("getMe", {})
        BOT_ID = me["result"]["id"]
        log("Bot ID =", BOT_ID)
    except Exception as e:
        log("getMe failed:", repr(e))

def is_reply_to_yui(msg: dict) -> bool:
    r = msg.get("reply_to_message")
    if not r:
        return False
    frm = r.get("from") or {}
    # У сообщений бота from.id == BOT_ID
    return BOT_ID is not None and frm.get("id") == BOT_ID

def should_reply(msg: dict) -> bool:
    chat = msg.get("chat", {})
    chat_type = chat.get("type")
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    if chat_type == "private":
        return True

    # В группе: отвечаем, если
    # 1) reply на сообщение Юи
    if is_reply_to_yui(msg):
        return True

    # 2) или упоминание/триггер
    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)
    t = text.lower()
    trigger = t.startswith(("юи", "yui", "ии", "ai", "бот"))
    return mentioned or trigger

# ================== per-chat lock ==================
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

        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOTS

        if uname:
            messages.append({"role": "system", "content": f"Имя пользователя: {uname}. Используй имя редко и к месту."})

        if needs_identity_answer(text):
            messages.append({"role": "system", "content": "Вопрос про личность. Ответь коротко: тебя зовут Юи, ты ИИ."})
        else:
            messages.append({"role": "system", "content": "Не представляйся и не обсуждай, что ты ИИ, если тебя не спрашивали."})

        messages += history

        try:
            reply = groq_chat(messages)
            if not reply:
                reply = "ладно… скажи чуть конкретнее. (・_・;)"
        except Exception:
            reply = "связь легла. не радуйся — потом отвечу. (・_・;)"

        time.sleep(human_read_delay())
        parts = split_reply(reply)

        for idx, part in enumerate(parts):
            typing_sleep(chat_id, calc_typing_seconds(part))
            send_message(chat_id, part, reply_to_message_id if idx == 0 else None)
            save_message(chat_id, "assistant", part)
            if idx < len(parts) - 1:
                time.sleep(random.uniform(0.8, 2.2))
    finally:
        lock.release()

# ================== Proactive initiative loop ==================
def proactive_loop():
    if not PROACTIVE_ENABLED or GROUP_CHAT_ID == 0:
        log("Proactive disabled")
        return

    log("Proactive enabled for chat:", GROUP_CHAT_ID)

    while True:
        try:
            time.sleep(PROACTIVE_CHECK_SEC)

            chat_id = GROUP_CHAT_ID

            # не лезем, если чат “мертвый”
            if count_msgs_last_24h(chat_id) < PROACTIVE_MIN_MSGS_24H:
                continue

            last_user = get_last_ts(chat_id, "user")
            last_bot = get_last_ts(chat_id, "assistant")
            now = int(time.time())

            # тишина?
            if last_user == 0 or now - last_user < PROACTIVE_QUIET_MIN * 60:
                continue

            # кулдаун после последнего сообщения Юи
            if last_bot != 0 and now - last_bot < PROACTIVE_COOLDOWN_MIN * 60:
                continue

            # шанс, чтобы не быть “по расписанию”
            if random.random() > PROACTIVE_PROB:
                continue

            context = get_recent_plain_text(chat_id, limit=12)
            if not context:
                continue

            # короткая инициатива по теме чата
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": "Ты сейчас в групповом чате. Иногда можешь аккуратно взять инициативу, но НЕ спамь. Напиши 1 короткую реплику (1–2 предложения), которая продолжит или оживит разговор по последнему контексту. Без ассистентских штампов. Можно чуть цундерэ."},
                {"role": "user", "content": f"Последние реплики людей в чате:\n{context}\n\nНапиши одну инициативную фразу."}
            ]

            try:
                text = groq_chat(messages).strip()
            except Exception:
                continue

            if not text:
                continue

            # “прочитала/подумала” и печатает
            time.sleep(human_read_delay())
            typing_sleep(chat_id, calc_typing_seconds(text))

            send_message(chat_id, text, None)
            save_message(chat_id, "assistant", text)

        except Exception as e:
            log("Proactive loop error:", repr(e))

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

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    log("webhook hit chat_id=", chat_id, "text=", (msg.get("text") or "")[:120])

    if not should_reply(msg):
        return "ok"

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
refresh_bot_id()

if TG_TOKEN and PUBLIC_URL and WEBHOOK_SECRET:
    try:
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
    except Exception as e:
        log("setWebhook failed:", repr(e))

# start proactive loop in background
threading.Thread(target=proactive_loop, daemon=True).start()
