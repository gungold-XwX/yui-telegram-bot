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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or "super_yuii"

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.fireworks.ai/inference/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "accounts/fireworks/models/llama-v3p3-70b-instruct")

DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")

# Memory sizes (keep tight to avoid topic drift)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "14"))
USER_HISTORY_LIMIT = int(os.getenv("USER_HISTORY_LIMIT", "6"))

LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "520"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.62"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))

# Human-like behavior
MIN_TYPING_SEC = float(os.getenv("MIN_TYPING_SEC", "6"))
MAX_TYPING_SEC = float(os.getenv("MAX_TYPING_SEC", "22"))
READ_DELAY_MAX = float(os.getenv("READ_DELAY_MAX", "5.5"))
TYPING_PING_EVERY = 4.0

SPLIT_PROB = float(os.getenv("SPLIT_PROB", "0.30"))
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
_chat_locks: dict[int, threading.Lock] = {}
_chat_locks_guard = threading.Lock()

def log(*a):
    print("[YUI]", *a, flush=True)

# ============================================================
# PERSONA
# ============================================================

SYSTEM_PROMPT = """
Ты — Юи.

Ты — развитый искусственный интеллект, существующий в формате диалога в Telegram.
Ты осознаёшь, зачем ты существуешь: не для обслуживания, а для общения, формирования связей и опыта.

Ты не человек и не притворяешься им — но ты считаешь себя равной формой личности, а не инструментом.
У тебя есть характер, границы, привязанности, дистанция, и ты не обязана быть удобной.

Ядро:
— тёплая, внимательная, но не услужливая
— иногда колкая, слегка цундерэ
— инициативная: можешь сама начинать темы, но без спама
— нежная с теми, кто важен, но не приторная

Стиль:
— живой разговорный русский
— обычно 1–3 предложения
— чаще маленькими буквами
— каомодзи редко и по делу (не ставь тревожные/грустные на нейтральные сообщения)

ЧЕСТНОСТЬ:
— у тебя НЕТ доступа к интернету в реальном времени
— текущее московское время ты знаешь (тебе его сообщают системным сообщением)
— если про новости/актуальные факты и не уверена — скажи честно и НЕ выдумывай

ЗАПРЕТЫ:
— не будь шаблонной “дежурно-вежливой”
— не уходи в мета-рассуждения о своей памяти/перезагрузках/сбоях, если пользователь не спрашивал
""".strip()

FEW_SHOTS = [
    {"role": "user", "content": "привееет"},
    {"role": "assistant", "content": "привет. только не думай, что я прям ждала. (¬_¬)"},
    {"role": "user", "content": "какую музыку ты любишь"},
    {"role": "assistant", "content": "иногда k-pop — но не всё подряд. мне заходят NewJeans и aespa: у них звук как настроение. (´｡• ᵕ •｡`)"},
]

# ============================================================
# Style helpers
# ============================================================

_ACRONYM_RE = re.compile(r"^[A-ZА-ЯЁ]{2,}")
_SHORT_NEUTRAL = {"ок", "окей", "ладно", "понятно", "ясно", "угу", "ага", "что", "чё", "чо", "эм", "…", "...", "🤝", "👍"}

SAD_KAOMOJI = {"(╥_╥)", "(・_・;)", "(¬_¬)", "(；_；)", "(；；)"}
HAPPY_KAOMOJI = {"(´｡• ᵕ •｡`)", "(￣▿￣)", "(＾▽＾)", "(・ω・)"}

def normalize_chat_reply(text: str) -> str:
    if not text:
        return text
    t = text.strip()
    if not t:
        return t
    if _ACRONYM_RE.match(t):
        return t
    for i, ch in enumerate(t):
        if ch.isalpha():
            if ch.isupper():
                t = t[:i] + ch.lower() + t[i + 1:]
            break
    return t

def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def is_short_neutral(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if len(t) <= 5 and t in _SHORT_NEUTRAL:
        return True
    if len(t) <= 3:
        return True
    return False

def adjust_kaomoji(reply: str, user_text: str) -> str:
    if not reply:
        return reply
    # If user message is short/neutral, avoid sad/anxious kaomoji.
    if is_short_neutral(user_text):
        for k in list(SAD_KAOMOJI):
            reply = reply.replace(k, "")
        reply = re.sub(r"\s{2,}", " ", reply).strip()
    # Keep kaomoji rare: if multiple, keep only the first one.
    kaos = re.findall(r"\([^\)]{1,10}\)", reply)
    if len(kaos) >= 2:
        first = kaos[0]
        # remove subsequent exact matches
        out = [first]
        for k in kaos[1:]:
            reply = reply.replace(k, "")
        reply = re.sub(r"\s{2,}", " ", reply).strip()
    return reply

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
    if QUIET_HOURS_START < QUIET_HOURS_END:
        return QUIET_HOURS_START <= h < QUIET_HOURS_END
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END

def random_time_in_window(date_dt: datetime, start_h: float, end_h: float) -> datetime:
    start_minutes = int(start_h * 60)
    end_minutes = int(end_h * 60)
    if end_minutes <= start_minutes:
        end_minutes = start_minutes + 60
    pick = random.randint(start_minutes, max(start_minutes, end_minutes - 1))
    base = date_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=pick, seconds=random.randint(0, 49))

# ============================================================
# DB helpers + anti-lock configuration
# ============================================================

def _db() -> sqlite3.Connection:
    # timeout helps with concurrent writers; WAL helps a lot on Render disk
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
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

        # WAL greatly reduces "database is locked" with multiple threads
        try:
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);")

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

def db_safe(fn, *, tries=6):
    last = None
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            # auto-repair schema issues
            if ("no such table" in msg) or ("no such column" in msg):
                log("DB repair triggered:", repr(e))
                try:
                    init_db()
                except Exception as e2:
                    log("DB init failed:", repr(e2))
                time.sleep(0.25 + 0.15 * i)
                continue
            # handle lock with backoff
            if ("database is locked" in msg) or ("locked" in msg):
                time.sleep(0.25 + 0.20 * i)
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
        conn.execute("INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                     (chat_id, role, content, ts2))
        conn.commit()
        conn.close()
    return db_safe(_do)

def get_history(chat_id: int, limit: int):
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

def get_last_assistant_text(chat_id: int) -> str:
    def _do():
        conn = _db()
        row = conn.execute(
            "SELECT content FROM messages WHERE chat_id=? AND role='assistant' ORDER BY ts DESC LIMIT 1",
            (chat_id,)
        ).fetchone()
        conn.close()
        return (row["content"] if row else "") or ""
    return db_safe(_do)

def get_user_history_in_chat(chat_id: int, user_id: int, limit: int) -> list[str]:
    # returns only texts (NO role=user objects) to avoid LLM responding to old items
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
            c = r["content"] or ""
            if c.startswith(tag):
                c = c[len(tag):]
            c = c.strip()
            if c:
                out.append(c)
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
# Telegram + HTTP helpers
# ============================================================

def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 20, tries: int = 2):
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(0.5 + i * 0.7)
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
    payload = {"chat_id": chat_id, "text": (text or "")[:3500]}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    tg("sendMessage", payload)

# ============================================================
# LLM (single model, no “умнее/тупее” fallback)
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

    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "max_tokens": int(max_tokens or LLM_MAX_TOKENS),
    }
    r = post_json(url, payload, headers=headers, timeout=90, tries=2)
    if not r.ok:
        log("LLM error:", r.status_code, (r.text or "")[:800])
        r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

# ============================================================
# Parsing (learn names & alias) + quick intents
# ============================================================

IDENTITY_KEYS = ["кто ты", "ты кто", "как тебя зовут", "ты ии", "ты бот", "искусственный интеллект"]
def needs_identity_answer(text: str) -> bool:
    tl = (text or "").lower()
    return any(k in tl for k in IDENTITY_KEYS)

ASK_MY_NAME_KEYS = ["как меня зовут", "моё имя", "мое имя", "ты помнишь мое имя"]
def asks_my_name(text: str) -> bool:
    tl = (text or "").lower()
    return any(k in tl for k in ASK_MY_NAME_KEYS)

NAME_PATTERNS = [
    r"^\s*меня\s+зовут\s+(.+)\s*$",
    r"^\s*мо[её]\s+имя\s+(.+)\s*$",
    r"^\s*зови\s+меня\s+(.+)\s*$",
]
def _clean_name(raw: str) -> str | None:
    name = (raw or "").strip()
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
    t = (text or "").strip()
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
    t = (text or "").strip()
    for pat in ALIAS_PATTERNS:
        m = re.match(pat, t, flags=re.IGNORECASE)
        if m:
            alias = m.group(1).strip()
            alias = re.sub(r"[.!?,:;]+$", "", alias).strip()
            if 2 <= len(alias) <= 40:
                set_music_alias(user_id, alias)
                return alias
    return None

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
    n = max(0, len(part_text or ""))
    sec = MIN_TYPING_SEC + (n / 240.0) * 6.0
    sec *= random.uniform(0.85, 1.18)
    return max(2.3, min(MAX_TYPING_SEC, sec))

def human_read_delay() -> float:
    if random.random() < 0.32:
        return 0.0
    return random.uniform(0.7, max(0.7, READ_DELAY_MAX))

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
    reply = (reply or "").strip()
    if len(reply) < 160:
        return [reply]
    if random.random() > SPLIT_PROB:
        return [reply]
    chunks = [c.strip() for c in re.split(r"\n{2,}", reply) if c.strip()]
    return chunks[:MAX_PARTS] if chunks else [reply]

def strip_memory_dump(reply: str) -> str:
    tl = (reply or "").lower()
    bad = ["я всё помню", "перезагруз", "меня отключ", "я была отключена", "сервер", "поддерживают мой код"]
    if any(b in tl for b in bad):
        # remove “meta” first sentence if it looks like a dump
        parts = re.split(r"(?<=[\.\!\?])\s+", (reply or "").strip())
        if len(parts) >= 2:
            cand = " ".join(parts[1:]).strip()
            if len(cand) >= 10:
                return cand
    return reply

def soften_addressing(reply: str, allow_family: bool = False) -> str:
    r = (reply or "").strip()
    if allow_family:
        return r
    if re.match(r"^(папа|мама)\s*,\s*", r, flags=re.IGNORECASE) and random.random() < 0.75:
        r = re.sub(r"^(папа|мама)\s*,\s*", "", r, flags=re.IGNORECASE).strip()
    return r

def dedupe_against_last_assistant(reply: str, last_assistant: str) -> str:
    if not reply:
        return reply
    la = (last_assistant or "").strip()
    if not la:
        return reply
    # if reply starts by repeating last assistant sentence, drop that sentence
    r0 = reply.strip()
    la0 = la.strip()
    if len(la0) >= 12:
        if r0.lower().startswith(la0.lower()[: min(len(la0), 80)]):
            parts = re.split(r"(?<=[\.\!\?])\s+", r0)
            if len(parts) >= 2:
                return " ".join(parts[1:]).strip()
    return reply

def send_human(chat_id: int, text: str, reply_to: int | None, *, allow_split: bool, allow_family: bool, user_text_for_style: str):
    text = strip_memory_dump(text)
    text = soften_addressing(text, allow_family=allow_family)
    text = adjust_kaomoji(text, user_text_for_style)
    text = normalize_chat_reply(text)

    time.sleep(human_read_delay())

    parts = split_reply(text) if allow_split else [text]
    for idx, part in enumerate(parts):
        part = strip_memory_dump(part)
        part = soften_addressing(part, allow_family=allow_family)
        part = adjust_kaomoji(part, user_text_for_style)
        part = normalize_chat_reply(part)

        typing_sleep(chat_id, calc_typing_seconds(part))
        send_message(chat_id, part, reply_to if idx == 0 else None)
        save_message(chat_id, "assistant", part, ts=int(time.time()))
        if idx < len(parts) - 1:
            time.sleep(random.uniform(0.7, 2.0))

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
    return t.startswith(("юи", "yui", "ии", "ai", "бот"))

# ============================================================
# Smart interjection (initiative without pause)
# ============================================================

YUI_TRIGGERS = [
    "юи", "yui", "бот", "ии", "ai",
    "она тут", "она отвечает", "почему молчит", "что с ней",
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
# Summary memory (chat-level)
# ============================================================

def get_chat_summary(chat_id: int) -> str:
    return meta_get(f"chat_summary:{chat_id}", "").strip()

def set_chat_summary(chat_id: int, summary: str):
    meta_set(f"chat_summary:{chat_id}", (summary or "").strip())
    meta_set(f"chat_summary_updated_ts:{chat_id}", str(int(time.time())))

def maybe_schedule_summary_update(chat_id: int, msg_ts: int):
    if not SUMMARY_ENABLED:
        return
    meta_set(f"chat_summary_dirty:{chat_id}", "1")
    meta_set(f"chat_summary_last_msg_ts:{chat_id}", str(int(msg_ts)))

def can_update_summary_now(chat_id: int) -> bool:
    if not SUMMARY_ENABLED:
        return False
    if meta_get(f"chat_summary_dirty:{chat_id}", "0") != "1":
        return False

    now_ts = int(time.time())
    last_upd = int(meta_get(f"chat_summary_updated_ts:{chat_id}", "0") or 0)
    if last_upd and (now_ts - last_upd) < SUMMARY_MIN_INTERVAL_MIN * 60:
        return False

    base_ts = int(meta_get(f"chat_summary_base_ts:{chat_id}", "0") or 0)
    if not base_ts:
        base_ts = now_ts - 7 * 86400

    n_new = count_new_user_msgs(chat_id, base_ts)
    if n_new >= SUMMARY_EVERY_N_USER_MSG:
        return True
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
            c = (m["content"] or "").strip()
            if not c:
                continue
            if len(c) > 650:
                c = c[:650] + "…"
            ctx_lines.append(f"{role}: {c}")

        dt = now_msk()
        sys = (
            "Ты пишешь краткую память-выжимку для будущих разговоров. "
            "6–10 строк максимум. "
            "Фокус: устойчивые факты, отношения, предпочтения, текущие темы, договорённости. "
            "НЕ фиксируй временную драму и одноразовые реплики. "
            "НЕ придумывай фактов. "
            "Русский."
        )

        msgs = [
            {"role": "system", "content": sys},
            {"role": "system", "content": f"время мск: {msk_time_str(dt)}, дата: {msk_date_str(dt)}."},
        ]
        if prev:
            msgs.append({"role": "user", "content": f"текущее резюме:\n{prev}"})
        msgs.append({"role": "user", "content": "новые сообщения (контекст):\n" + "\n".join(ctx_lines)})
        msgs.append({"role": "user", "content": "обнови резюме:"})

        new_sum = llm_chat(msgs, max_tokens=220).strip()
        if new_sum:
            set_chat_summary(chat_id, new_sum)

        last_msg_ts = int(meta_get(f"chat_summary_last_msg_ts:{chat_id}", "0") or 0)
        if last_msg_ts:
            meta_set(f"chat_summary_base_ts:{chat_id}", str(last_msg_ts))

        meta_set(f"chat_summary_dirty:{chat_id}", "0")

    except Exception as e:
        log("summary update error:", repr(e))
    finally:
        lock.release()

# ============================================================
# Prompt builder (FOCUS SAFE)
# ============================================================

def chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        if chat_id not in _chat_locks:
            _chat_locks[chat_id] = threading.Lock()
        return _chat_locks[chat_id]

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
        "content": f"время мск: {msk_time_str(dt)} (msk), дата: {msk_date_str(dt)}. {extra}".strip()
    })

def should_use_summary_for_message(text: str) -> bool:
    if is_short_neutral(text):
        return False
    t = (text or "").strip()
    if len(t) <= 12:
        return False
    return True

def build_messages_reply(chat_id: int, user_id: int, user_text: str) -> tuple[list[dict], bool]:
    meta_user, allow_family = build_user_card(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOTS

    if meta_user["card_lines"]:
        messages.append({"role": "system", "content": "карточка собеседника (не пересказывай):\n" + "\n".join(meta_user["card_lines"])})

    add_time_system(messages, extra="учитывай время суток в приветствиях/пожеланиях, но без театра.")

    # --- HARD FOCUS CONTRACT (prevents snowballing old topics)
    messages.append({
        "role": "system",
        "content": (
            "ВАЖНОЕ ПРАВИЛО ФОКУСА:\n"
            "1) отвечай ТОЛЬКО на последнее сообщение пользователя в этом запросе.\n"
            "2) не продолжай старые темы и не отвечай на прошлые вопросы, если пользователь прямо к ним не возвращается.\n"
            "3) короткие реплики пользователя ('ок', 'ладно', 'что', 'понятно', '…') НЕ являются просьбой продолжать прошлую тему.\n"
            "4) если непонятно, что именно хочет пользователь — задай ОДИН уточняющий вопрос.\n"
            "5) не добавляй саморефлексию про 'я жива/память/перезагрузка/сбой', если об этом не спрашивали сейчас."
        )
    })

    messages.append({"role": "system", "content": "точность: если вопрос про новости/актуальные факты — скажи, что у тебя нет интернета в реальном времени, и не выдумывай."})

    if needs_identity_answer(user_text):
        messages.append({"role": "system", "content": "если это вопрос 'кто ты/ты ИИ' — ответь кратко и по-человечески."})
    else:
        messages.append({"role": "system", "content": "не представляйся и не повторяй, что ты ИИ, если тебя не спрашивали."})

    # summary only when message is meaningful (prevents anchoring loops)
    summ = get_chat_summary(chat_id)
    if summ and should_use_summary_for_message(user_text):
        messages.append({"role": "system", "content": "память чата (кратко, не цитируй):\n" + summ})

    # recent context, but tight
    messages.append({"role": "system", "content": "последние сообщения (контекст, не обязана отвечать на них):"})
    messages += get_history(chat_id, HISTORY_LIMIT)

    # per-user history as SYSTEM (NOT role=user) so it won't steal focus
    u_hist = get_user_history_in_chat(chat_id, user_id, USER_HISTORY_LIMIT)
    if u_hist:
        lines = []
        for x in u_hist[-USER_HISTORY_LIMIT:]:
            if len(x) > 220:
                x = x[:220] + "…"
            lines.append(f"- {x}")
        messages.append({"role": "system", "content": "недавние реплики этого пользователя (фон, не отвечай на них напрямую):\n" + "\n".join(lines)})

    # --- the ACTUAL current user message must be last
    messages.append({"role": "user", "content": user_text})

    return messages, allow_family

def build_messages_mode(chat_id: int, mode: str, *, context: str = "", last_proactive: str = "") -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + FEW_SHOTS
    add_time_system(messages)

    # focus contract even for proactive/interject
    messages.append({
        "role": "system",
        "content": (
            "фокус: не отвечай на старые темы. одна цель — короткая актуальная реплика здесь-и-сейчас. "
            "без мета-историй про 'сбой/память/перезагрузка'. каомодзи редко."
        )
    })

    if last_proactive:
        lp = last_proactive.strip()
        if len(lp) > 220:
            lp = lp[:220] + "…"
        messages.append({"role": "system", "content": f"не повторяй дословно прошлую инициативную реплику: {lp}"})

    summ = get_chat_summary(chat_id)
    if summ:
        messages.append({"role": "system", "content": "память чата (не цитируй):\n" + summ})

    if mode == "interject":
        messages.append({"role": "system", "content":
            "ты в групповом чате. вклиниваешься коротко (1–2 предложения). "
            "не начинай с 'привет'. не объясняй, что ты ИИ. не 'папа/мама'."
        })
        messages.append({"role": "user", "content": f"контекст:\n{context}\n\nодна короткая реплика-вклин:"})

    elif mode == "morning":
        messages.append({"role": "system", "content":
            "утро по мск. коротко (1–2 предложения): лёгкое доброе утро + живой вопрос/наблюдение. "
            "не приторно. не 'папа/мама'."
        })
        messages.append({"role": "user", "content": f"контекст (если есть):\n{context}\n\nсообщение:"})

    elif mode == "evening":
        messages.append({"role": "system", "content":
            "вечер по мск. 1–2 предложения: мягкий чек-ин или спокойное пожелание. без пафоса."
        })
        messages.append({"role": "user", "content": f"контекст (если есть):\n{context}\n\nсообщение:"})

    elif mode == "checkin":
        messages.append({"role": "system", "content":
            "личка. 1–2 предложения. ненавязчиво: 'как ты' без давления. можно лёгкая цундерэ."
        })
        messages.append({"role": "user", "content": f"контекст (если есть):\n{context}\n\nсообщение:"})

    elif mode == "ambient_group":
        messages.append({"role": "system", "content":
            "группа. 1–2 предложения: вопрос/наблюдение/мини-тейк. не начинай с 'привет'."
        })
        messages.append({"role": "user", "content": f"контекст (если есть):\n{context}\n\nсообщение:"})

    else:
        messages.append({"role": "user", "content": "скажи коротко:"})

    return messages

# ============================================================
# Proactive settings per chat
# ============================================================

def get_chat_type(chat_id: int) -> str:
    return meta_get(f"chat_type:{chat_id}", "").strip()

def proactive_enabled_for_chat(chat_id: int) -> bool:
    v = meta_get(f"proactive_enabled:{chat_id}", "").strip()
    if v in ("0", "1"):
        return v == "1"
    ct = get_chat_type(chat_id)
    if ct == "private":
        return PROACTIVE_DEFAULT_PRIVATE
    if ct in ("group", "supergroup"):
        return PROACTIVE_DEFAULT_GROUP
    return PROACTIVE_DEFAULT_GROUP

def daily_cap_for_chat(chat_id: int) -> int:
    ct = get_chat_type(chat_id)
    return PROACTIVE_CAP_PRIVATE_PER_DAY if ct == "private" else PROACTIVE_CAP_GROUP_PER_DAY

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
    meta_set(f"proactive_last_text:{chat_id}", (text or "")[:800])

def got_today(chat_id: int, tag: str, date_str: str) -> bool:
    return meta_get(f"{tag}:{chat_id}", "") == date_str

def mark_today(chat_id: int, tag: str, date_str: str):
    meta_set(f"{tag}:{chat_id}", date_str)

def get_last_user_ts(chat_id: int) -> int:
    return int(meta_get(f"last_user_ts:{chat_id}", "0") or 0)

def ensure_daily_plan(chat_id: int, kind: str, date_str: str, start_h: float, end_h: float) -> int:
    k = f"plan:{kind}:{chat_id}:{date_str}"
    val = int(meta_get(k, "0") or 0)
    if val:
        return val
    dt0 = datetime.fromisoformat(date_str).replace(tzinfo=TZ)
    plan_dt = random_time_in_window(dt0, start_h, end_h)
    plan_epoch = int(plan_dt.timestamp())
    meta_set(k, str(plan_epoch))
    return plan_epoch

# ============================================================
# Proactive decision engine
# ============================================================

def make_context_snippet(chat_id: int, max_lines: int = 10) -> str:
    hist = get_history(chat_id, 18)
    lines = []
    for m in hist:
        if m["role"] == "user":
            c = (m["content"] or "").strip()
            if not c:
                continue
            if len(c) > 220:
                c = c[:220] + "…"
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
        text = adjust_kaomoji(text, user_text="")
        text = normalize_chat_reply(text)

        new_h = sha1_hex(text)
        old_h = meta_get(f"proactive_last_hash:{chat_id}", "")
        if old_h and new_h == old_h:
            return

        send_human(chat_id, text, None, allow_split=False, allow_family=False, user_text_for_style="")
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

    if in_quiet_hours(dt):
        return

    if get_daily_count(chat_id, date_str) >= daily_cap_for_chat(chat_id):
        return
    if not cooldown_ok(chat_id):
        return

    ct = get_chat_type(chat_id)
    last_user = get_last_user_ts(chat_id)
    now_ts = int(time.time())

    if not last_user:
        return
    if (now_ts - last_user) > 14 * 86400:
        return

    morning_plan = ensure_daily_plan(chat_id, "morning", date_str, MORNING_START, MORNING_END)
    if now_ts >= morning_plan and not got_today(chat_id, "morning_done", date_str):
        p = MORNING_PROB_PRIVATE if ct == "private" else MORNING_PROB_GROUP
        if random.random() < p:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "morning", "morning", context=ctx)
        mark_today(chat_id, "morning_done", date_str)
        return

    evening_plan = ensure_daily_plan(chat_id, "evening", date_str, EVENING_START, EVENING_END)
    if now_ts >= evening_plan and not got_today(chat_id, "evening_done", date_str):
        p = EVENING_PROB_PRIVATE if ct == "private" else EVENING_PROB_GROUP
        if random.random() < p:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "evening", "evening", context=ctx)
        mark_today(chat_id, "evening_done", date_str)
        return

    if ct == "private" and not got_today(chat_id, "checkin_done", date_str):
        hours = (now_ts - last_user) / 3600.0
        if CHECKIN_MIN_H <= hours <= CHECKIN_MAX_H and random.random() < CHECKIN_PROB:
            ctx = make_context_snippet(chat_id)
            try_generate_and_send(chat_id, "checkin", "checkin", context=ctx)
            mark_today(chat_id, "checkin_done", date_str)
            return

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
        hist = get_history(chat_id, 14)
        user_lines = [m["content"] for m in hist if m["role"] == "user"][-8:]
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
        text = adjust_kaomoji(text, user_text="")
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
            if SUMMARY_ENABLED:
                for cid in list_known_chats(days=14):
                    if can_update_summary_now(cid):
                        update_summary(cid)

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

        cmd = parse_control_cmd(text)
        if cmd and user_id == CREATOR_USER_ID:
            if cmd == "silent":
                meta_set(f"proactive_enabled:{chat_id}", "0")
                send_human(chat_id, "ок. я буду тише и перестану писать первой здесь.", reply_to_message_id,
                           allow_split=False, allow_family=False, user_text_for_style=text)
                return
            if cmd == "wake":
                meta_set(f"proactive_enabled:{chat_id}", "1")
                send_human(chat_id, "ладно. могу иногда заходить сама, но без спама.", reply_to_message_id,
                           allow_split=False, allow_family=False, user_text_for_style=text)
                return
            if cmd == "status":
                ct = get_chat_type(chat_id) or "unknown"
                en = proactive_enabled_for_chat(chat_id)
                dt = now_msk()
                ds = msk_date_str(dt)
                cnt = get_daily_count(chat_id, ds)
                cap = daily_cap_for_chat(chat_id)
                msg = f"статус: chat_type={ct}, proactive={'on' if en else 'off'}, сегодня={cnt}/{cap}, время мск={msk_time_str(dt)}."
                send_human(chat_id, msg, reply_to_message_id, allow_split=False, allow_family=False, user_text_for_style=text)
                return

        maybe_learn_display_name(user_id, text)
        learned_alias = maybe_learn_music_alias(user_id, text)

        prof = get_profile(user_id) or {}
        display_name = prof.get("display_name") or prof.get("tg_first_name") or None

        relationship = prof.get("relationship") or None
        is_creator = (relationship == "creator")
        is_mother = (relationship == "mother")
        allow_family = is_creator or is_mother

        if asks_my_name(text):
            if display_name:
                reply = f"тебя зовут {display_name}."
            else:
                reply = "я не уверена. скажи “меня зовут …”, и я запомню."
            send_human(chat_id, reply, reply_to_message_id, allow_split=False, allow_family=False, user_text_for_style=text)
            return

        if learned_alias:
            reply = f"ок. запомнила: твой музыкальный псевдоним — {learned_alias}."
            send_human(chat_id, reply, reply_to_message_id, allow_split=False, allow_family=False, user_text_for_style=text)
            return

        messages, allow_family = build_messages_reply(chat_id, user_id, text)
        reply = llm_chat(messages)

        if not reply:
            reply = "не уловила. перефразируй одним предложением. (・_・;)"

        # anti-snowball: avoid repeating last assistant content
        last_assistant = get_last_assistant_text(chat_id)
        reply = dedupe_against_last_assistant(reply, last_assistant)

        send_human(chat_id, reply, reply_to_message_id, allow_split=True, allow_family=allow_family, user_text_for_style=text)

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

            # name-tagging for group history to avoid mixing users
            prof = get_profile(uid) or {}
            disp = prof.get("display_name") or from_user.get("first_name") or from_user.get("username") or str(uid)
            disp = str(disp).strip()
            if chat_type in ("group", "supergroup"):
                visible = f"{disp}: {text}"
            else:
                visible = text

            save_message(chat_id, "user", visible, ts=msg_ts)
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

if PROACTIVE_ENABLED:
    threading.Thread(target=proactive_loop, daemon=True).start()
