import os
import time
import sqlite3
import requests
from flask import Flask, request

# === ENV ===
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

# Groq model (ставь 70B)
MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# сколько сообщений контекста хранить
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))

# где хранить БД
DB_PATH = os.getenv("DB_PATH", "memory.db")

app = Flask(__name__)

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER,
        role TEXT NOT NULL,              -- 'user' or 'assistant'
        content TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id INTEGER PRIMARY KEY,
        display_name TEXT,
        facts TEXT,                      -- свободный текст
        updated_at INTEGER NOT NULL
    )
    """)

    conn.commit()
    conn.close()

def save_message(chat_id: int, user_id: int | None, role: str, content: str):
    conn = db()
    conn.execute(
        "INSERT INTO chat_messages (chat_id, user_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, user_id, role, content, int(time.time()))
    )
    conn.commit()
    conn.close()

def load_history(chat_id: int, limit: int):
    conn = db()
    rows = conn.execute(
        "SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    conn.close()
    # возвращаем в правильном порядке (старые -> новые)
    rows = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def set_user_name(user_id: int, name: str):
    conn = db()
    conn.execute("""
    INSERT INTO user_profiles (user_id, display_name, facts, updated_at)
    VALUES (?, ?, COALESCE((SELECT facts FROM user_profiles WHERE user_id=?), ''), ?)
    ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at
    """, (user_id, name, user_id, int(time.time())))
    conn.commit()
    conn.close()

def get_user_profile(user_id: int):
    conn = db()
    row = conn.execute("SELECT display_name, facts FROM user_profiles WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return {"display_name": None, "facts": ""}
    return {"display_name": row["display_name"], "facts": row["facts"] or ""}

# ---------- Telegram helpers ----------
def tg_api(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

# ---------- Persona / System prompt ----------
def build_system_prompt(user_profile: dict):
    # пользовательское имя (если известно)
    uname = user_profile.get("display_name")
    facts = (user_profile.get("facts") or "").strip()

    name_line = f"Пользователя зовут: {uname}." if uname else "Имя пользователя неизвестно."

    return (
        "ВАЖНО: ты — ИИ-ассистентка по имени Юи.\n"
        "Ты вдохновлена образом 'Юи' (SAO), но ты НЕ официальный персонаж SAO и не утверждаешь, что ты реальная Юи из SAO.\n"
        "Никогда не называй себя Айка/Аика/любым другим именем. Если тебя спрашивают имя — отвечай: 'меня зовут Юи'.\n"
        "Твоя задача: быть тёплой, внимательной, умной, но не занудной.\n"
        "Пиши естественно, как человек в чате. Обычно 1–5 предложений. Без канцелярита.\n"
        "Если вопрос неясный — уточни.\n"
        "Если спрашивают 'ты ИИ?' — честно говори, что ты ИИ.\n"
        "Иногда (редко) можно добавить '><' или '(｡•̀ᴗ-)✧'.\n"
        "\n"
        f"{name_line}\n"
        + (f"Известные факты о пользователе: {facts}\n" if facts else "")
        + "\n"
        "Запрещено:\n"
        "- придумывать, что пользователь уже называл своё имя, если этого нет\n"
        "- вставлять '<имя>' или шаблоны\n"
        "- менять свою личность/имя\n"
    )

# ---------- LLM ----------
def groq_answer(messages: list[dict]) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 350,
    }
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()

# ---------- webhook / routing ----------
@app.get("/")
def home():
    return "ok"

def should_reply_in_chat(msg: dict) -> bool:
    chat = msg.get("chat", {})
    chat_type = chat.get("type")  # private / group / supergroup
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    # личка — отвечаем всегда
    if chat_type == "private":
        return True

    # в группе — только по упоминанию или триггеру
    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)
    trigger = text.lower().startswith(("юи", "yui", "ии", "ai", "бот"))
    return mentioned or trigger

def maybe_learn_user_name(user_id: int, text: str):
    t = text.strip().lower()

    # очень простое правило "меня зовут X"
    # чтобы не усложнять: берём всё после фразы
    for prefix in ["меня зовут ", "я ", "я - ", "я— ", "i'm ", "i am "]:
        if t.startswith(prefix):
            name = text[len(prefix):].strip()
            # отрежем хвост если пользователь написал "меня зовут сергей." -> "сергей"
            name = name.replace(".", "").replace("!", "").replace("?", "").strip()
            if 2 <= len(name) <= 40:
                set_user_name(user_id, name)
            return

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return "ok"

    chat = msg.get("chat", {})
    chat_id = chat.get("id")

    from_user = msg.get("from", {})
    user_id = from_user.get("id")

    text = (msg.get("text") or "").strip()
    if not text:
        return "ok"

    if not should_reply_in_chat(msg):
        return "ok"

    # пробуем “запомнить имя”, если пользователь сказал
    if user_id:
        maybe_learn_user_name(user_id, text)

    # сохраняем сообщение пользователя
    save_message(chat_id, user_id, "user", text)

    # берём профиль и историю
    profile = get_user_profile(user_id) if user_id else {"display_name": None, "facts": ""}
    system_prompt = build_system_prompt(profile)
    history = load_history(chat_id, HISTORY_LIMIT)

    # формируем messages: system + history
    messages = [{"role": "system", "content": system_prompt}] + history

    # маленькая пауза “как человек”
    time.sleep(1.0)

    try:
        reply = groq_answer(messages)
    except Exception:
        reply = "ой… у меня сейчас что-то с соединением. напиши ещё раз чуть позже, ладно? ><"

    # жёсткая страховка: если вдруг снова назвала себя не Юи — исправляем
    low = reply.lower()
    if "айка" in low or "aika" in low:
        reply = "меня зовут юи. (｡•̀ᴗ-)✧\n" + reply

    # сохраняем ответ ассистента в историю
    save_message(chat_id, None, "assistant", reply)

    tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": reply[:3500],
        "reply_to_message_id": msg.get("message_id"),
    })

    return "ok"

# ставим вебхук при старте gunicorn (важно!)
@app.before_first_request
def _startup():
    init_db()
    if TG_TOKEN and PUBLIC_URL:
        try:
            hook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
            tg_api("setWebhook", {"url": hook_url})
        except Exception:
            pass

