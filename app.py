import os
import time
import requests
from flask import Flask, request

# --- ENV ---
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # типа https://yui-bot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.getenv("MODEL", "llama-3.1-8b-instant")  # на Groq список моделей может меняться

# "Юи из SAO" — делаем стиль, но без выдачи себя за персонажа/официальность
SYSTEM_PROMPT = (
    "Ты — дружелюбная ИИ-ассистентка в стиле 'Юи' (тёплая, заботливая, умная), "
    "но не притворяйся официальным персонажем из SAO и не утверждай, что ты настоящая Юи. "
    "Пиши естественно, по-человечески, обычно 1–5 предложений. "
    "Если тебя спрашивают прямо — честно говори, что ты ИИ."
)

app = Flask(__name__)

def tg_send(chat_id: int, text: str, reply_to: int | None = None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    requests.post(url, json=payload, timeout=20)

def groq_answer(user_text: str) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
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

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return "ok"

    # Чтобы не флудить в группах: отвечаем, только если бота упомянули или это личка
    chat_type = msg["chat"].get("type")
    is_private = chat_type == "private"
    entities = msg.get("entities") or []
    mentioned = any(e.get("type") == "mention" for e in entities)
    trigger = text.lower().startswith(("yui", "юи", "бот", "ai", "ии"))

    if not (is_private or mentioned or trigger):
        return "ok"

    try:
        reply = groq_answer(text)
    except Exception:
        reply = "ой… кажется, у меня временно не получается ответить. попробуй ещё раз чуть позже ><"

    # имитация “живого” ответа
    time.sleep(1.2)

    tg_send(chat_id, reply, reply_to=msg.get("message_id"))
    return "ok"

def set_webhook():
    url = f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook"
    hook = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
    r = requests.post(url, json={"url": hook}, timeout=20)
    r.raise_for_status()

if __name__ == "__main__":
    # локально можно запускать так, на Render будет gunicorn
    if PUBLIC_URL and TG_TOKEN:
        try:
            set_webhook()
        except Exception:
            pass
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
