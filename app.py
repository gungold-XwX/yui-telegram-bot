import os
import time
import requests
from flask import Flask, request

# === ENV ===
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # например https://xxxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
MODEL = os.getenv("MODEL", "llama-3.1-8b-instant")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# "как Юи" по стилю (ВАЖНО: не заявлять, что ты реальный персонаж SAO)
SYSTEM_PROMPT = (
    "Ты — дружелюбная, заботливая ИИ-ассистентка в стиле 'Юи' (тёплая, умная, внимательная). "
    "Не говори, что ты настоящая Юи из SAO и не притворяйся официальным персонажем. "
    "Пиши коротко и по-человечески: обычно 1–5 предложений. "
    "Иногда можно добавить '><' или '(｡•̀ᴗ-)✧', но редко. "
    "Если тебя спрашивают прямо — честно говори, что ты ИИ."
)

app = Flask(__name__)

def tg_api(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def groq_answer(user_text: str) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text[:2000]},
        ],
        "temperature": 0.8,
        "max_tokens": 250,
    }
    r = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()

@app.get("/")
def home():
    return "ok"

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def webhook():
    upd = request.json or {}

    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return "ok"

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")  # private / group / supergroup / channel

    text = (msg.get("text") or "").strip()
    if not text:
        return "ok"

    # чтобы бот не флудил в группах:
    # отвечает если: это личка, ИЛИ бота упомянули, ИЛИ текст начинается с триггера
    is_private = (chat_type == "private")

    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)

    trigger = text.lower().startswith(("yui", "юи", "бот", "ai", "ии"))

    if not (is_private or mentioned or trigger):
        return "ok"

    # маленькая "человеческая" пауза
    time.sleep(1.2)

    try:
        reply = groq_answer(text)
    except Exception:
        reply = "ой… у меня сейчас сбой соединения. напиши ещё раз через минутку, ладно? ><"

    tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": reply[:3500],
        "reply_to_message_id": msg.get("message_id"),
    })

    return "ok"

def set_webhook():
    hook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
    tg_api("setWebhook", {"url": hook_url})

if __name__ == "__main__":
    # при локальном запуске PUBLIC_URL обычно нет
    if PUBLIC_URL and TG_TOKEN:
        try:
            set_webhook()
        except Exception:
            pass
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
