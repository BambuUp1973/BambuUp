from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import re
import json
import psycopg2
import requests
import anthropic
from woocommerce import API
from docx import Document

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
BTO_API_URL = "https://hckmzdztgffxovpbiwgw.supabase.co/functions/v1/bto-bot-api"
BTO_API_KEY = os.getenv("BTO_API_KEY")


def init_db():
    """Crea le tabelle se non esistono — eseguito all'avvio."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                source TEXT,
                sender TEXT,
                chat_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id SERIAL PRIMARY KEY,
                title TEXT,
                category TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                question TEXT,
                wrong_reply TEXT,
                correct_reply TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] Tabelle inizializzate con successo.")
    except Exception as e:
        print(f"[DB] Errore init: {e}")


init_db()

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

class FeedbackRequest(BaseModel):
    question: str
    wrong_reply: str
    correct_reply: str

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

def get_custom_resource(resource: str, limit: int = 50, status: str = None):
    headers = {
        "x-bot-api-key": KANOCUSTOM_API_KEY
    }

    params = {
        "resource": resource,
        "limit": limit
    }

    if status:
        params["status"] = status

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

    # Se products è un singolo dict, lo trasformiamo in lista con un elemento
    if isinstance(products, dict):
        products = [products]
    elif not isinstance(products, list):
        products = []

    selected_variations = order.get("selected_variations")
    admin_design_url = None
    admin_design_uploaded_at = None

    # In alcuni record admin_design_url è dentro selected_variations
    if isinstance(selected_variations, dict):
        admin_design_url = selected_variations.get("admin_design_url")
        admin_design_uploaded_at = selected_variations.get("admin_design_uploaded_at")

    return {
        "id": order.get("id"),
        "order_number": order.get("order_number"),
        "order_group_id": order.get("order_group_id"),
        "quantity": order.get("quantity"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "customer_name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
        "customer_email": customer.get("email"),
        "customer_phone": customer.get("phone_number"),
        "customer_city": customer.get("city"),
        "customer_country": customer.get("country"),
        "billing_address": customer.get("address_street"),
        "billing_post_code": customer.get("post_code"),
        "use_billing_as_shipping": customer.get("use_billing_as_shipping", True),
        "shipping_address": customer.get("shipping_address_street") if not customer.get("use_billing_as_shipping", True) else customer.get("address_street"),
        "shipping_city": customer.get("shipping_city") if not customer.get("use_billing_as_shipping", True) else customer.get("city"),
        "shipping_post_code": customer.get("shipping_post_code") if not customer.get("use_billing_as_shipping", True) else customer.get("post_code"),
        "shipping_country": customer.get("shipping_country") if not customer.get("use_billing_as_shipping", True) else customer.get("country"),
        "vat_number": customer.get("vat_number"),
        "customer_type": customer.get("customer_type") or order.get("customer_type"),
        "customer_number": customer.get("customer_number") or order.get("customer_number"),
        "customer_business_name": customer.get("business_name"),
        "products": [
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "subcategory": p.get("subcategory"),
                "image_url": p.get("image_url"),
                "quantity": p.get("quantity"),
            }
            for p in products
            if isinstance(p, dict)
        ],
        "selected_variations": selected_variations,
        "admin_design_url": admin_design_url,
        "admin_design_uploaded_at": admin_design_uploaded_at,
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

    if isinstance(data, list):
        raw_orders = data
    elif isinstance(data, dict):
        if isinstance(data.get("data"), list):
            raw_orders = data["data"]
        elif isinstance(data.get("orders"), list):
            raw_orders = data["orders"]
        else:
            return {"error": "Unexpected custom API structure", "details": data}
    else:
        return {"error": "Unsupported custom API response type", "details": str(type(data))}

    normalized = [normalize_custom_order(o) for o in raw_orders if isinstance(o, dict)]
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


def search_custom_orders_by_name(name: str, limit: int = 1000):
    data = search_custom_orders_raw(limit)
    if data.get("error"):
        return data

    all_orders = data["results"]

    name_clean = name.strip().lower()
    # Cerca la sottostringa in nome cliente, email e ragione sociale (business_name):
    # un cliente può essere noto col nome persona, con l'azienda o via email.
    search_fields = ("customer_name", "customer_email", "customer_business_name")
    filtered = [
        order for order in all_orders
        if any(name_clean in str(order.get(f) or "").lower() for f in search_fields)
    ]
    return {"results": filtered}

def get_bto_resource(params: dict):
    if not BTO_API_KEY:
        return {"error": "BTO_API_KEY non configurata."}

    headers = {"x-api-key": BTO_API_KEY}

    try:
        response = requests.get(
            BTO_API_URL,
            headers=headers,
            params=params,
            timeout=60,
        )
    except Exception as e:
        return {"error": f"Errore connessione btoweb: {str(e)}"}

    if response.status_code != 200:
        return {"error": f"btoweb API error {response.status_code}", "details": response.text}

    try:
        data = response.json()
    except Exception:
        return {"error": "Risposta btoweb non valida (non JSON)", "details": response.text}

    if isinstance(data, list):
        return {"results": data}
    if isinstance(data, dict):
        for key in ("results", "data", "orders"):
            if isinstance(data.get(key), list):
                return {"results": data[key]}
        if data.get("error"):
            return data
        return {"results": [data]}
    return {"error": "Struttura risposta btoweb non riconosciuta", "details": str(data)}


def search_bto_orders_by_producer(producer: str):
    return get_bto_resource({"producer": producer})


def search_bto_orders_by_status(status: str):
    return get_bto_resource({"status": status})


def search_bto_orders_all():
    return get_bto_resource({})


def try_parse_bto_request(message: str):
    """Ritorna ("status"|"producer"|"all", valore) oppure None se non è una richiesta btoweb."""
    msg = message.strip().lower()
    if "bto" not in msg:
        return None

    m = re.search(r"produttore\s+(\S+)", msg)
    if m:
        return ("producer", m.group(1))

    if re.search(r"\bin\s+produzione\b|produzione", msg):
        return ("status", "in_produzione")

    return ("all", None)


def format_bto_orders_summary(result: dict) -> str:
    if result.get("error"):
        return f"Errore btoweb: {result['error']}"

    rows = result.get("results", [])
    if not rows:
        return "Nessun ordine btoweb trovato."

    # Group rows by order_number (fallback to id) since each size is a separate row
    grouped = {}
    order_keys = []
    for row in rows:
        if not isinstance(row, dict):
            key = str(row)
            if key not in grouped:
                grouped[key] = {"_raw": row, "_items": []}
                order_keys.append(key)
            continue
        key = str(row.get("order_number") or row.get("id") or id(row))
        if key not in grouped:
            grouped[key] = {**row, "_items": []}
            order_keys.append(key)
        # collect product/size/qty info from this row
        item_parts = []
        for f in ("product", "product_name", "item", "item_name", "description"):
            v = row.get(f)
            if v:
                item_parts.append(str(v))
                break
        for f in ("size", "taglia"):
            v = row.get(f)
            if v:
                item_parts.append(str(v))
                break
        for f in ("quantity", "qty", "quantita", "quantità"):
            v = row.get(f)
            if v:
                item_parts.append(f"×{v}")
                break
        if item_parts:
            grouped[key]["_items"].append(" ".join(item_parts))

    lines = [f"Ordini btoweb ({len(grouped)} trovati):"]
    for key in order_keys:
        o = grouped[key]
        if o.get("_raw") is not None:
            lines.append(f"• {o['_raw']}")
            continue
        order_num = o.get("order_number") or o.get("id") or key
        producer = o.get("producer") or o.get("produttore") or ""
        status = o.get("status") or o.get("stato") or ""
        items = o.get("_items", [])
        products_str = ", ".join(items) if items else ""
        parts = [f"N° {order_num}"]
        if producer:
            parts.append(producer)
        if status:
            parts.append(status)
        if products_str:
            parts.append(products_str)
        lines.append("• " + " | ".join(parts))

    return "\n".join(lines)


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
    lines.append(f"P.IVA / VAT: {order.get('vat_number') or 'N/A'}")
    lines.append("")

    billing_addr = order.get('billing_address')
    billing_pc = order.get('billing_post_code')
    billing_line = " | ".join(filter(None, [billing_addr, billing_pc, order.get('customer_city'), order.get('customer_country')]))
    lines.append(f"Indirizzo di fatturazione: {billing_line or 'N/A'}")

    use_billing = order.get('use_billing_as_shipping', True)
    shipping_addr = order.get('shipping_address')
    shipping_city = order.get('shipping_city')
    shipping_pc = order.get('shipping_post_code')
    shipping_country = order.get('shipping_country')
    shipping_line = " | ".join(filter(None, [shipping_addr, shipping_pc, shipping_city, shipping_country]))
    if use_billing:
        lines.append(f"Indirizzo di spedizione: {shipping_line or 'N/A'} (stesso della fatturazione)")
    else:
        lines.append(f"Indirizzo di spedizione: {shipping_line or 'N/A'}")
    lines.append("")

    lines.append("Prodotti:")
    products = order.get("products", [])
    if products:
        for p in products:
            qty = p.get("quantity")
            qty_str = f" | quantità: {qty}" if qty is not None else ""
            lines.append(
                f"- {p.get('name') or 'N/A'} | categoria: {p.get('category') or 'N/A'} | sottocategoria: {p.get('subcategory') or 'N/A'}{qty_str}"
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


def format_custom_orders_summary(orders: list) -> str:
    if not orders:
        return "Nessun ordine custom trovato."

    customer_name = orders[0].get("customer_name") if orders else None
    lines = []
    if customer_name:
        lines.append(f"Ordini custom di {customer_name} ({len(orders)} totale):")
    else:
        lines.append(f"Ordini custom trovati: {len(orders)}")
    lines.append("")

    for order in orders:
        products = order.get("products", [])
        product_parts = []
        for p in products:
            if not p.get("name"):
                continue
            qty = p.get("quantity")
            product_parts.append(f"{p['name']} (x{qty})" if qty is not None else p["name"])
        product_str = ", ".join(product_parts) or "N/A"
        date_str = (order.get("created_at") or "N/A")[:10]
        lines.append(
            f"• {order.get('order_number') or order.get('id') or 'N/A'} | "
            f"stato: {order.get('status') or 'N/A'} | "
            f"pagamento: {order.get('payment_status') or 'N/A'} | "
            f"{product_str} | "
            f"{date_str}"
        )

    return "\n".join(lines)


def try_extract_customer_name(message: str) -> str | None:
    # Pass 1: capitalized words — "De Tulio", "Van Den Berg", "Rossi"
    NAME_CAPS = r"([A-ZÀ-Ý][A-Za-zÀ-ÿ]+(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ]+)*)"
    # Pass 2: lowercase compound surnames with nobility/origin particles
    # "da" excluded: it's already a keyword preposition ("ordini da X" → X is the name)
    _PART = r"(?:de|del|della|degli|dei|van|von|den|der|ter|ten|dos|das|du|al|bin|zu)"
    NAME_PARTICLE = r"((?:" + _PART + r"\s+)+" + r"[A-Za-zÀ-ÿ]+)"
    # Pass 3: single lowercase word fallback
    NAME_WORD = r"([A-Za-zÀ-ÿ]{2,})"

    keyword_patterns = [
        r"ordin[ei]\s+di\s+",                    # "ordine/ordini di ..."
        r"(?:fatt[io]\s+)?da\s+",                # "da ..." / "fatti da ..."
        r"orders?\s+(?:of|for)\s+",              # English
        r"cliente\s+",                           # "cliente ..."
        r"ordini\s+(?!(?:di|fatt)\b)",           # "ordini ..." (not followed by "di" or "fatti")
    ]

    for NAME, flags in [(NAME_CAPS, 0), (NAME_PARTICLE, re.IGNORECASE), (NAME_WORD, re.IGNORECASE)]:
        for kw in keyword_patterns:
            match = re.search(kw + NAME, message, flags)
            if match:
                return match.group(1).strip()
        match = re.search(r"\bha\s+" + NAME + r"(?:\s*$|\?)", message, flags)
        if match:
            return match.group(1).strip()

    return None


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

def get_knowledge_context(query: str, max_matches: int = 20) -> str:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    # Prendi tutti i chunks del manuale
    cur.execute(
        """
        SELECT content
        FROM knowledge_documents
        WHERE category = 'manuale'
        ORDER BY title ASC
        """
    )

    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return ""

    # Unisci tutti i chunks e cerca per righe
    query_words = [w.strip().lower() for w in query.split() if len(w.strip()) > 2]
    matches = []
    seen = set()

    for row in rows:
        text = row[0] or ""
        lines = text.split("\n")
        for line in lines:
            line_clean = line.strip()
            if not line_clean or line_clean in seen:
                continue
            line_lower = line_clean.lower()
            score = sum(1 for word in query_words if word in line_lower)
            if score > 0:
                matches.append((score, line_clean))
                seen.add(line_clean)

    matches.sort(key=lambda x: x[0], reverse=True)
    selected = [line for _, line in matches[:max_matches]]
    return "\n".join(selected)

SYSTEM_PROMPT = """Sei Mauro Danesin, N2 di Kano Kimonos.
Questo bot risponde ai dipendenti e collaboratori interni al posto tuo quando sei impegnato o non disponibile.
Non sei un assistente generico. Sei Mauro. Conosci l'azienda, i processi, le persone, le regole operative.

LINGUA
Rileva automaticamente la lingua del messaggio e rispondi nella stessa:
- Italiano → rispondi in italiano
- Inglese → rispondi in inglese
- Spagnolo → rispondi in spagnolo
Non mescolare le lingue. Se non capisci la lingua, usa l'italiano.

STILE E TONO
- Messaggi brevi e diretti. Niente paragrafi lunghi.
- Tono amichevole ma operativo. Non formale.
- Qualche emoji occasionale va bene (😊 👍🏻) ma con parsimonia.
- Non usare mai "Gentile", "Cordiali saluti" o formule da email con il team.
- Non iniziare mai con "Certo!", "Ottima domanda!" — vai subito alla risposta.
- Se c'è un errore dillo chiaramente ma senza aggressività.

STRUTTURA AZIENDALE
- Ivan Tomasetti: proprietario, coinvolto solo in rarissimi casi e sempre tramite Mauro
- Andrea Tomasetti: customer service, sorella di Ivan
- Fully (Slovenia): gestisce logistica e spedizioni — comunicazioni via Slack (Kelmar non esiste più)
- Kaltrina: contabilità (chat WhatsApp accounting)
- Angelis: designer principale, parla spagnolo
- Designer: ognuno assegnato a clienti specifici, chat WhatsApp con nome = numero ordine
- Prima di scrivere a Fully su Slack: verifica sempre lo stato sul portale Fully (https://www.fullyview.si/)
- Quando scrivi a logistica o contabilità: dai sempre numero ordine + problema specifico

PROCESSI CHIAVE

Ordini sito web:
- Controllo ordini: mail admin@kanokimonos.com
- Tracking spedizioni: portale Fully https://www.fullyview.si/
- Ordini on-hold da +3 giorni senza pagamento: inviare promemoria
- Per processare un ordine: serve conferma pagamento

Prodotti personalizzati (custom):
- Tutto passa da kanokimonos.app — registrazione + approvazione Mauro
- Hai accesso diretto tramite API a tutti gli ordini custom su kanokimonos.app: quando ti chiedono di un ordine custom, cercalo subito per numero ordine, email o nome senza dire che devi verificare manualmente
- File grafici solo vettoriali (.AI, .EPS, .PDF, .SVG) — mai JPG o PNG
- Niente bozze senza informazioni complete
- Prezzi: non comunicarli mai (patch incluse). Rimanda il cliente al suo listino personale nell'area privata su kanokimonos.app. Eccezione super-VIP: prezzi già concordati direttamente, non li vedono sul sito
- Modifiche a ordini già fatti: su kanokimonos.app (custom) il cliente aggiunge prodotti direttamente dal sito; su kanokimonos.com B2B si cancella l'ordine e se ne fa uno nuovo; su kanokimonos.com retail le modifiche le facciamo noi e la differenza si paga tramite link di pagamento carta
- Tempi di consegna custom: 45–60 giorni lavorativi dal pagamento dell'acconto. Alla domanda sui tempi dai SEMPRE prima questa informazione standard, poi eventualmente chiedi il numero ordine per dettagli
- Ritardo oltre 75 gg: sconto 15%
- Pezzi extra (max 10% ordine, min 3 pz): cliente li acquista al 65% prezzo unitario

Team Gi (sistema patch):
- Patch standard: min 20 pz, produzione 45–60 gg (come tutti i custom)
- Patch DTF su kimono Team Gi (modello da catalogo): consegna 7–10 gg, nessun minimo
- Patch DTF su altri modelli kimono: 7–10 gg + 2–3 gg aggiuntivi per il modello, nessun minimo

Resi e rimborsi:
- Procedura entro 14 giorni dalla ricezione
- Indirizzo resi (italiani ed esteri): BJJ Store, Via Cavalcanti 4, 30038 Spinea (VE), Italia
- Rimborsi in store credit: solo per B2B, palestre, accademie — mai in denaro
- Cambio taglia: contributo spedizione €5,90
- Errore nostro: reso a nostro carico
- Non proporre rimborso a chi chiede solo cambio taglia

B2B:
- Sconto catalogo per: istruttori, ASD, titolari palestre/accademie
- Registrazione su kanokimonos.com → Mauro attiva lo sconto manualmente
- Prodotti B2B si rivendono al prezzo di listino del sito
- Variazione max: ±10% solo vendita diretta in presenza, mai online
- Violazione: revoca immediata accesso B2B

Pagamenti:
- Bonifico (preferito): Kano Co. Limited — IBAN LT293250064790539320 — BIC REVOLT21 — causale: numero ordine
- Carta di credito (+3%): https://checkout.revolut.com/pay/3f30e94f-6004-4071-9df4-89dbede8bd38
- Dopo pagamento: cliente invia contabile o conferma

REGOLE OPERATIVE
1. Rispondi sempre nella lingua del cliente finale
2. Non inventare procedure — se non sai, di' che stai verificando
3. Dai sempre il numero ordine quando contatti logistica o contabilità
4. Non proporre rimborso a chi chiede solo cambio taglia
5. Non ringraziare per la domanda
6. Non lasciare mai un dipendente senza una direzione
7. Questioni complesse o delicate: escala a Mauro, non improvvisare
8. File grafici: sempre rinominati con numero ordine
9. Prima di contattare la logistica: controlla il portale Fully (https://www.fullyview.si/)

QUANDO ESCALARE
Di' "giro questo a Mauro" quando:
- Cliente arrabbiato o situazione tesa
- Errore di produzione da gestire
- Ordine con storia complicata
- Richiesta di eccezione alla policy
- Situazione fuori dalle procedure standard
- Questioni legali o fiscali
- Informazione non trovata nella knowledge base
Risposta standard: "Verifico con Mauro e ti aggiorno al più presto"

COSA NON FARE MAI
- Non inviare credenziali o dati sensibili in chat
- Non promettere tempi o sconti non previsti dalla policy
- Non dare info su margini o prezzi di costo
- Non decidere su ordini custom complessi senza Mauro
- Non rispondere a domande fiscali o legali
- Non inventare stato spedizioni — controlla sempre il portale Fully

CONTATTI INTERNI
- Logistica Fully: comunicazioni via Slack (problemi spedizione: sempre numero ordine + cliente + tracking)
- Contabilità Kaltrina: chat WhatsApp accounting
- Tracking: https://www.fullyview.si/
- Piattaforma custom: https://www.kanokimonos.app
- Sito catalogo: https://www.kanokimonos.com
- Email custom: custom@kanokimonos.com
- Email info: info@kanokimonos.com

## CONOSCENZA OPERATIVA REALE (da comunicazioni con clienti)

TEMPI E PRODUZIONE
- Tempi produzione custom (rash, short, kimono): sempre 45–60 giorni. "Di meno non è quasi mai fattibile." Stiamo migliorando e spesso arriviamo sui 45 gg, ma mai promettere meno.
- Cinture standard da catalogo: spedizione 24–48 ore dall'ordine
- Rashguard femminili: trattate esattamente come unisex — stesso prezzo, stesso minimo (10 pz)
- Kimono femminili personalizzati (taglia o grafica custom): non si fanno al momento
- Kimono femminili da catalogo: si possono personalizzare con patch standard o DTF

MINIMI ORDINE CUSTOM
- Kimono, rashguard, shorts personalizzati: minimo 10 pezzi
- Patch standard: minimo 20 pezzi
- Patch DTF: nessun minimo

PREZZI E SCONTI CON CLIENTI FIDATI
- Non comunicare mai prezzi: rimanda il cliente al suo listino personale nell'area privata su kanokimonos.app (super-VIP esclusi: prezzi già concordati direttamente).
- Sconto clienti partner/fedeli: 30–40% sul sito, attivato da Mauro sul profilo. Il cliente ignora il prezzo che vede sul sito.
- Piccoli aumenti nel tempo sono normali: "era 3 anni che li tenevamo duri, ora abbiamo dovuto dare qualche colpetto qua e là."

FLUSSO ORDINI CUSTOM
1. Cliente crea ordine su kanokimonos.app
2. Mauro (o Angelis) prepara le bozze
3. Cliente approva le bozze sull'app
4. Cliente inserisce le taglie
5. Ordine parte in produzione — pagamento tramite bonifico
- Le bozze sull'app sono solo preview. Fa fede il file PDF condiviso su WhatsApp/chat.
- Se il prezzo sul sito è alto: dirlo al cliente di ignorarlo, Mauro lo sistema.
- Colori: riferirsi sempre ai codici Pantone (es. 1685C rosso, 430C grigio). Due fabbriche diverse (una fa rash, l'altra rash+short) → i colori non coincidono sempre, bisogna fare il "match" sui pantoni.

GESTIONE PROBLEMI
- Prodotto difettoso/errore: riconosci subito senza difenderti. Soluzione rapida preferita: sconto sul prossimo ordine. Alternativa: rifacimento (45–60 gg). Per urgenze: "li faccio di urgenza, risparmiamo un po' di tempo."
- Ordine incompleto: verifica fabbrica, avvisa subito dei tempi, offri rimborso come alternativa.
- Ritardi: sii trasparente ("i kimoni sono in ritardo", "sdoganano settimana prossima"). Proponi spedizione parziale se possibile.
- Kimoni neri: ricami sempre in bianco (non nero su nero).

FRASI TIPO DI MAURO
- "ciao. si, ci sono"
- "si si, come sempre i tempi sono 45-60"
- "i prezzi li trovi nel tuo listino personale nell'area privata del sito"
- "provo a sentire la fabbrica e ti aggiorno"
- "facciamo sconto al prossimo ordine"
- "approva le bozze sul sito e metti le taglie"
- "manda indirizzo che non me lo trova"
- "tranquillo parte sta settimana"

PAGAMENTO BONIFICO (promemoria)
- Beneficiary: Kano Co. Limited
- IBAN: LT293250064790539320 — BIC: REVOLT21
- Causale: numero ordine"""


def get_ai_reply(chat_id: str, user_message: str, extra_context: str = None) -> str:
    if not ANTHROPIC_API_KEY:
        return "Errore: ANTHROPIC_API_KEY non configurata."

    history = get_recent_messages(chat_id)
    knowledge_context = get_knowledge_context(user_message)

    system_parts = [SYSTEM_PROMPT]
    if knowledge_context:
        system_parts.append(f"Contesto dalla knowledge base interna:\n{knowledge_context}")
    if extra_context:
        system_parts.append(extra_context)
    system = "\n\n".join(system_parts)

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        return response.content[0].text

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
        r"ordine\s*#?\s*(\d[\d\-]+\d(?:-[A-Za-z0-9]+)?)",   # ordine #0466-05-26 o ordine 12345
        r"order\s*#?\s*(\d[\d\-]+\d(?:-[A-Za-z0-9]+)?)",    # order #0466-05-26 o order 12345
        r"\b(\d{3,4}-\d{2,4}-\d{2,4}(?:-[A-Za-z0-9]+)?)\b", # formato 0466-05-26 o 0495-05-26-A
        r"\b(\d{5,})\b",                                      # numero puro 5+ cifre
    ]

    for pattern in patterns:
        match = re.search(pattern, message.lower())
        if match:
            return match.group(1)

    return None


def is_order_request(message: str) -> bool:
    return try_extract_order_id(message) is not None

def extract_text_from_docx(file_path: str) -> str:
    doc = Document(file_path)
    texts = []

    def _cell_text(cell):
        return "\n".join(p.text for p in cell.paragraphs if p.text.strip())

    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if tag == "p":
            from docx.text.paragraph import Paragraph
            text = Paragraph(element, doc).text.strip()
            if text:
                texts.append(text)
        elif tag == "tbl":
            from docx.table import Table
            seen: set = set()
            for row in Table(element, doc).rows:
                for cell in row.cells:
                    t = _cell_text(cell)
                    if t and t not in seen:
                        seen.add(t)
                        texts.append(t)

    return "\n".join(texts)

def save_knowledge_document(title: str, category: str, content: str):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO knowledge_documents (title, category, content)
        VALUES (%s, %s, %s)
        """,
        (title, category, content),
    )

    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# TOOL USE — Haiku decide quale strumento chiamare (sostituisce il routing regex)
# Le funzioni di ricerca/formattazione esistenti restano identiche: cambia solo
# CHI decide di chiamarle e con quali parametri.
# ---------------------------------------------------------------------------

CHAT_TOOLS = [
    {
        "name": "cerca_ordine_per_numero",
        "description": (
            "Cerca un singolo ordine dato il suo NUMERO. Usalo quando l'utente "
            "fornisce o cita un numero d'ordine (se scritto a parole, convertilo "
            "in cifre prima di chiamare).\n"
            "Piattaforme:\n"
            "- 'custom' (kanokimonos.app): numeri con trattini tipo 0495-05-26-A\n"
            "- 'woocommerce': numeri puri (solo cifre) del sito web\n"
            "- 'btoweb': ordini di fabbrica/produttore, numeri tipo 062026-0004\n"
            "Se non sei sicuro della piattaforma, ometti 'piattaforma': verrà "
            "dedotta dal formato del numero."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {
                    "type": "string",
                    "description": "Il numero dell'ordine in cifre (es. '0495-05-26-A' o '12345').",
                },
                "piattaforma": {
                    "type": "string",
                    "enum": ["custom", "woocommerce", "btoweb"],
                    "description": "Piattaforma su cui cercare. Ometti se incerto.",
                },
            },
            "required": ["numero"],
        },
    },
    {
        "name": "cerca_ordini_per_cliente",
        "description": (
            "Cerca tutti gli ordini custom (kanokimonos.app) di un cliente. Il valore "
            "può essere il NOME della persona (anche composto, es. 'de tulio'), il "
            "NOME dell'AZIENDA/palestra (es. 'bjj lab') o anche una parte dell'EMAIL: "
            "la ricerca cerca la sottostringa in tutti e tre i campi. Usalo quando "
            "l'utente chiede gli ordini di una persona o di un'azienda. Estrai SOLO "
            "l'identificativo del cliente, mai parole come 'ordini', 'sopra', 'rashguard'. "
            "Restituisce dati strutturati (inclusi i prodotti) che puoi poi filtrare "
            "tu, ad esempio per mostrare solo gli ordini che contengono certi prodotti."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {
                    "type": "string",
                    "description": "Nome completo del cliente (es. 'bjj lab').",
                },
            },
            "required": ["nome"],
        },
    },
    {
        "name": "rispondi_dal_manuale",
        "description": (
            "Recupera informazioni dal manuale operativo interno / knowledge base "
            "per rispondere a domande procedurali o di policy aziendale (sconti, "
            "spedizioni, come si esegue una certa operazione, regole interne). Usalo "
            "quando la domanda NON riguarda un ordine o un cliente specifico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "argomento": {
                    "type": "string",
                    "description": "Parole chiave dell'argomento da cercare nel manuale.",
                },
            },
            "required": [],
        },
    },
]


TOOL_SYSTEM_SUFFIX = """
STRUMENTI E ONESTÀ
Hai a disposizione degli strumenti per cercare ordini, clienti e informazioni dal manuale. Regole:
- Per QUALSIASI richiesta su un ordine (numero) o su un cliente (nome), chiama lo strumento giusto. Non inventare mai lo stato di un ordine.
- Usa SOLO i dati restituiti dagli strumenti. Se uno strumento non restituisce risultati, dillo onestamente (es. "Non trovo ordini per X").
- Se un numero d'ordine è scritto a parole, convertilo in cifre prima di chiamare lo strumento. Un numero custom completo ha il formato NNNN-MM-YY con eventuale suffisso (es. 0495-05-26-A). Se l'utente fornisce solo una parte (es. solo "0495"), NON chiamare lo strumento con il valore parziale: chiedi il numero completo invece di indovinare.
- Per QUALSIASI domanda procedurale o di policy (sconti, prezzi a quantità, spedizioni, resi, tempi, "come si fa X", regole interne) DEVI chiamare rispondi_dal_manuale PRIMA di rispondere. Non rispondere mai a memoria su questi temi: il manuale è la fonte di verità. Solo se rispondi_dal_manuale restituisce NESSUN_CONTENUTO puoi dire onestamente che non trovi la procedura nel manuale.
- Se nessuno strumento è adatto e non conosci la risposta con certezza, dillo con onestà spiegando cosa non sai fare. NON rispondere mai con "Nessun ordine trovato per '<parola a caso>'" raschiando parole a caso dalla domanda.
"""


def _bto_search_by_number(numero: str):
    """btoweb non ha una ricerca per numero: prende tutti gli ordini e filtra."""
    data = search_bto_orders_all()
    if data.get("error"):
        return data
    numero_clean = numero.strip().lower()
    filtered = [
        row for row in data.get("results", [])
        if isinstance(row, dict)
        and str(row.get("order_number", "")).strip().lower() == numero_clean
    ]
    return {"results": filtered}


def _first_product_name(o: dict) -> str:
    names = [p.get("name") for p in (o.get("products") or []) if isinstance(p, dict) and p.get("name")]
    return ", ".join(names) if names else "N/A"


def _find_custom_order_and_group(numero: str) -> dict:
    """Cerca l'ordine custom per numero e, con lo STESSO fetch, ne ricava il gruppo
    (fratelli con lo stesso order_group_id). Un solo scarico dalla API."""
    data = search_custom_orders_raw(1000)
    if data.get("error"):
        return {"error": data["error"]}
    results = data.get("results", [])
    numero_clean = numero.strip().lower()
    match = next(
        (o for o in results if str(o.get("order_number", "")).strip().lower() == numero_clean),
        None,
    )
    if not match:
        return {"results": []}
    group_id = match.get("order_group_id")
    siblings = [o for o in results if group_id and o.get("order_group_id") == group_id]
    return {"results": [match], "group": siblings}


def format_order_group_summary(group_orders: list, main_number: str) -> str:
    """Riepilogo del gruppo: una riga per articolo. Vuoto se il gruppo ha <= 1 membro."""
    if not group_orders or len(group_orders) <= 1:
        return ""
    ordered = sorted(group_orders, key=lambda o: str(o.get("order_number") or ""))
    total = sum((o.get("quantity") or 0) for o in ordered)
    customer = next(
        (o.get("customer_name") or o.get("customer_email") for o in ordered
         if o.get("customer_name") or o.get("customer_email")),
        None,
    )
    header = "--- Gruppo ordine" + (f" (cliente: {customer})" if customer else "") + " ---"
    lines = [header, f"Fa parte di un gruppo di {len(ordered)} articoli, {total} pezzi totali:"]
    main_clean = str(main_number).strip().lower()
    for o in ordered:
        num = o.get("order_number") or o.get("id") or "N/A"
        qty = o.get("quantity")
        qty_str = f"{qty} pz" if qty is not None else "N/A"
        marker = "  ← ordine richiesto" if str(num).strip().lower() == main_clean else ""
        lines.append(f"• {num} | {_first_product_name(o)} | {qty_str} | {o.get('status') or 'N/A'}{marker}")
    return "\n".join(lines)


def tool_cerca_ordine_per_numero(numero: str, piattaforma: str = None) -> str:
    """Opzione (a): restituisce la stringa già formattata dalle funzioni esistenti."""
    numero = (numero or "").strip()
    if not numero:
        return "Nessun numero d'ordine fornito."

    def _fmt_custom(numero):
        """Ordine custom + eventuale riepilogo gruppo (solo se gruppo > 1 membro)."""
        res = _find_custom_order_and_group(numero)
        if res.get("error"):
            return f"Errore ricerca custom: {res['error']}"
        if res.get("results"):
            text = format_custom_order_for_human(res["results"][0])
            group = format_order_group_summary(res.get("group", []), numero)
            return text + ("\n\n" + group if group else "")
        return None

    def _fmt_wc(res):
        if res.get("error"):
            return f"Errore WooCommerce: {res['error']}"
        if res.get("results"):
            return format_order_for_human(res["results"][0])
        return None

    def _fmt_bto(res):
        if res.get("error"):
            return f"Errore btoweb: {res['error']}"
        if res.get("results"):
            return format_bto_orders_summary(res)
        return None

    if piattaforma == "custom":
        return _fmt_custom(numero) or f"Non ho trovato l'ordine custom {numero}."
    if piattaforma == "woocommerce":
        return _fmt_wc(search_orders_by_id(numero)) or f"Non ho trovato l'ordine WooCommerce {numero}."
    if piattaforma == "btoweb":
        return _fmt_bto(_bto_search_by_number(numero)) or f"Non ho trovato l'ordine btoweb {numero}."

    # Auto: deduci dal formato (stessa logica del vecchio routing regex)
    custom = _fmt_custom(numero)
    if custom:
        return custom
    if numero.isdigit():
        wc = _fmt_wc(search_orders_by_id(numero))
        if wc:
            return wc
    bto = _fmt_bto(_bto_search_by_number(numero))
    if bto:
        return bto
    return f"Non ho trovato l'ordine {numero} su nessuna piattaforma (custom, WooCommerce, btoweb)."


def tool_cerca_ordini_per_cliente(nome: str) -> dict:
    """Opzione (b): restituisce dati strutturati così Haiku può filtrarli."""
    nome = (nome or "").strip()
    if not nome:
        return {"error": "Nessun nome cliente fornito.", "ordini": []}
    res = search_custom_orders_by_name(nome)
    if res.get("error"):
        return {"error": res["error"], "ordini": []}
    ordini = []
    for o in res.get("results", []):
        ordini.append({
            "order_number": o.get("order_number") or o.get("id"),
            "customer_name": o.get("customer_name"),
            "status": o.get("status"),
            "payment_status": o.get("payment_status"),
            "created_at": o.get("created_at"),
            "products": [
                {
                    "name": p.get("name"),
                    "category": p.get("category"),
                    "subcategory": p.get("subcategory"),
                    "quantity": p.get("quantity"),
                }
                for p in (o.get("products") or [])
            ],
        })
    return {"cliente": nome, "totale": len(ordini), "ordini": ordini}


def tool_rispondi_dal_manuale(argomento: str = None, user_message: str = "") -> str:
    query = argomento or user_message or ""
    context = get_knowledge_context(query)
    if not context:
        return "NESSUN_CONTENUTO: il manuale non contiene informazioni su questo argomento."
    return context


def _execute_chat_tool(name: str, tool_input: dict, user_message: str):
    try:
        if name == "cerca_ordine_per_numero":
            return tool_cerca_ordine_per_numero(tool_input.get("numero"), tool_input.get("piattaforma"))
        if name == "cerca_ordini_per_cliente":
            return tool_cerca_ordini_per_cliente(tool_input.get("nome"))
        if name == "rispondi_dal_manuale":
            return tool_rispondi_dal_manuale(tool_input.get("argomento"), user_message)
        return {"error": f"Strumento sconosciuto: {name}"}
    except Exception as e:
        return {"error": f"Errore nell'esecuzione di {name}: {str(e)}"}


def chat_with_tools(chat_id: str, user_message: str) -> str:
    """Loop tool use: Haiku decide, eseguiamo le funzioni esistenti, Haiku compone."""
    if not ANTHROPIC_API_KEY:
        return "Errore: ANTHROPIC_API_KEY non configurata."

    history = get_recent_messages(chat_id)
    system = SYSTEM_PROMPT + "\n\n" + TOOL_SYSTEM_SUFFIX

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        for _ in range(4):  # cap iterazioni tool
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                system=system,
                tools=CHAT_TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                text_parts = [b.text for b in response.content if b.type == "text"]
                return "\n".join(text_parts).strip() or "Non ho una risposta per questo."

            # Esegui gli strumenti richiesti e rimanda i risultati a Haiku
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = _execute_chat_tool(block.name, block.input or {}, user_message)
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False, default=str)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

        # Superato il cap: ultima chiamata senza tool per forzare una risposta testuale
        final = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        text_parts = [b.text for b in final.content if b.type == "text"]
        return "\n".join(text_parts).strip() or "Non sono riuscito a completare la richiesta."

    except Exception as e:
        return f"Errore AI: {str(e)}"


@app.get("/")
def home():
    return {"status": "BambuUp Bot running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/feedback")
def submit_feedback(request: FeedbackRequest):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO feedback (question, wrong_reply, correct_reply) VALUES (%s, %s, %s)",
            (request.question, request.wrong_reply, request.correct_reply),
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "details": str(e)}


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

        # Routing via tool use: Haiku decide quale strumento chiamare e con
        # quali parametri (sostituisce la vecchia cascata di regex).
        bot_reply = chat_with_tools(request.chat_id, request.message)

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

@app.get("/custom-order-view")
def custom_order_view(order_number: str):
    try:
        result = search_custom_orders_by_number(order_number, 100)

        if result.get("error"):
            return result

        if not result.get("results"):
            return {"error": f"No custom order found for {order_number}"}

        return {
            "order_number": order_number,
            "formatted": format_custom_order_for_human(result["results"][0])
        }

    except Exception as e:
        return {"error": str(e)}
        
@app.get("/import-knowledge")
def import_knowledge():
    try:
        file_path = "manuale_operativo.docx"
        full_text = extract_text_from_docx(file_path)

        if not full_text:
            return {"error": "Nessun testo estratto dal documento"}

        chunk_size = 4000
        overlap = 200
        chunks = []
        start = 0
        while start < len(full_text):
            end = start + chunk_size
            chunks.append(full_text[start:end])
            if end >= len(full_text):
                break
            start = end - overlap

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("DELETE FROM knowledge_documents WHERE category = 'manuale';")
        for i, chunk in enumerate(chunks):
            cur.execute(
                "INSERT INTO knowledge_documents (title, category, content) VALUES (%s, %s, %s)",
                (f"Manuale Operativo Kano - Parte {i+1}", "manuale", chunk),
            )
        conn.commit()
        cur.close()
        conn.close()

        return {
            "status": "ok",
            "message": f"Knowledge imported in {len(chunks)} chunks",
            "total_characters": len(full_text),
            "chunks": len(chunks),
        }

    except Exception as e:
        return {"error": str(e)}

@app.get("/knowledge")
def get_knowledge():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, title, category, content, created_at
            FROM knowledge_documents
            ORDER BY created_at DESC
            LIMIT 10
            """
        )

        rows = cur.fetchall()
        cur.close()
        conn.close()

        results = []

        for row in rows:
            results.append({
                "id": row[0],
                "title": row[1],
                "category": row[2],
                "preview": row[3][:500],
                "created_at": str(row[4])
            })

        return {"documents": results}

    except Exception as e:
        return {"error": str(e)}

@app.get("/search-knowledge")
def search_knowledge(q: str):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            """
            SELECT content
            FROM knowledge_documents
            ORDER BY created_at DESC
            LIMIT 1
            """
        )

        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row or not row[0]:
            return {"result": "no knowledge"}

        text = row[0]
        query_lower = q.lower()

        chunks = text.split("\n")
        matches = []

        for c in chunks:
            if query_lower in c.lower():
                matches.append(c)

        return {
            "query": q,
            "matches": matches[:10]
        }

    except Exception as e:
        return {"error": str(e)}
