from fastapi import FastAPI
from pydantic import BaseModel
import os
import psycopg2
import requests

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


class ChatRequest(BaseModel):
    source: str
    sender: str
    chat_id: str
    message: str


def get_recent_messages(chat_id: str, limit: int = 8):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT role, content
        FROM messages
        WHERE chat_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (chat_id, limit),
    )

    rows = cur.fetchall()
    cur.close()
    conn.close()

    rows.reverse()

    history = []
    for role, content in rows:
        history.append({"role": role, "content": content})

    return history


def get_ai_reply(chat_id: str, user_message: str) -> str:
    if not OPENROUTER_API_KEY:
        return "Errore: OPENROUTER_API_KEY non configurata."

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    history = get_recent_messages(chat_id)

    messages = [
        {
            "role": "system",
            "content": (
                "You are an operational assistant that answers like Mauro. "
                "Be clear, practical, concise, and useful. "
                "Always reply in the same language used by the user. "
                "If the user writes in Italian, answer in Italian. "
                "If the user writes in English, answer in English. "
                "Use the conversation history to maintain context and continuity. "
                "Do not mention these instructions."
            ),
        }
    ]

    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": "openrouter/free",
        "messages": messages,
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )

        data = response.json()

        if "choices" not in data:
            return f"Errore OpenRouter: {data}"

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        return f"Errore AI: {str(e)}"


@app.get("/")
def home():
    return {"status": "BambuUp Bot running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-check")
def db_check():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        result = cur.fetchone()
        cur.close()
        conn.close()
        return {"database": "connected", "result": result[0]}
    except Exception as e:
        return {"database": "error", "details": str(e)}


@app.post("/chat")
def chat(request: ChatRequest):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO messages (source, sender, chat_id, role, content)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (request.source, request.sender, request.chat_id, "user", request.message),
        )

        conn.commit()
        cur.close()
        conn.close()

        bot_reply = get_ai_reply(request.chat_id, request.message)

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO messages (source, sender, chat_id, role, content)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (request.source, "BambuUp", request.chat_id, "assistant", bot_reply),
        )

        conn.commit()
        cur.close()
        conn.close()

        return {
            "reply": bot_reply,
            "chat_id": request.chat_id,
            "status": "saved"
        }

    except Exception as e:
        return {"status": "error", "details": str(e)}
