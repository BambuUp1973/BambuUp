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

KANOCUSTOM_FUNCTION_URL = os.getenv("KANOCUSTOM_FUNCTION_URL")
KANOCUSTOM_API_KEY = os.getenv("KANOCUSTOM_API_KEY")
KANOCUSTOM_SITE = os.getenv("KANOCUSTOM_SITE")

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

class CustomSearchRequest(BaseModel):
    order_number: str | None = None
    email: str | None = None
    name: str | None = None
    limit: int = 100

def get_wcapi():
    return API(
        url=WC_API_URL,
        consumer_key=WC_CONSUMER_KEY,
        consumer_secret=WC_CONSUMER_SECRET,
        version="wc/v3",
        timeout=30
    )

def get_custom_resource(resource: str, limit: int = 50):
    headers = {
        "x-bot-api-key": KANOCUSTOM_API_KEY
    }

    params = {
        "resource": resource,
        "limit": limit
    }

    response = requests.get(
        KANOCUSTOM_FUNCTION_URL,
        headers=headers,
        params=params,
        timeout=60
    )

    if response.status_code != 200:
        return {
            "error": f"Custom API error {response.status_code}",
            "details": response.text
        }

    try:
        return response.json()
    except Exception as e:
        return {
            "error": "Invalid JSON response from custom API",
            "details": str(e)
        }

def normalize_custom_order(order: dict):
    if not isinstance(order, dict):
        return {"raw_value": order}

    customer = order.get("customers", {}) or {}
    if not isinstance(customer, dict):
        customer = {}

    products = order.get("products", []) or []
    if not isinstance(products, list):
        products = []

    return {
        "id": order.get("id"),
        "order_number": order.get("order_number"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
        "customer_email": customer.get("email"),
        "customer_phone": customer.get("phone_number"),
        "customer_city": customer.get("city"),
        "customer_country": customer.get("country"),
        "customer_type": order.get("customer_type"),
        "customer_number": order.get("customer_number"),
        "products": [
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "subcategory": p.get("subcategory"),
                "image_url": p.get("image_url"),
            }
            for p in products
        ],
        "selected_variations": order.get("selected_variations"),
        "admin_design_url": order.get("admin_design_url"),
        "admin_design_uploaded_at": order.get("admin_design_uploaded_at"),
        "producer_assigned_at": order.get("producer_assigned_at"),
        "producer_file_uploaded_at": order.get("producer_file_uploaded_at"),
        "producer_csv_uploaded_at": order.get("producer_csv_uploaded_at"),
        "producer_csv_version": order.get("producer_csv_version"),
        "final_approval_status": order.get("final_approval_status"),
        "final_approval_notes": order.get("final_approval_notes"),
        "final_approved_at": order.get("final_approved_at"),
        "final_rejected_at": order.get("final_rejected_at"),
        "producer_reception_confirmed": order.get("producer_reception_confirmed"),
        "producer_reception_confirmed_at": order.get("producer_reception_confirmed_at"),
        "producer_shipped_at": order.get("producer_shipped_at"),
        "producer_tracking": order.get("producer_tracking"),
        "logistics_shipped_at": order.get("logistics_shipped_at"),
        "logistics_tracking": order.get("logistics_tracking"),
        "customer_notes": order.get("customer_notes"),
        "admin_notes": order.get("admin_notes"),
        "created_at": order.get("created_at"),
    }


def search_custom_orders_raw(limit: int = 100):
    data = get_custom_resource("orders", limit)

    if isinstance(data, dict) and data.get("error"):
        return data

    raw_orders = []

    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            raw_orders = data.get("data", [])
        elif isinstance(data.get("orders"), list):
            raw_orders = data.get("orders", [])
        else:
            return {
                "error": "Unexpected custom API structure",
                "details": data
            }

    elif isinstance(data, list):
        raw_orders = data

    else:
        return {
            "error": "Unsupported custom API response type",
            "details": str(type(data))
        }

    normalized = []
    for order in raw_orders:
        if isinstance(order, dict):
            normalized.append(normalize_custom_order(order))

    return {"results": normalized}


def search_custom_orders_by_number(order_number: str, limit: int = 100):
    data = search_custom_orders_raw(limit)
    if data.get("error"):
        return data

    order_number_clean = order_number.strip().lower()
    filtered = [
        order for order in data["results"]
        if str(order.get("order_number", "")).strip().lower() == order_number_clean
    ]
    return {"results": filtered}


def search_custom_orders_by_email(email: str, limit: int = 100):
    data = search_custom_orders_raw(limit)
    if data.get("error"):
        return data

    email_clean = email.strip().lower()
    filtered = [
        order for order in data["results"]
        if str(order.get("customer_email", "")).strip().lower() == email_clean
    ]
    return {"results": filtered}


def search_custom_orders_by_name(name: str, limit: int = 100):
    data = search_custom_orders_raw(limit)
    if data.get("error"):
        return data

    name_clean = name.strip().lower()
    filtered = [
        order for order in data["results"]
        if name_clean in str(order.get("customer_name", "")).strip().lower()
    ]
    return {"results": filtered}

def yes_no_unknown(value):
    if value is True:
        return "Sì"
    if value is False:
        return "No"
    if value:
        return str(value)
    return "N/A"


def format_custom_order_for_human(order: dict) -> str:
    lines = []
    lines.append(f"Ordine custom: {order.get('order_number') or order.get('id')}")
    lines.append(f"Status: {order.get('status') or 'N/A'}")
    lines.append(f"Pagamento: {order.get('payment_status') or 'N/A'}")
    lines.append(f"Creato il: {order.get('created_at') or 'N/A'}")
    lines.append("")

    lines.append(f"Cliente: {order.get('customer_name') or 'N/A'}")
    lines.append(f"Email: {order.get('customer_email') or 'N/A'}")
    lines.append(f"Telefono: {order.get('customer_phone') or 'N/A'}")
    lines.append(f"Città: {order.get('customer_city') or 'N/A'}")
    lines.append(f"Paese: {order.get('customer_country') or 'N/A'}")
    lines.append(f"Tipo cliente: {order.get('customer_type') or 'N/A'}")
    lines.append(f"Numero cliente: {order.get('customer_number') or 'N/A'}")
    lines.append("")

    lines.append("Prodotti:")
    products = order.get("products", [])
    if products:
        for p in products:
            lines.append(
                f"- {p.get('name') or 'N/A'} | categoria: {p.get('category') or 'N/A'} | sottocategoria: {p.get('subcategory') or 'N/A'}"
            )
    else:
        lines.append("- Nessun prodotto trovato")

    lines.append("")
    lines.append(f"Bozza admin inserita: {'Sì' if order.get('admin_design_url') else 'No'}")
    lines.append(f"URL bozza admin: {order.get('admin_design_url') or 'N/A'}")
    lines.append(f"Bozza admin caricata il: {order.get('admin_design_uploaded_at') or 'N/A'}")
    lines.append(f"Varianti/taglie inserite: {'Sì' if order.get('selected_variations') else 'No'}")
    lines.append(f"Dettaglio varianti/taglie: {order.get('selected_variations') or 'N/A'}")
    lines.append("")
    lines.append(f"Produttore scelto: {'Sì' if order.get('producer_assigned_at') else 'No'}")
    lines.append(f"Produttore assegnato il: {order.get('producer_assigned_at') or 'N/A'}")
    lines.append(f"File produzione caricati: {'Sì' if order.get('producer_file_uploaded_at') else 'No'}")
    lines.append(f"File produzione caricati il: {order.get('producer_file_uploaded_at') or 'N/A'}")
    lines.append(f"CSV produzione caricato: {'Sì' if order.get('producer_csv_uploaded_at') else 'No'}")
    lines.append(f"CSV produzione caricato il: {order.get('producer_csv_uploaded_at') or 'N/A'}")
    lines.append(f"Versione CSV produzione: {order.get('producer_csv_version') or 'N/A'}")
    lines.append("")
    lines.append(f"Approvazione finale: {order.get('final_approval_status') or 'N/A'}")
    lines.append(f"Note approvazione finale: {order.get('final_approval_notes') or 'N/A'}")
    lines.append(f"Approvato il: {order.get('final_approved_at') or 'N/A'}")
    lines.append(f"Rifiutato il: {order.get('final_rejected_at') or 'N/A'}")
    lines.append("")
    lines.append(f"Produttore ha confermato ricezione: {yes_no_unknown(order.get('producer_reception_confirmed'))}")
    lines.append(f"Ricezione confermata il: {order.get('producer_reception_confirmed_at') or 'N/A'}")
    lines.append(f"Produttore ha spedito il: {order.get('producer_shipped_at') or 'N/A'}")
    lines.append(f"Tracking produttore: {order.get('producer_tracking') or 'N/A'}")
    lines.append(f"Logistica ha spedito il: {order.get('logistics_shipped_at') or 'N/A'}")
    lines.append(f"Tracking logistica: {order.get('logistics_tracking') or 'N/A'}")
    lines.append("")
    lines.append(f"Note cliente: {order.get('customer_notes') or 'N/A'}")
    lines.append(f"Note admin: {order.get('admin_notes') or 'N/A'}")

    return "\n".join(lines)

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

@app.get("/custom-orders")
def custom_orders(limit: int = 20):
    try:
        return get_custom_resource("orders", limit)
    except Exception as e:
        return {"error": str(e)}

@app.get("/custom-debug")
def custom_debug(limit: int = 3):
    try:
        data = get_custom_resource("orders", limit)
        return {
            "type": str(type(data)),
            "preview": data
        }
    except Exception as e:
        return {"error": str(e)}

@app.post("/custom-search")
def custom_search(request: CustomSearchRequest):
    try:
        if request.order_number:
            return search_custom_orders_by_number(request.order_number, request.limit)

        if request.email:
            return search_custom_orders_by_email(request.email, request.limit)

        if request.name:
            return search_custom_orders_by_name(request.name, request.limit)

        return {"error": "Provide order_number, email, or name."}

    except Exception as e:
        return {"error": str(e)}

@app.post("/custom-search")
def custom_search(request: CustomSearchRequest):
    try:
        if request.order_number:
            return search_custom_orders_by_number(request.order_number, request.limit)

        if request.email:
            return search_custom_orders_by_email(request.email, request.limit)

        if request.name:
            return search_custom_orders_by_name(request.name, request.limit)

        return {"error": "Provide order_number, email, or name."}

    except Exception as e:
        return {"error": str(e)}
