from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import re
import psycopg2
import requests
from woocommerce import API

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

DATABASE_URL = os.getenv("DATABASE_URL")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

WC_API_URL = os.getenv("WC_API_URL")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET")


class ChatRequest(BaseModel):
    source: str
    sender: str
    chat_id: str
    message: str


class OrderSearchRequest(BaseModel):
    order_id: str | None = None
    email: str | None = None
    name: str | None = None


def get_wcapi():
    return API(
        url=WC_API_URL,
        consumer_key=WC_CONSUMER_KEY,
        consumer_secret=WC_CONSUMER_SECRET,
        version="wc/v3",
        timeout=30
    )


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


def normalize_order(order):
    billing = order.get("billing", {}) or {}
    shipping = order.get("shipping", {}) or {}
    shipping_lines = order.get("shipping_lines", []) or []
    line_items = order.get("line_items", []) or []

    return {
        "id": order.get("id"),
        "status": order.get("status"),
        "date_created": order.get("date_created"),
        "total": order.get("total"),
        "currency": order.get("currency"),
        "payment_method_title": order.get("payment_method_title"),
        "customer_note": order.get("customer_note"),
        "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
        "email": billing.get("email"),
        "phone": billing.get("phone"),
        "billing_address": {
            "address_1": billing.get("address_1"),
            "address_2": billing.get("address_2"),
            "city": billing.get("city"),
            "state": billing.get("state"),
            "postcode": billing.get("postcode"),
            "country": billing.get("country"),
        },
        "shipping_name": f"{shipping.get('first_name', '')} {shipping.get('last_name', '')}".strip(),
        "shipping_address": {
            "address_1": shipping.get("address_1"),
            "address_2": shipping.get("address_2"),
            "city": shipping.get("city"),
            "state": shipping.get("state"),
            "postcode": shipping.get("postcode"),
            "country": shipping.get("country"),
        },
        "shipping_methods": [line.get("method_title") for line in shipping_lines],
        "items": [
            {
                "name": item.get("name"),
                "quantity": item.get("quantity"),
                "total": item.get("total"),
            }
            for item in line_items
        ],
    }


def search_orders_by_id(order_id: str):
    wcapi = get_wcapi()
    response = wcapi.get(f"orders/{order_id}")
    if response.status_code != 200:
        return {"error": f"WooCommerce error {response.status_code}", "details": response.text}
    return {"results": [normalize_order(response.json())]}


def search_orders_by_email(email: str):
    wcapi = get_wcapi()
    response = wcapi.get("orders", params={"search": email, "per_page": 20})
    if response.status_code != 200:
        return {"error": f"WooCommerce error {response.status_code}", "details": response.text}

    orders = response.json()
    filtered = [
        normalize_order(order)
        for order in orders
        if (order.get("billing", {}) or {}).get("email", "").lower() == email.lower()
    ]
    return {"results": filtered}


def search_orders_by_name(name: str):
    wcapi = get_wcapi()
    response = wcapi.get("orders", params={"search": name, "per_page": 20})
    if response.status_code != 200:
        return {"error": f"WooCommerce error {response.status_code}", "details": response.text}

    orders = response.json()
    name_lower = name.lower().strip()

    filtered = []
    for order in orders:
        billing = order.get("billing", {}) or {}
        full_name = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip().lower()
        if name_lower in full_name:
            filtered.append(normalize_order(order))

    return {"results": filtered}


def format_address(address: dict) -> str:
    parts = [
        address.get("address_1"),
        address.get("address_2"),
        address.get("city"),
        address.get("state"),
        address.get("postcode"),
        address.get("country"),
    ]
    clean_parts = [p for p in parts if p]
    return ", ".join(clean_parts) if clean_parts else "N/A"


def format_order_for_human(order: dict) -> str:
    lines = []
    lines.append(f"Ordine: {order.get('id')}")
    lines.append(f"Stato: {order.get('status')}")
    lines.append(f"Data ordine: {order.get('date_created')}")
    lines.append(f"Totale: {order.get('total')} {order.get('currency')}")
    lines.append(f"Metodo pagamento: {order.get('payment_method_title') or 'N/A'}")
    lines.append("")
    lines.append(f"Cliente: {order.get('customer_name') or 'N/A'}")
    lines.append(f"Email: {order.get('email') or 'N/A'}")
    lines.append(f"Telefono: {order.get('phone') or 'N/A'}")
    lines.append("")
    lines.append(f"Indirizzo fatturazione: {format_address(order.get('billing_address', {}))}")
    lines.append(f"Destinatario spedizione: {order.get('shipping_name') or 'N/A'}")
    lines.append(f"Indirizzo spedizione: {format_address(order.get('shipping_address', {}))}")
    lines.append("")
    lines.append("Prodotti:")
    items = order.get("items", [])
    if items:
        for item in items:
            lines.append(
                f"- {item.get('name')} | quantità: {item.get('quantity')} | totale: {item.get('total')}"
            )
    else:
        lines.append("- Nessun prodotto trovato")

    shipping_methods = order.get("shipping_methods", [])
    if shipping_methods:
        lines.append("")
        lines.append("Metodo spedizione:")
        for method in shipping_methods:
            lines.append(f"- {method}")

    if order.get("customer_note"):
        lines.append("")
        lines.append(f"Nota cliente: {order.get('customer_note')}")

    lines.append("")
    status = (order.get("status") or "").lower()
    if status in ["completed", "shipped"]:
        lines.append(
            "Nota tracking: l'ordine risulta spedito/completato. Per il tracking dettagliato bisogna controllare il sistema logistica."
        )
    else:
        lines.append(
            "Nota tracking: l'ordine non risulta ancora spedito/completato in WooCommerce."
        )

    return "\n".join(lines)


def try_extract_order_id(message: str) -> str | None:
    patterns = [
        r"ordine\s*#?\s*(\d+)",
        r"order\s*#?\s*(\d+)",
        r"\b(\d{5,})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, message.lower())
        if match:
            return match.group(1)

    return None


def is_order_request(message: str) -> bool:
    msg = message.lower()
    keywords = [
        "ordine",
        "order",
        "stato ordine",
        "order status",
        "spedito",
        "processing",
        "completed",
    ]
    return any(word in msg for word in keywords)


@app.get("/")
def home():
    return {"status": "BambuUp Bot running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/webchat")
def webchat():
    return FileResponse("static/chat.html")


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

        bot_reply = None

        if is_order_request(request.message):
            order_id = try_extract_order_id(request.message)
            if order_id:
                result = search_orders_by_id(order_id)
                if result.get("results"):
                    bot_reply = format_order_for_human(result["results"][0])
                elif result.get("error"):
                    bot_reply = f"Errore ricerca ordine: {result['error']}"
                else:
                    bot_reply = f"Non ho trovato l'ordine {order_id}."
            else:
                bot_reply = (
                    "Ho capito che stai chiedendo informazioni su un ordine, "
                    "ma mi serve il numero ordine per cercarlo con precisione."
                )

        if not bot_reply:
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


@app.post("/order-search")
def order_search(request: OrderSearchRequest):
    try:
        if request.order_id:
            return search_orders_by_id(request.order_id)

        if request.email:
            return search_orders_by_email(request.email)

        if request.name:
            return search_orders_by_name(request.name)

        return {"error": "Provide order_id, email, or name."}

    except Exception as e:
        return {"error": str(e)}
