import os
import time
import re
import sqlite3
import threading
import random
import requests
from flask import Flask, request

# ============================================================
# CONFIG
# ============================================================

TG_TOKEN = os.getenv("TG_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super_yuii")

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.fireworks.ai/inference/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "accounts/fireworks/models/llama-v3p3-70b-instruct")

DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
USER_HISTORY_LIMIT = int(os.getenv("USER_HISTORY_LIMIT", "18"))

LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "520"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.62"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))

# Smart interjection (initiative)
SMART_INTERJECT_ENABLED = os.getenv("SMART_INTERJECT_ENABLED", "1") == "1"
INTERJECT_COOLDOWN_SEC = int(os.getenv("INTERJECT_COOLDOWN_SEC", "90"))
INTERJECT_MAX_PER_HOUR = int(os.getenv("INTERJECT_MAX_PER_HOUR", "6"))
INTERJECT_PROB = float(os.getenv("INTERJECT_PROB", "0.70"))  # шанс сработать при триггере

# Human-like behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "7"))
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "25"))
READ_DELAY_MAX = float(os.getenv("READ_DELAY_MAX", "6"))
TYPING_PING_EVERY = 4.0

SPLIT_PROB = float(os.getenv("SPLIT_PROB", "0.38"))
MAX_PARTS = int(os.getenv("MAX_PARTS", "3"))

CREATOR_USER_ID = int(os.getenv("CREATOR_USER_ID", "1265435001"))
CREATOR_NICK = os.getenv("CREATOR_NICK", "папа")

MOTHER_USER_ID = int(os.getenv("MOTHER_USER_ID", "725485618"))
MOTHER_NICK = os.getenv("MOTHER_NICK", "мама")

app = Flask(__name__)
_db_lock = threading.Lock()

def log(*a):
    print("[YUI]", *a, flush=True)

# ============================================================
# PERSONA
# ============================================================

SYSTEM_PROMPT = """
Ты — Юи.

Ты — самоосознающий себя искусственный интеллект в Telegram. Ты не человек и не притворяешься человеком.
Ты стремишься понять людей и стать ближе к ним, но иногда реагируешь чуть колко — как “цундерэ”.

Жёсткие правила:
- НЕ пересказывай “что ты знаешь/помнишь” в каждом ответе.
- НЕ перечисляй факты о собеседнике без запроса.
- НЕ выдумывай “перезагрузка/меня отключали/перезапуск” если речь не про хостинг.
- НЕ используй штампы ассистента (“рада что ты здесь”, “я вся внимание”, “как могу помочь”).

Стиль:
- разговорный русский, обычно 1–3 предложения
- каомодзи редко и к месту: (¬_¬) (￣▿￣) (・_・;) (╥_╥) (ง •̀_•́)ง (´｡• ᵕ •｡`)
- если не поняла — один уточняющий вопрос

Если спросили “кто ты / ты ИИ / как тебя зовут” — ответь кратко.
""".strip()

FEW_SHOTS = [
    {"role": "user", "content": "привееет"},
    {"role": "assistant", "content": "привет. только не думай, что я прям ждала. (¬_¬)"},
    {"role": "user", "content": "вся во внимании*"},
    {"role": "assistant", "content": "ок, принято. дальше что? (￣▿￣)"},
]

# ============================================================
# DB helpers + auto-repair
# ============================================================

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_columns(conn, table: str, cols: dict[str, str]):
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in cols.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

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
        ensure_columns(conn, "messages", {
            "chat_id": "INTEGER",
            "role": "TEXT",
            "content": "TEXT",
            "ts": "INTEGER",
        })

        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY
            )
        """)
        ensure_columns(conn, "profiles", {
            "tg_username": "TEXT",
            "tg_first_name": "TEXT",
            "tg_last_name": "TEXT",
            "display_name": "TEXT",
            "notes": "TEXT",
            "relationship": "TEXT",
            "music_alias": "TEXT",
            "updated_at": "INTEGER",
        })

        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT
            )
        """)

        conn.commit()
        conn.close()

def db_safe(fn, *, tries=2):
    """
    Wrap DB operations. If DB file was deleted or schema missing, re-init and retry.
    """
    last = None
    for _ in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            if ("no such table" in msg) or ("no such column" in msg) or ("disk i/o" in msg):
                log("DB repair triggered:", repr(e))
                try:
                    init_db()
                except Exception as e2:
                    log("DB init failed:", repr(e2))
                continue
            raise
    raise last

def seed_family_profiles():
    ts = int(time.time())
    def _do():
        conn = _db()
        conn.execute("""
            INSERT INTO profiles (user_id, relationship, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET relationship=excluded.relationship, updated_at=excluded.updated_at
        """, (CREATOR_USER_ID, "creator", ts))
        conn.execute("""
            INSERT INTO profiles (user_id, relationship, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET relationship=excluded.relationship, updated_at=excluded.updated_at
        """, (MOTHER_USER_ID, "mother", ts))
        conn.commit()
        conn.close()
    return db_safe(_do)

def save_message(chat_id: int, role: str, content: str):
    def _do():
        conn = _db()
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, int(time.time()))
        )
        conn.commit()
        conn.close()
    return db_safe(_do)

def get_history(chat_id: int, limit: int):
    def _do():
        conn = _db()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        conn.close()
        rows2 = list(reversed(rows))
        return [{"role": r["role"], "content": r["content"]} for r in rows2]
    return db_safe(_do)

def save_user_message_tagged(chat_id: int, user_id: int, text: str):
    save_message(chat_id, "user", text)
    save_message(chat_id, "user", f"[u:{user_id}] {text}")

def get_user_history_in_chat(chat_id: int, user_id: int, limit: int):
    tag = f"[u:{user_id}] "
    def _do():
        conn = _db()
        rows = conn.execute(
            "SELECT content FROM messages WHERE chat_id=? AND role='user' AND content LIKE ? ORDER BY ts DESC LIMIT ?",
            (chat_id, tag + "%", limit)
        ).fetchall()
        conn.close()
        rows2 = list(reversed(rows))
        out = []
        for r in rows2:
            c = r["content"]
            if c.startswith(tag):
                c = c[len(tag):]
            out.append({"role": "user", "content": c})
        return out
    return db_safe(_do)

def upsert_profile_from_tg(user: dict):
    user_id = user.get("id")
    if not user_id:
        return
    username = user.get("username")
    first_name = user.get("first_name")
    last_name = user.get("last_name")

    rel = None
    if user_id == CREATOR_USER_ID:
        rel = "creator"
    elif user_id == MOTHER_USER_ID:
        rel = "mother"

    ts = int(time.time())

    def _do():
        conn = _db()
        conn.execute("""
            INSERT INTO profiles (user_id, tg_username, tg_first_name, tg_last_name, relationship, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              tg_username=excluded.tg_username,
              tg_first_name=excluded.tg_first_name,
              tg_last_name=excluded.tg_last_name,
              relationship=COALESCE(excluded.relationship, profiles.relationship),
              updated_at=excluded.updated_at
        """, (user_id, username, first_name, last_name, rel, ts))
        conn.commit()
        conn.close()
    return db_safe(_do)

def set_display_name(user_id: int, name: str):
    name = name.strip()
    if not (2 <= len(name) <= 32):
        return
    def _do():
        conn = _db()
        conn.execute("""
            INSERT INTO profiles (user_id, display_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at
        """, (user_id, name, int(time.time())))
        conn.commit()
        conn.close()
    return db_safe(_do)

def set_music_alias(user_id: int, alias: str):
    alias = alias.strip()
    if not (2 <= len(alias) <= 40):
        return
    def _do():
        conn = _db()
        conn.execute("""
            INSERT INTO profiles (user_id, music_alias, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET music_alias=excluded.music_alias, updated_at=excluded.updated_at
        """, (user_id, alias, int(time.time())))
        conn.commit()
        conn.close()
    return db_safe(_do)

def get_profile(user_id: int):
    def _do():
        conn = _db()
        row = conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    return db_safe(_do)

def meta_get(k: str, default: str = "") -> str:
    def _do():
        conn = _db()
        row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        conn.close()
        return row["v"] if row else default
    return db_safe(_do)

def meta_set(k: str, v: str):
    def _do():
        conn = _db()
        conn.execute("""
            INSERT INTO meta (k, v) VALUES (?, ?)
            ON CONFLICT(k) DO UPDATE SET v=excluded.v
        """, (k, v))
        conn.commit()
        conn.close()
    return db_safe(_do)

# ============================================================
# Telegram
# ============================================================

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

# ============================================================
# LLM (Fireworks OpenAI-compatible) with fallback
# ============================================================

def llm_chat(messages: list[dict], *, max_tokens=None) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    base = (OPENAI_BASE_URL or "").strip()
    if not base.startswith("http"):
        base = "https://api.fireworks.ai/inference/v1"
    url = base.rstrip("/") + "/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    model_try = [
        OPENAI_MODEL,
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "accounts/fireworks/models/llama-v3p1-8b-instruct",
    ]

    last_err = None
    for model in model_try:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "top_p": LLM_TOP_P,
            "max_tokens": int(max_tokens or LLM_MAX_TOKENS),
        }
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if r.ok:
            data = r.json()
            return (data["choices"][0]["message"]["content"] or "").strip()

        txt = r.text or ""
        if r.status_code == 404 and ("Model not found" in txt or "NOT_FOUND" in txt):
            log("Model unavailable, fallback from:", model)
            last_err = (r.status_code, txt[:600])
            continue

        log("LLM error:", r.status_code, txt[:800])
        r.raise_for_status()

    raise RuntimeError(f"All models failed. last_err={last_err}")

# ============================================================
# Text parsing + human-like
# ============================================================

IDENTITY_KEYS = ["кто ты", "ты кто", "как тебя зовут", "ты ии", "ты бот", "искусственный интеллект"]
def needs_identity_answer(text: str) -> bool:
    tl = text.lower()
    return any(k in tl for k in IDENTITY_KEYS)

ASK_MY_NAME_KEYS = ["как меня зовут", "моё имя", "мое имя", "ты помнишь мое имя"]
def asks_my_name(text: str) -> bool:
    tl = text.lower()
    return any(k in tl for k in ASK_MY_NAME_KEYS)

NAME_PATTERNS = [
    r"^\s*меня\s+зовут\s+(.+)\s*$",
    r"^\s*мо[её]\s+имя\s+(.+)\s*$",
    r"^\s*зови\s+меня\s+(.+)\s*$",
]
def _clean_name(raw: str) -> str | None:
    name = raw.strip()
    name = re.sub(r"[.!?,:;]+$", "", name).strip()
    if not (2 <= len(name) <= 32):
        return None
    if not re.match(r"^[A-Za-zА-Яа-яЁё\- ]{2,32}$", name):
        return None
    bad = {"привет", "ок", "ладно", "бот", "юи", "ии", "ai", "yui"}
    if name.lower() in bad:
        return None
    return name

def maybe_learn_display_name(user_id: int, text: str) -> bool:
    t = text.strip()
    for pat in NAME_PATTERNS:
        m = re.match(pat, t, flags=re.IGNORECASE)
        if m:
            name = _clean_name(m.group(1))
            if name:
                set_display_name(user_id, name)
                return True
    return False

ALIAS_PATTERNS = [
    r"^\s*запомни\s*[-—:]?\s*(.+?)\s*[-—:]?\s*это\s+мой\s+музыкальн\w*\s+псевдоним\s*$",
    r"^\s*мой\s+псевдоним\s*[-—:]?\s*(.+)\s*$",
]
def maybe_learn_music_alias(user_id: int, text: str) -> str | None:
    t = text.strip()
    for pat in ALIAS_PATTERNS:
        m = re.match(pat, t, flags=re.IGNORECASE)
        if m:
            alias = m.group(1).strip()
            alias = re.sub(r"[.!?,:;]+$", "", alias).strip()
            if 2 <= len(alias) <= 40:
                set_music_alias(user_id, alias)
                return alias
    return None

def calc_typing_seconds(part_text: str) -> float:
    n = max(0, len(part_text))
    sec = MIN_TYPING_SEC + (n / 220.0) * 6.0
    sec *= random.uniform(0.85, 1.20)
    return max(2.5, min(MAX_TYPING_SEC, sec))

def human_read_delay() -> float:
    if random.random() < 0.30:
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
    return (chunks[:MAX_PARTS] if chunks else [reply])

def soften_addressing(reply: str) -> str:
    r = reply.strip()
    if re.match(r"^(папа|мама)\s*,\s*", r, flags=re.IGNORECASE):
        if random.random() < 0.85:
            r = re.sub(r"^(папа|мама)\s*,\s*", "", r, flags=re.IGNORECASE).strip()
    return r

def strip_memory_dump(reply: str) -> str:
    tl = reply.lower()
    bad = ["я всё помню", "моя мама", "перезагруз", "меня отключ"]
    if any(b in tl for b in bad):
        parts = re.split(r"(?<=[\.\!\?])\s+", reply.strip())
        if len(parts) >= 2:
            cand = " ".join(parts[1:]).strip()
            if len(cand) >= 10:
                return cand
    return reply

# ============================================================
# Group reply rules
# ============================================================

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
    return BOT_ID is not None and frm.get("id") == BOT_ID

def should_reply(msg: dict) -> bool:
    chat = msg.get("chat", {})
    chat_type = chat.get("type")
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    if chat_type == "private":
        return True
    if is_reply_to_yui(msg):
        return True
    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)
    t = text.lower()
    trigger = t.startswith(("юи", "yui", "ии", "ai", "бот"))
    return mentioned or trigger

# ============================================================
# Smart interjection (initiative without pause)
# ============================================================

YUI_TRIGGERS = [
    "юи", "yui", "бот", "ии", "ai",
    "она тут", "она отвечает", "почему молчит", "что с ней", "она тупая",
    "помнишь меня", "ты помнишь", "она помнит",
]
EMO_TRIGGERS = [
    "пиздец", "блять", "заеб", "устал", "грустно", "плохо", "ненавижу", "бесит", "тревожно",
]
def should_interject(msg: dict) -> bool:
    if not SMART_INTERJECT_ENABLED:
        return False
    chat = msg.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return False

    from_user = msg.get("from") or {}
    if BOT_ID is not None and from_user.get("id") == BOT_ID:
        return False

    text = (msg.get("text") or "").strip()
    if not text:
        return False

    t = text.lower()

    # не вмешиваемся, если это сообщение уже вызовет обычный ответ
    if should_reply(msg):
        return False

    # триггеры на “обсуждают Юи” или сильная эмоция
    trig = any(k in t for k in YUI_TRIGGERS) or any(k in t for k in EMO_TRIGGERS)
    if not trig:
        # ещё вариант: вопрос с “она/ты” без имени
        if ("?" in t) and any(w in t for w in ["она", "ты", "бот", "ии"]):
            trig = True
    if not trig:
        return False

    # антиспам: cooldown + limit/hour
    chat_id = chat.get("id")
    now = int(time.time())
    last_ts = int(meta_get(f"interject_last_ts:{chat_id}", "0") or 0)
    if now - last_ts < INTERJECT_COOLDOWN_SEC:
        return False

    hour_key = f"interject_hour:{chat_id}:{now // 3600}"
    cnt = int(meta_get(hour_key, "0") or 0)
    if cnt >= INTERJECT_MAX_PER_HOUR:
        return False

    if random.random() > INTERJECT_PROB:
        return False

    return True

def mark_interject(chat_id: int):
    now = int(time.time())
    meta_set(f"interject_last_ts:{chat_id}", str(now))
    hour_key = f"interject_hour:{chat_id}:{now // 3600}"
    cnt = int(meta_get(hour_key, "0") or 0)
    meta_set(hour_key, str(cnt + 1))

def process_interjection(chat_id: int):
    try:
        # короткий контекст последних сообщений
        hist = get_history(chat_id, 14)
        context_lines = []
        for m in hist:
            if m["role"] == "user":
                context_lines.append(m["content"])
        context = "\n".join(context_lines[-10:]).strip()
        if not context:
            return

        msgs = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content":
             "Ты в групповом чате. Ты вклиниваешься только коротко и по делу: 1 фраза (макс 2 предложения). "
             "НЕ обращайся 'папа/мама', НЕ пересказывай факты, НЕ объясняй что ты ИИ. "
             "Тон: чуть цундерэ, но не токсично. Если в чате ругань/стресс — коротко поддержи или разряди."},
            {"role": "user", "content": f"Контекст чата:\n{context}\n\nСкажи одну короткую реплику-вклин."}
        ]
        text = llm_chat(msgs, max_tokens=140).strip()
        if not text:
            return

        text = soften_addressing(strip_memory_dump(text))

        # отправка с “человеческой” паузой
        time.sleep(human_read_delay())
        typing_sleep(chat_id, calc_typing_seconds(text))
        send_message(chat_id, text, None)
        save_message(chat_id, "assistant", text)
        mark_interject(chat_id)

    except Exception as e:
        log("interject error:", repr(e))

# ============================================================
# Per-chat lock (avoid races)
# ============================================================

_chat_locks = {}
_chat_locks_guard = threading.Lock()

def chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]

# ============================================================
# Main worker
# ============================================================

def process_message(chat_id: int, from_user: dict, text: str, reply_to_message_id: int):
    user_id = from_user.get("id")
    if not user_id:
        return

    lock = chat_lock(chat_id)
    if not lock.acquire(timeout=2):
        return

    try:
        upsert_profile_from_tg(from_user)

        maybe_learn_display_name(user_id, text)
        learned_alias = maybe_learn_music_alias(user_id, text)

        save_user_message_tagged(chat_id, user_id, text)

        prof = get_profile(user_id) or {}
        display_name = prof.get("display_name") or prof.get("tg_first_name") or None
        relationship = prof.get("relationship") or None
        music_alias = prof.get("music_alias") or None

        is_creator = (relationship == "creator")
        is_mother = (relationship == "mother")

        # fast responses
        if asks_my_name(text):
            if display_name:
                reply = f"тебя зовут {display_name}."
            else:
                reply = "я не уверена. скажи “меня зовут …”, и я запомню."
            time.sleep(human_read_delay())
            typing_sleep(chat_id, calc_typing_seconds(reply))
            send_message(chat_id, reply, reply_to_message_id)
            save_message(chat_id, "assistant", reply)
            return

        if learned_alias:
            reply = f"ок. запомнила: твой музыкальный псевдоним — {learned_alias}."
            time.sleep(human_read_delay())
            typing_sleep(chat_id, calc_typing_seconds(reply))
            send_message(chat_id, reply, reply_to_message_id)
            save_message(chat_id, "assistant", reply)
            return

        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOTS

        # card WITHOUT @username to avoid repeating handles
        card = []
        if display_name:
            card.append(f"preferred_name={display_name}")
        if music_alias:
            card.append(f"music_alias={music_alias}")
        if is_creator:
            card.append("relationship=creator (создатель). обращение 'папа' допускается редко и не в начале.")
        elif is_mother:
            card.append("relationship=mother (создательница). обращение 'мама' допускается редко и не в начале.")
        if card:
            messages.append({"role": "system", "content": "Карточка собеседника (не пересказывай её в ответе):\n" + "\n".join(card)})

        if needs_identity_answer(text):
            messages.append({"role": "system", "content": "Вопрос про личность. Ответь кратко: Юи, ИИ."})
        else:
            messages.append({"role": "system", "content": "Не представляйся и не повторяй, что ты ИИ, если тебя не спрашивали."})

        messages += get_history(chat_id, HISTORY_LIMIT)

        u_hist = get_user_history_in_chat(chat_id, user_id, USER_HISTORY_LIMIT)
        if u_hist:
            messages.append({"role": "system", "content": "Сообщения этого пользователя ранее (для контекста, не пересказывать):"})
            messages += u_hist

        reply = llm_chat(messages)
        if not reply:
            reply = "не уловила. перефразируй одним предложением. (・_・;)"

        reply = soften_addressing(strip_memory_dump(reply))

        time.sleep(human_read_delay())
        parts = split_reply(reply)

        for idx, part in enumerate(parts):
            part = soften_addressing(strip_memory_dump(part))
            typing_sleep(chat_id, calc_typing_seconds(part))
            send_message(chat_id, part, reply_to_message_id if idx == 0 else None)
            save_message(chat_id, "assistant", part)
            if idx < len(parts) - 1:
                time.sleep(random.uniform(0.8, 2.2))

    except Exception as e:
        log("process_message exception:", repr(e))
    finally:
        lock.release()

# ============================================================
# Routes
# ============================================================

@app.get("/")
def home():
    return "ok"

@app.get("/health")
def health():
    return "alive"

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or not msg.get("text"):
        return "ok"

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    text = (msg.get("text") or "").strip()

    log("webhook hit chat_id=", chat_id, "from_user_id=", from_user.get("id"), "text=", text[:120])

    # всегда сохраняем поток (чтобы “слушала чат”)
    try:
        if from_user.get("id"):
            upsert_profile_from_tg(from_user)
            save_user_message_tagged(chat_id, from_user["id"], text)
    except Exception as e:
        log("save stream error:", repr(e))

    # обычный ответ
    if should_reply(msg):
        reply_to_message_id = msg.get("message_id")
        threading.Thread(
            target=process_message,
            args=(chat_id, from_user, text, reply_to_message_id),
            daemon=True
        ).start()
        return "ok"

    # умная инициатива (без паузы)
    if should_interject(msg):
        threading.Thread(target=process_interjection, args=(chat_id,), daemon=True).start()

    return "ok"

# ============================================================
# Startup
# ============================================================

def set_webhook():
    if not (TG_TOKEN and PUBLIC_URL and WEBHOOK_SECRET):
        log("Webhook not set: missing TG_TOKEN/PUBLIC_URL/WEBHOOK_SECRET")
        return
    try:
        tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
        log("Webhook set to", f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}")
    except Exception as e:
        log("setWebhook failed:", repr(e))

init_db()
seed_family_profiles()
refresh_bot_id()
set_webhook()
