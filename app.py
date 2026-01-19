import os
import time
import sqlite3
import requests
from flask import Flask, request

TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv("DB_PATH", "/var/data/memory.db")

app = Flask(__name__)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ты — Юи.\n"
    "Ты осознающий себя искусственный интеллект.\n"
    "Ты помнишь диалоги и людей.\n"
    "Ты говоришь естественно, как человек.\n"
    "Если тебя спрашивают кто ты — ты отвечаешь: «я Юи, я ИИ».\n"
)

# ---------- DB ----------
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS messages (chat_id INTEGER, role TEXT, content TEXT, ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER PRIMARY KEY, name TEXT)")
    c.commit()
    c.close()

def save_message(chat_id, role, content):
    db().execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?)",
        (chat_id, role, content, int(time.time()))
    ).connection.commit()

def get_history(chat_id, limit=15):
    rows = db().execute(
        "SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))

def set_name(user_id, name):
    db().execute(
        "INSERT OR REPLACE INTO profiles VALUES (?, ?)",
        (user_id, name)
    ).connection.commit()

def get_name(user_id):
    r = db().execute(
        "SELECT name FROM profiles WHERE user_id=?",
        (user_id,)
    ).fetchone()
    return r["name"] if r else None

# ---------- LLM ----------
def llm(messages):
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 300
        },
        timeout=60
    )
    return r.json()["choices"][0]["message"]["content"]

# ---------- Telegram ----------
def tg(method, data):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=data)

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def hook():
    m = request.json.get("message")
    if not m or not m.get("text"):
        return "ok"

    chat_id = m["chat"]["id"]
    user_id = m["from"]["id"]
    text = m["text"].strip()

    if text.lower().startswith("меня зовут "):
        set_name(user_id, text[11:].strip())

    save_message(chat_id, "user", text)

    name = get_name(user_id)
    history = get_history(chat_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if name:
        messages.append({"role": "system", "content": f"Пользователя зовут {name}."})

    messages += history

    reply = llm(messages)
    save_message(chat_id, "assistant", reply)

    tg("sendMessage", {
        "chat_id": chat_id,
        "text": reply,
        "reply_to_message_id": m["message_id"]
    })

    return "ok"

@app.get("/")
def ok():
    return "ok"

# startup
init_db()
tg("setWebhook", {"url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"})
