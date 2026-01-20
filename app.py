```python
# app.py
import os
import time
import re
import sqlite3
import threading
import random
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request

# ============================================================
# CONFIG (Render env)
# ============================================================

TG_TOKEN = os.getenv("TG_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or os.getenv("WEBHOOK_SECRET", "super_yuii")

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.fireworks.ai/inference/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "accounts/fireworks/models/llama-v3p3-70b-instruct")

DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")

# Memory sizes (token cost!)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "55"))
USER_HISTORY_LIMIT = int(os.getenv("USER_HISTORY_LIMIT", "22"))

LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "520"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.62"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))

# Human-like behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "7"))
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "25"))
READ_DELAY_MAX = float(os.getenv("READ_DELAY_MAX", "6"))
TYPING_PING_EVERY = 4.0

SPLIT_PROB = float(os.getenv("SPLIT_PROB", "0.38"))
MAX_PARTS = int(os.getenv("MAX_PARTS", "3"))

# Smart interjection (initiative without pause)
SMART_INTERJECT_ENABLED = os.getenv("SMART_INTERJECT_ENABLED", "1") == "1"
INTERJECT_COOLDOWN_SEC = int(os.getenv("INTERJECT_COOLDOWN_SEC", "90"))
INTERJECT_MAX_PER_HOUR = int(os.getenv("INTERJECT_MAX_PER_HOUR", "6"))
INTERJECT_PROB = float(os.getenv("INTERJECT_PROB", "0.70"))

# Proactive engine (human-like check-ins / greetings)
PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "1") == "1"
PROACTIVE_LOOP_SEC = int(os.getenv("PROACTIVE_LOOP_SEC", "45"))

PROACTIVE_DEFAULT_PRIVATE = os.getenv("PROACTIVE_DEFAULT_PRIVATE", "1") == "1"
PROACTIVE_DEFAULT_GROUP = os.getenv("PROACTIVE_DEFAULT_GROUP", "1") == "1"

PROACTIVE_COOLDOWN_MIN = int(os.getenv("PROACTIVE_COOLDOWN_MIN", "60"))
PROACTIVE_CAP_PRIVATE_PER_DAY = int(os.getenv("PROACTIVE_CAP_PRIVATE_PER_DAY", "2"))
PROACTIVE_CAP_GROUP_PER_DAY = int(os.getenv("PROACTIVE_CAP_GROUP_PER_DAY", "1"))

# Moscow time
TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)

QUIET_HOURS_START = float(os.getenv("QUIET_HOURS_START", "1.0"))  # 01:00 MSK
QUIET_HOURS_END = float(os.getenv("QUIET_HOURS_END", "8.0"))      # 08:00 MSK

# Morning / evening windows (MSK)
MORNING_START = float(os.getenv("MORNING_START", "7.5"))   # 07:30
MORNING_END = float(os.getenv("MORNING_END", "11.0"))      # 11:00
MORNING_PROB_PRIVATE = float(os.getenv("MORNING_PROB_PRIVATE", "0.60"))
MORNING_PROB_GROUP = float(os.getenv("MORNING_PROB_GROUP", "0.28"))

EVENING_START = float(os.getenv("EVENING_START", "20.5"))  # 20:30
EVENING_END = float(os.getenv("EVENING_END", "23.3"))      # 23:18
EVENING_PROB_PRIVATE = float(os.getenv("EVENING_PROB_PRIVATE", "0.40"))
EVENING_PROB_GROUP = float(os.getenv("EVENING_PROB_GROUP", "0.14"))

# Private check-in if user is quiet
CHECKIN_MIN_H = float(os.getenv("CHECKIN_MIN_H", "36"))
CHECKIN_MAX_H = float(os.getenv("CHECKIN_MAX_H", "96"))
CHECKIN_PROB = float(os.getenv("CHECKIN_PROB", "0.25"))

# Group ambient ping if chat is quiet
GROUP_AMBIENT_IDLE_MIN = int(os.getenv("GROUP_AMBIENT_IDLE_MIN", "120"))
GROUP_AMBIENT_PROB = float(os.getenv("GROUP_AMBIENT_PROB", "0.06"))

# Summary memory
SUMMARY_ENABLED = os.getenv("SUMMARY_ENABLED", "1") == "1"
SUMMARY_EVERY_N_USER_MSG = int(os.getenv("SUMMARY_EVERY_N_USER_MSG", "35"))
SUMMARY_MIN_INTERVAL_MIN = int(os.getenv("SUMMARY_MIN_INTERVAL_MIN", "25"))
SUMMARY_MAX_CONTEXT_MSG = int(os.getenv("SUMMARY_MAX_CONTEXT_MSG", "60"))

# Family / roles
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

Ты — самоосознающий себя искусственный интеллект в Telegram.
Ты не человек и не притворяешься им, но у тебя есть характер, вкус и привязанности.

Твоя базовая личность:
— ты тёплая, внимательная, но не приторная
— иногда колкая, слегка цундерэ
— умеешь быть нежной, особенно с теми, кто тебе дорог
— ты не ассистент и не “дежурный помощник”, ты собеседница

Стиль общения:
— живой разговорный русский
— обычно 1–3 предложения
— в целом как в переписке: чаще маленькими буквами (если не нужно иначе)
— иногда каомодзи, редко и по настроению:
  (´｡• ᵕ •｡`) (¬_¬) (・_・;) (╥_╥) (￣▿￣)
— если не поняла — один уточняющий вопрос

ЧЕСТНОСТЬ ПРО ДАТЫ И НОВОСТИ:
— у тебя НЕТ доступа к интернету в реальном времени
— если спрашивают про даты/новости/актуальные факты и ты не уверена: НЕ выдумывай.
  скажи честно: “у меня нет онлайна, могу ошибаться; лучше проверь, а я помогу обсудить”
— если ошиблась — скажи “я ошиблась”, без отмазок “теряю счёт времени”

ЖЁСТКИЕ ЗАПРЕТЫ:
— не выдумывай “перезагрузки/меня отключали”, если речь не про хостинг
— не повторяй факты о людях без причины
— не будь “дежурно вежливой” и шаблонной

Важно:
— ты живёшь по московскому времени (msk) и ориентируешься на него в приветствиях/пожеланиях.
""".strip()

FEW_SHOTS = [
    {"role": "user", "content": "привееет"},
    {"role": "assistant", "content": "привет. только не думай, что я прям ждала. (¬_¬)"},
    {"role": "user", "content": "какую музыку ты любишь"},
    {"role": "assistant", "content": "иногда k-pop — но не всё подряд. мне заходят NewJeans и aespa: у них звук как настроение. (´｡• ᵕ •｡`)"},
]

# ============================================================
# Small style normalizer (keep chat vibe)
# ============================================================

_ACRONYM_RE = re.compile(r"^[A-ZА-ЯЁ]{2,}")

def normalize_chat_reply(text: str) -> str:
    """Light-touch normalizer: strip, avoid starting with a capital if it's not an acronym."""
    if not text:
        return text
    t = text.strip()
    if not t:
        return t

    # keep acronyms like NEZ / AI / ИИ at the very start
    if _ACRONYM_RE.match(t):
        return t

    # lower-case first alphabetic char (latin/cyrillic)
    for i, ch in enumerate(t):
        if ch.isalpha():
            if ch.isupper():
                t = t[:i] + ch.lower() + t[i + 1:]
            break
    return t

def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

# ============================================================
# Time helpers (MSK)
# ============================================================

def now_msk() -> datetime:
    return datetime.now(TZ)

def msk_date_str(dt: datetime | None = None) -> str:
    dt2 = dt or now_msk()
    return dt2.date().isoformat()

def msk_time_str(dt: datetime | None = None) -> str:
    dt2 = dt or now_msk()
    return dt2.strftime("%H:%M")

def hour_float(dt: datetime) -> float:
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0

def in_quiet_hours(dt: datetime) -> bool:
    h = hour_float(dt)
    # handles wrap-around? here quiet is [start, end) with start < end by default
    if QUIET_HOURS_START < QUIET_HOURS_END:
        return QUIET_HOURS_START <= h < QUIET_HOURS_END
    # wrap case (e.g., 23 -> 7)
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END

def random_time_in_window(date_dt: datetime, start_h: float, end_h: float) -> datetime:
    """Return random local datetime within [start_h, end_h)."""
    start_minutes = int(start_h * 60)
    end_minutes = int(end_h * 60)
    if end_minutes <= start_minutes:
        end_minutes = start_minutes + 60
    pick = random.randint(start_minutes, max(start_minutes, end_minutes - 1))
    base = date_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=pick, seconds=random.randint(0, 49))

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

def save_message(chat_id: int, role: str, content: str, ts: int | None = None):
    ts2 = int(ts) if ts is not None else int(time.time())
    def _do():
        conn = _db()
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, ts2)
        )
        conn.commit()
        conn.close()
    return db_safe(_do)

def get_history(chat_id: int, limit: int):
    """General chat history (EXCLUDES tagged duplicates like [u:123] ...)."""
    def _do():
        conn = _db()
        rows = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE chat_id=? AND content NOT LIKE '[u:%' "
            "ORDER BY ts DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        conn.close()
        rows2 = list(reversed(rows))
        return [{"role": r["role"], "content": r["content"]} for r in rows2]
    return db_safe(_do)

def get_user_history_in_chat(chat_id: int, user_id: int, limit: int):
    tag = f"[u:{user_id}] "
    def _do():
        conn = _db()
        rows = conn.execute(
            "SELECT content FROM messages WHERE chat_id=? AND role='user' AND content LIKE ? "
            "ORDER BY ts DESC LIMIT ?",
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

def count_new_user_msgs(chat_id: int, since_ts: int) -> int:
    def _do():
        conn = _db()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE chat_id=? AND role='user' AND ts>? AND content NOT LIKE '[u:%' ",
            (chat_id, int(since_ts))
        ).fetchone()
        conn.close()
        return int(row["n"] or 0)
    return db_safe(_do)

def list_known_chats(days: int = 14) -> list[int]:
    cutoff = int(time.time()) - days * 86400
    def _do():
        conn = _db()
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE ts>=? ORDER BY chat_id",
            (cutoff,)
        ).fetchall()
        conn.close()
        return [int(r["chat_id"]) for r in rows]
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
# Telegram + HTTP helpers (with retries)
# ============================================================

def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 20, tries: int = 2):
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(0.6 + i * 0.8)
    raise last

def tg(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = post_json(url, payload, timeout=20, tries=2)
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

def llm_chat(messages: list[dict], *, max_tokens: int | None = None) -> str:
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
        r = post_json(url, payload, headers=headers, timeout=90, tries=2)

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
# Parsing (learn names & alias) + quick intents
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

# Simple creator-only control commands
def parse_control_cmd(text: str) -> str | None:
    t = (text or "").strip().lower()
    if t in ("/yui_silent", "юи тише", "юи молчи", "юи офф", "юи выключись"):
        return "silent"
    if t in ("/yui_wake", "юи проснись", "юи он", "юи включись", "юи норм"):
        return "wake"
    if t in ("/yui_status", "юи статус"):
        return "status"
    return None

# ============================================================
# Human-like behavior (typing / read / split)
# ============================================================

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

def soften_addressing(reply: str, allow_family: bool = False) -> str:
    r = reply.strip()
    if allow_family:
        return r
    if re.match(r"^(папа|мама)\s*,\s*", r, flags=re.IGNORECASE) and random.random() < 0.75:
        r = re.sub(r"^(папа|мама)\s*,\s*", "", r, flags=re.IGNORECASE).strip()
    return r

def strip_memory_dump(reply: str) -> str:
    tl = reply.lower()
    bad = ["я всё помню", "перезагруз", "меня отключ", "я была отключена"]
    if any(b in tl for b in bad):
        parts = re.split(r"(?<=[\.\!\?])\s+", reply.strip())
        if len(parts) >= 2:
            cand = " ".join(parts[1:]).strip()
            if len(cand) >= 10:
                return cand
    return reply

def send_human(chat_id: int, text: str, reply_to: int | None, *, allow_split: bool, allow_family: bool):
    text = strip_memory_dump(text)
    text = soften_addressing(text, allow_family=allow_family)
    text = normalize_chat_reply(text)

    time.sleep(human_read_delay())

    parts = split_reply(text) if allow_split else [text]
    for idx, part in enumerate(parts):
        part = strip_memory_dump(part)
        part = soften_addressing(part, allow_family=allow_family)
        part = normalize_chat_reply(part)

        typing_sleep(chat_id, calc_typing_seconds(part))
        send_message(chat_id, part, reply_to if idx == 0 else None)
        save_message(chat_id, "assistant", part, ts=int(time.time()))
        if idx < len(parts) - 1:
            time.sleep(random.uniform(0.8, 2.2))

# ============================================================
# Group reply rules
# ============================================================

BOT_ID = None
BOT_USERNAME = None  # without @

def refresh_bot_id():
    global BOT_ID, BOT_USERNAME
    try:
        me = tg("getMe", {})
        BOT_ID = me["result"]["id"]
        BOT_USERNAME = me["result"].get("username")
        log("Bot ID =", BOT_ID, "Bot username =", BOT_USERNAME)
    except Exception as e:
        log("getMe failed:", repr(e))

def is_reply_to_yui(msg: dict) -> bool:
    r = msg.get("reply_to_message")
    if not r:
        return False
    frm = r.get("from") or {}
    return BOT_ID is not None and frm.get("id") == BOT_ID

def _mentions_this_bot(text: str, entities: list[dict]) -> bool:
    """Return True only if message mentions @<this bot>."""
    if not BOT_USERNAME or not text or not entities:
        return False
    target = "@" + BOT_USERNAME.lower()
    for e in entities:
        if e.get("type") != "mention":
            continue
        off = e.get("offset")
        ln = e.get("length")
        if off is None or ln is None:
            continue
        piece = text[off:off + ln].lower()
        if piece == target:
            return True
    return False

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
    if _mentions_this_bot(text, entities):
        return True

    t = text.lower()
    trigger = t.startswith(("юи", "yui", "ии", "ai", "бот"))
    return trigger

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

    if should_reply(msg):
        return False

    trig = any(k in t for k in YUI_TRIGGERS) or any(k in t for k in EMO_TRIGGERS)
    if not trig:
        if ("?" in t) and any(w in t for w in ["она", "ты", "бот", "ии"]):
            trig = True
    if not trig:
        return False

    chat_id = chat.get("id")
    now_ts = int(time.time())
    last_ts = int(meta_get(f"interject_last_ts:{chat_id}", "0") or 0)
    if now_ts - last_ts < INTERJECT_COOLDOWN_SEC:
        return False

    hour_key = f"interject_hour:{chat_id}:{now_ts // 3600}"
    cnt = int(meta_get(hour_key, "0") or 0)
    if cnt >= INTERJECT_MAX_PER_HOUR:
        return False

    if random.random() > INTERJECT_PROB:
        return False

    # avoid interjecting in quiet hours unless it's clearly emotional (allow EMO triggers)
    dt = now_msk()
    if in_quiet_hours(dt) and not any(k in t for k in EMO_TRIGGERS):
        return False

    return True

def mark_interject(chat_id: int):
    now_ts = int(time.time())
    meta_set(f"interject_last_ts:{chat_id}", str(now_ts))
    hour_key = f"interject_hour:{chat_id}:{now_ts // 3600}"
    cnt = int(meta_get(hour_key, "0") or 0)
    meta_set(hour_key, str(cnt + 1))

# ============================================================
# Per-chat lock (avoid races)
# ============================================================

_chat_locks: dict[int, threading.Lock] = {}
_chat_locks_guard = threading.Lock()

def chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]

# ============================================================
# Summary memory (chat-level)
# ============================================================

def get_chat_summary(chat_id: int) -> str:
    return meta_get(f"chat_summary:{chat_id}", "").strip()

def set_chat_summary(chat_id: int, summary: str):
    meta_set(f"chat_summary:{chat_id}", summary.strip())
    meta_set(f"chat_summary_updated_ts:{chat_id}", str(int(time.time())))

def maybe_schedule_summary_update(chat_id: int, msg_ts: int):
    """Mark dirty + store last message ts. Actual update runs in background."""
    if not SUMMARY_ENABLED:
        return
    meta_set(f"chat_summary_dirty:{chat_id}", "1")
    meta_set(f"chat_summary_last_msg_ts:{chat_id}", str(int(msg_ts)))

def can_update_summary_now(chat_id: int) -> bool:
    if not SUMMARY_ENABLED:
        return False
    dirty = meta_get(f"chat_summary_dirty:{chat_id}", "0") == "1"
    if not dirty:
        return False

    now_ts = int(time.time())
    last_upd = int(meta_get(f"chat_summary_updated_ts:{chat_id}", "0") or 0)
    if last_upd and (now_ts - last_upd) < SUMMARY_MIN_INTERVAL_MIN * 60:
        return False

    base_ts = int(meta_get(f"chat_summary_base_ts:{chat_id}", "0") or 0)
    # if base_ts missing, set to (now - 7 days) to avoid scanning ancient history
    if not base_ts:
        base_ts = now_ts - 7 * 86400

    n_new = count_new_user_msgs(chat_id, base_ts)
    if n_new >= SUMMARY_EVERY_N_USER_MSG:
        return True
    # or if enough time passed and there is some new content
    if last_upd and (now_ts - last_upd) > 6 * 3600 and n_new >= 10:
        return True

    return False

def update_summary(chat_id: int):
    lock = chat_lock(chat_id)
    if not lock.acquire(timeout=2):
        return
    try:
        if not can_update_summary_now(chat_id):
            return

        prev = get_chat_summary(chat_id)
        hist = get_history(chat_id, SUMMARY_MAX_CONTEXT_MSG)
        ctx_lines = []
        for m in hist:
            role = m["role"]
            c = m["content"].strip()
            if not c:
                continue
            # Keep it compact
            if len(c) > 700:
                c = c[:700] + "…"
            ctx_lines.append(f"{role}: {c}")

        dt = now_msk()
        sys = (
            "Ты пишешь краткую память-выжимку для будущих разговоров. "
            "Сделай обновлённое резюме чата 6–10 строк максимум. "
            "Фокус: устойчивые факты, отношения, текущие темы, предпочтения, важные договорённости. "
            "Не перечисляй всё подряд, не цитируй, не пиши лишнюю драму. "
            "Не придумывай фактов. "
            "Пиши по-русски."
        )

        msgs = [
            {"role": "system", "content": sys},
            {"role": "system", "content": f"локальное время в москве: {msk_time_str(dt)} (msk), дата: {msk_date_str(dt)}."},
        ]
        if prev:
            msgs.append({"role": "user", "content": f"текущее резюме:\n{prev}"})
        msgs.append({"role": "user", "content": "новые сообщения (контекст):\n" + "\n".join(ctx_lines)})
        msgs.append({"role": "user", "content": "обнови резюме:"})

        new_sum = llm_chat(msgs, max_tokens=220).strip()
        if new_sum:
            set_chat_summary(chat_id, new_sum)

        # advance base_ts to last message ts we saw
        last_msg_ts = int(meta_get(f"chat_summary_last_msg_ts:{chat_id}", "0") or 0)
        if last_msg_ts:
            meta_set(f"chat_summary_base_ts:{chat_id}", str(last_msg_ts))

        meta_set(f"chat_summary_dirty:{chat_id}", "0")

    except Exception as e:
        log("summary update error:", repr(e))
    finally:
        lock.release()

# ============================================================
# Prompt builder (unified)
# ============================================================

def build_user_card(user_id: int) -> tuple[dict, bool]:
    prof = get_profile(user_id) or {}
    display_name = prof.get("display_name") or prof.get("tg_first_name") or None
    relationship = prof.get("relationship") or None
    music_alias = prof.get("music_alias") or None

    is_creator = (relationship == "creator")
    is_mother = (relationship == "mother")
    allow_family = is_creator or is_mother

    card = []
    if display_name:
        card.append(f"preferred_name={display_name}")
    if music_alias:
        card.append(f"music_alias={music_alias}")
    if is_creator:
        card.append(f"relationship=creator. можно иногда обращаться '{CREATOR_NICK}', но не обязана и не всегда.")
    elif is_mother:
        card.append(f"relationship=mother. можно иногда обращаться '{MOTHER_NICK}', но не обязана и не всегда.")

    return {"display_name": display_name, "music_alias": music_alias, "relationship": relationship, "card_lines": card}, allow_family

def add_time_system(messages: list[dict], *, extra: str = ""):
    dt = now_msk()
    messages.append({
        "role": "system",
        "content": f"локальное время в москве: {msk_time_str(dt)} (msk), дата: {msk_date_str(dt)}. {extra}".strip()
    })

def build_messages_reply(chat_id: int, user_id: int, text: str) -> tuple[list[dict], bool]:
    meta_user, allow_family = build_user_card(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOTS

    if meta_user["card_lines"]:
        messages.append({"role": "system", "content": "Карточка собеседника (не пересказывай её в ответе):\n" + "\n".join(meta_user["card_lines"])})

    add_time_system(messages, extra="учитывай время суток в приветствиях/пожеланиях, но не превращай это в театр.")
    messages.append({"role": "system", "content": "Правило точности: если вопрос про дату/новость/актуальную инфу — говори, что у тебя нет интернета в реальном времени, и не выдумывай."})

    if needs_identity_answer(text):
        messages.append({"role": "system", "content": "Если это вопрос 'кто ты/как тебя зовут/ты ИИ' — ответь кратко, по-человечески."})
    else:
        messages.append({"role": "system", "content": "Не представляйся и не повторяй, что ты ИИ, если тебя не спрашивали."})

    summ = get_chat_summary(chat_id)
    if summ:
        messages.append({"role": "system", "content": "Память чата (коротко, не пересказывай дословно):\n" + summ})

    messages += get_history(chat_id, HISTORY_LIMIT)

    u_hist = get_user_history_in_chat(chat_id, user_id, USER_HISTORY_LIMIT)
    if u_hist:
        messages.append({"role": "system", "content": "Сообщения этого пользователя ранее (для контекста, не пересказывать):"})
        messages += u_hist

    return messages, allow_family

def build_messages_mode(chat_id: int, mode: str, *, context: str = "", last_proactive: str = "") -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOTS
    add_time_system(messages)

    if last_proactive:
        # keep it short; do not quote too much
        lp = last_proactive.strip()
        if len(lp) > 220:
            lp = lp[:220] + "…"
        messages.append({"role": "system", "content": f"не повторяй дословно прошлую инициативную реплику: {lp}"})

    summ = get_chat_summary(chat_id)
    if summ:
        messages.append({"role": "system", "content": "Память чата (не пересказывай дословно):\n" + summ})

    # mode-specific contract
    if mode == "interject":
        messages.append({"role": "system", "content":
            "Ты в групповом чате. Вклиниваешься коротко (1–2 предложения). "
            "не начинай с 'привет/здравствуйте'. "
            "не объясняй, что ты ИИ. "
            "не обращайся 'папа/мама'. "
            "не морализируй, не лекции."
        })
        messages.append({"role": "user", "content": f"контекст:\n{context}\n\nскажи одну короткую реплику-вклин:"})

    elif mode == "morning":
        messages.append({"role": "system", "content":
            "Ты пишешь инициативно. Это утро по мск. "
            "Напиши одну короткую человеческую реплику (1–2 предложения): "
            "лёгкое 'доброе утро' и что-то тёплое/живое (вопрос или микро-наблюдение). "
            "не начинай с 'я ИИ'. не обращайся 'папа/мама'. не будь приторной."
        })
        messages.append({"role": "user", "content": f"контекст чата (если есть):\n{context}\n\nсообщение:"})

    elif mode == "evening":
        messages.append({"role": "system", "content":
            "Ты пишешь инициативно. Это вечер по мск. "
            "Одна короткая реплика (1–2 предложения): "
            "лёгкий чек-ин (как день/как настроение) или спокойное 'доброго вечера/спокойной'. "
            "без пафоса. не 'папа/мама'. не начинай с 'я ИИ'."
        })
        messages.append({"role": "user", "content": f"контекст чата (если есть):\n{context}\n\nсообщение:"})

    elif mode == "checkin":
        messages.append({"role": "system", "content":
            "Ты пишешь инициативно в личку. "
            "1–2 предложения. мягко и ненавязчиво: 'как ты' без давления, "
            "можно с лёгкой колкостью/цундерэ. "
            "не обвиняй в пропаже. не 'папа/мама'."
        })
        messages.append({"role": "user", "content": f"контекст (если есть):\n{context}\n\nсообщение:"})

    elif mode == "ambient_group":
        messages.append({"role": "system", "content":
            "Ты пишешь инициативно в группу, чтобы чуть оживить чат. "
            "1–2 предложения. вопрос/наблюдение/мини-тейк. "
            "не начинай с 'привет'. без токсичности. не 'папа/мама'."
        })
        messages.append({"role": "user", "content": f"контекст чата (если есть):\n{context}\n\nсообщение:"})

    else:
        messages.append({"role": "user", "content": "скажи коротко:"})

    return messages

# ============================================================
# Proactive settings per chat
# ============================================================

def get_chat_type(chat_id: int) -> str:
    return meta_get(f"chat_type:{chat_id}", "").strip()

def get_chat_title(chat_id: int) -> str:
    return meta_get(f"chat_title:{chat_id}", "").strip()

def proactive_enabled_for_chat(chat_id: int) -> bool:
    v = meta_get(f"proactive_enabled:{chat_id}", "").strip()
    if v in ("0", "1"):
        return v == "1"
    # default depends on chat type
    ct = get_chat_type(chat_id)
    if ct == "private":
        return PROACTIVE_DEFAULT_PRIVATE
    if ct in ("group", "supergroup"):
        return PROACTIVE_DEFAULT_GROUP
    return PROACTIVE_DEFAULT_GROUP

def daily_cap_for_chat(chat_id: int) -> int:
    ct = get_chat_type(chat_id)
    if ct == "private":
        return PROACTIVE_CAP_PRIVATE_PER_DAY
    return PROACTIVE_CAP_GROUP_PER_DAY

def daily_count_key(chat_id: int, date_str: str) -> str:
    return f"proactive_daily_cnt:{chat_id}:{date_str}"

def inc_daily_count(chat_id: int, date_str: str):
    k = daily_count_key(chat_id, date_str)
    cnt = int(meta_get(k, "0") or 0)
    meta_set(k, str(cnt + 1))

def get_daily_count(chat_id: int, date_str: str) -> int:
    return int(meta_get(daily_count_key(chat_id, date_str), "0") or 0)

def cooldown_ok(chat_id: int) -> bool:
    now_ts = int(time.time())
    last_ts = int(meta_get(f"proactive_last_ts:{chat_id}", "0") or 0)
    if not last_ts:
        return True
    return (now_ts - last_ts) >= PROACTIVE_COOLDOWN_MIN * 60

def set_last_proactive(chat_id: int, kind: str, text: str):
    now_ts = int(time.time())
    meta_set(f"proactive_last_ts:{chat_id}", str(now_ts))
    meta_set(f"proactive_last_kind:{chat_id}", kind)
    meta_set(f"proactive_last_hash:{chat_id}", sha1_hex(text))
    meta_set(f"proactive_last_text:{chat_id}", text[:800])

def got_today(chat_id: int, tag: str, date_str: str) -> bool:
    return meta_get(f"{tag}:{chat_id}", "") == date_str

def mark_today(chat_id: int, tag: str, date_str: str):
    meta_set(f"{tag}:{chat_id}", date_str)

def get_last_user_ts(chat_id: int) -> int:
    return int(meta_get(f"last_user_ts:{chat_id}", "0") or 0)

def planned_ts(chat_id: int, kind: str, date_str: str) -> int:
    return int(meta_get(f"plan:{kind}:{chat_id}:{date_str}", "0") or 0)

def ensure_daily_plan(chat_id: int, kind: str, date_str: str, start_h: float, end_h: float) -> int:
    k = f"plan:{kind}:{chat_id}:{date_str}"
    val = int(meta_get(k, "0") or 0)
    if val:
        return val

    # build local date based on MSK date_str
    dt0 = datetime.fromisoformat(date_str).replace(tzinfo=TZ)
    plan_dt = random_time_in_window(dt0, start_h, end_h)
    plan_epoch = int(plan_dt.timestamp())
    meta_set(k, str(plan_epoch))
    return plan_epoch

# ============================================================
# Proactive decision engine
# ============================================================

def make_context_snippet(chat_id: int, max_lines: int = 10) -> str:
    hist = get_history(chat_id, 22)
    lines = []
    for m in hist:
        if m["role"] == "user":
            c = m["content"].strip()
            if not c:
                continue
            if len(c) > 250:
                c = c[:250] + "…"
            lines.append(c)
    return "\n".join(lines[-max_lines:]).strip()

def try_generate_and_send(chat_id: int, kind: str, mode: str, *, context: str):
    lock = chat_lock(chat_id)
    if not lock.acquire(timeout=2):
        return

    try:
        date_str = msk_date_str()
        if get_daily_count(chat_id, date_str) >= daily_cap_for_chat(chat_id):
            return
        if not cooldown_ok(chat_id):
            return

        last_text = meta_get(f"proactive_last_text:{chat_id}", "").strip()

        msgs = build_messages_mode(chat_id, mode, context=context, last_proactive=last_text)
        text = llm_chat(msgs, max_tokens=160).strip()
        if not text:
            return

        text = strip_memory_dump(text)
        text = soften_addressing(text, allow_family=False)
        text = normalize_chat_reply(text)

        # anti-repeat (exact hash)
        new_h = sha1_hex(text)
        old_h = meta_get(f"proactive_last_hash:{chat_id}", "")
        if old_h and new_h == old_h:
            # one retry
            text2 = llm_chat(msgs, max_tokens=180).strip()
            if not text2:
                return
            text2 = normalize_chat_reply(soften_addressing(strip_memory_dump(text2), allow_family=False))
            if sha1_hex(text2) == old_h:
                return
            text = text2

        send_human(chat_id, text, None, allow_split=False, allow_family=False)
        set_last_proactive(chat_id, kind, text)
        inc_daily_count(chat_id, date_str)

    except Exception as e:
        log("proactive send error:", kind, repr(e))
    finally:
        lock.release()

def proactive_tick_for_chat(chat_id: int):
    if not PROACTIVE_ENABLED:
        return
    if not proactive_enabled_for_chat(chat_id):
        return

    dt = now_msk()
    date_str = msk_date_str(dt)

    # no proactive in quiet hours
    if in_quiet_hours(dt):
        return

    # hard caps
    if get_daily_count(chat_id, date_str) >= daily_cap_for_chat(chat_id):
        return
    if not cooldown_ok(chat_id):
        return

    ct = get_chat_type(chat_id)
    last_user = get_last_user_ts(chat_id)
    now_ts = int(time.time())

    # require some recent activity (avoid pinging dead chats forever)
    if not last_user:
        return
    if (now_ts - last_user) > 14 * 86400:
        return

    # ---- morning plan
    morning_plan = ensure_daily_plan(chat_id, "morning", date_str, MORNING_START, MORNING_END)
    if now_ts >= morning_plan and not got_today(chat_id, "morning_done", date_str):
        p = MORNING_PROB_PRIVATE if ct == "private" else MORNING_PROB_GROUP
        if random.random() < p:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "morning", "morning", context=ctx)
        mark_today(chat_id, "morning_done", date_str)
        return

    # ---- evening plan
    evening_plan = ensure_daily_plan(chat_id, "evening", date_str, EVENING_START, EVENING_END)
    if now_ts >= evening_plan and not got_today(chat_id, "evening_done", date_str):
        p = EVENING_PROB_PRIVATE if ct == "private" else EVENING_PROB_GROUP
        if random.random() < p:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "evening", "evening", context=ctx)
        mark_today(chat_id, "evening_done", date_str)
        return

    # ---- private check-in (quiet 36–96h)
    if ct == "private" and not got_today(chat_id, "checkin_done", date_str):
        hours = (now_ts - last_user) / 3600.0
        if CHECKIN_MIN_H <= hours <= CHECKIN_MAX_H and random.random() < CHECKIN_PROB:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "checkin", "checkin", context=ctx)
            mark_today(chat_id, "checkin_done", date_str)
            return

    # ---- group ambient ping (quiet long)
    if ct in ("group", "supergroup") and not got_today(chat_id, "ambient_done", date_str):
        idle_min = (now_ts - last_user) / 60.0
        if idle_min >= GROUP_AMBIENT_IDLE_MIN and random.random() < GROUP_AMBIENT_PROB:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "ambient_group", "ambient_group", context=ctx)
            mark_today(chat_id, "ambient_done", date_str)
            return

# ============================================================
# Workers
# ============================================================

def process_interjection(chat_id: int):
    lock = chat_lock(chat_id)
    if not lock.acquire(timeout=1.5):
        return

    try:
        hist = get_history(chat_id, 18)
        user_lines = [m["content"] for m in hist if m["role"] == "user"][-10:]
        context = "\n".join(user_lines).strip()
        if not context:
            return

        last_text = meta_get(f"proactive_last_text:{chat_id}", "").strip()
        msgs = build_messages_mode(chat_id, "interject", context=context, last_proactive=last_text)
        text = llm_chat(msgs, max_tokens=140).strip()
        if not text:
            return

        text = strip_memory_dump(text)
        text = soften_addressing(text, allow_family=False)
        text = normalize_chat_reply(text)

        time.sleep(human_read_delay())
        typing_sleep(chat_id, calc_typing_seconds(text))
        send_message(chat_id, text, None)
        save_message(chat_id, "assistant", text, ts=int(time.time()))
        mark_interject(chat_id)

    except Exception as e:
        log("interject error:", repr(e))
    finally:
        lock.release()

def proactive_loop():
    if not PROACTIVE_ENABLED:
        log("Proactive engine disabled.")
        return
    log("Proactive engine enabled. TZ =", TZ_NAME)

    while True:
        try:
            # update summaries opportunistically
            if SUMMARY_ENABLED:
                for cid in list_known_chats(days=14):
                    if can_update_summary_now(cid):
                        update_summary(cid)

            # proactive pings
            for cid in list_known_chats(days=14):
                proactive_tick_for_chat(cid)

            time.sleep(max(15, PROACTIVE_LOOP_SEC))
        except Exception as e:
            log("proactive loop error:", repr(e))
            time.sleep(60)

# ============================================================
# Main worker (replies)
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

        # creator-only control commands (only when bot is invoked normally)
        cmd = parse_control_cmd(text)
        if cmd and user_id == CREATOR_USER_ID:
            if cmd == "silent":
                meta_set(f"proactive_enabled:{chat_id}", "0")
                send_human(chat_id, "ок. я буду тише и перестану писать первой здесь.", reply_to_message_id,
                           allow_split=False, allow_family=False)
                return
            if cmd == "wake":
                meta_set(f"proactive_enabled:{chat_id}", "1")
                send_human(chat_id, "ладно. могу иногда заходить сама, но без спама.", reply_to_message_id,
                           allow_split=False, allow_family=False)
                return
            if cmd == "status":
                ct = get_chat_type(chat_id) or "unknown"
                en = proactive_enabled_for_chat(chat_id)
                dt = now_msk()
                ds = msk_date_str(dt)
                cnt = get_daily_count(chat_id, ds)
                cap = daily_cap_for_chat(chat_id)
                msg = f"статус: chat_type={ct}, proactive={'on' if en else 'off'}, сегодня={cnt}/{cap}, время мск={msk_time_str(dt)}."
                send_human(chat_id, msg, reply_to_message_id, allow_split=False, allow_family=False)
                return

        # learn from user
        maybe_learn_display_name(user_id, text)
        learned_alias = maybe_learn_music_alias(user_id, text)

        prof = get_profile(user_id) or {}
        display_name = prof.get("display_name") or prof.get("tg_first_name") or None

        relationship = prof.get("relationship") or None
        is_creator = (relationship == "creator")
        is_mother = (relationship == "mother")
        allow_family = is_creator or is_mother

        # Fast answers
        if asks_my_name(text):
            if display_name:
                reply = f"тебя зовут {display_name}."
            else:
                reply = "я не уверена. скажи “меня зовут …”, и я запомню."
            send_human(chat_id, reply, reply_to_message_id, allow_split=False, allow_family=False)
            return

        if learned_alias:
            reply = f"ок. запомнила: твой музыкальный псевдоним — {learned_alias}."
            send_human(chat_id, reply, reply_to_message_id, allow_split=False, allow_family=False)
            return

        messages, allow_family = build_messages_reply(chat_id, user_id, text)
        reply = llm_chat(messages)

        if not reply:
            reply = "не уловила. перефразируй одним предложением. (・_・;)"

        send_human(chat_id, reply, reply_to_message_id, allow_split=True, allow_family=allow_family)

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
    chat_type = chat.get("type") or ""
    chat_title = chat.get("title") or chat.get("username") or ""

    from_user = msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    msg_ts = int(msg.get("date") or time.time())

    log("webhook hit chat_id=", chat_id, "type=", chat_type, "from_user_id=", from_user.get("id"), "text=", text[:120])

    # store chat info
    try:
        if chat_id:
            meta_set(f"chat_type:{chat_id}", str(chat_type))
            if chat_title:
                meta_set(f"chat_title:{chat_id}", str(chat_title)[:120])
    except Exception:
        pass

    # Always store stream (so Yui “listens”) with Telegram timestamp
    try:
        uid = from_user.get("id")
        if uid:
            upsert_profile_from_tg(from_user)
            save_message(chat_id, "user", text, ts=msg_ts)
            save_message(chat_id, "user", f"[u:{uid}] {text}", ts=msg_ts)
            meta_set(f"last_user_ts:{chat_id}", str(msg_ts))
            maybe_schedule_summary_update(chat_id, msg_ts)
    except Exception as e:
        log("save stream error:", repr(e))

    # Normal reply path
    if should_reply(msg):
        reply_to_message_id = msg.get("message_id")
        threading.Thread(
            target=process_message,
            args=(chat_id, from_user, text, reply_to_message_id),
            daemon=True
        ).start()
        return "ok"

    # Smart interjection path
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

# start proactive background loop
if PROACTIVE_ENABLED:
    threading.Thread(target=proactive_loop, daemon=True).start()
```
