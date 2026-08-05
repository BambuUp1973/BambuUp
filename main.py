from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import re
import json
import difflib
import unicodedata
from datetime import datetime, timezone
from collections import Counter
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
    role: str = "staff"


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

def get_custom_resource(resource: str, limit: int = 50, status: str = None, extra_params: dict = None):
    headers = {
        "x-bot-api-key": KANOCUSTOM_API_KEY
    }

    params = {
        "resource": resource,
        "limit": limit
    }

    if status:
        params["status"] = status

    # Filtri della edge function (es. order_number / shipment_number /
    # only_discrepancies su fully_reconciliation). Senza questo inoltro i filtri
    # venivano scartati in silenzio e la risposta tornava NON filtrata con HTTP 200.
    if extra_params:
        for k, v in extra_params.items():
            if v is not None and k not in params:
                params[k] = v

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
    design_confirmed = None
    design_confirmed_at = None
    sizes_selected_at = None
    selected_sizes = None

    # In alcuni record admin_design_url + i flag checklist sono dentro selected_variations
    if isinstance(selected_variations, dict):
        admin_design_url = selected_variations.get("admin_design_url")
        admin_design_uploaded_at = selected_variations.get("admin_design_uploaded_at")
        design_confirmed = selected_variations.get("design_confirmed")
        design_confirmed_at = selected_variations.get("design_confirmed_at")
        sizes_selected_at = selected_variations.get("sizes_selected_at")
        selected_sizes = selected_variations.get("selected_sizes")

    return {
        "id": order.get("id"),
        "order_number": order.get("order_number"),
        "order_group_id": order.get("order_group_id"),
        "quantity": order.get("quantity"),
        # ATTENZIONE: 'status' della sorgente è un campo INTERNO di workflow
        # (conferma preventivo/design) che resta quasi sempre fermo su
        # 'pending_confirmation' anche per ordini prodotti, spediti e consegnati.
        # NON indica l'avanzamento reale e NON va mai mostrato come stato dell'ordine:
        # lo stato di business è order_status (etichette in CUSTOM_STATUS_LABELS).
        # Rinominato apposta perché nessuno lo legga per sbaglio come "stato".
        "workflow_status_interno": order.get("status"),
        "order_status": order.get("order_status"),
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
        "design_confirmed": design_confirmed,
        "design_confirmed_at": design_confirmed_at,
        "sizes_selected_at": sizes_selected_at,
        "selected_sizes": selected_sizes,
        "customer_files": order.get("customer_files"),
        "image_url": order.get("image_url"),
        "producer_assigned_at": order.get("producer_assigned_at"),
        "producer_file_path": order.get("producer_file_path"),
        "producer_file_uploaded_at": order.get("producer_file_uploaded_at"),
        "producer_courier": order.get("producer_courier"),
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


def _bto_get(params: dict):
    """Chiamata grezza alla edge function btoweb: restituisce il JSON completo
    (data + count/total/disclaimer/source). get_bto_resource appiattisce tutto in
    'results' e perde il disclaimer di stock, che qui va invece propagato."""
    if not BTO_API_KEY:
        return {"error": "BTO_API_KEY non configurata."}

    try:
        response = requests.get(
            BTO_API_URL,
            headers={"x-api-key": BTO_API_KEY},
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
        return {"data": data}
    if isinstance(data, dict):
        return data
    return {"error": "Struttura risposta btoweb non riconosciuta", "details": str(data)}


def search_bto_orders_all():
    return get_bto_resource({})


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
    # Stato di business REALE = order_status (con etichette usate anche nelle statistiche),
    # NON il campo workflow 'status' (spesso fermo su 'pending_confirmation' e fuorviante).
    os_code = order.get("order_status")
    os_label = CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A")
    lines.append(f"Stato ordine: {os_label}" + (f" [{os_code}]" if os_code else ""))
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
    # Checklist avanzamento: flag espliciti Sì/No ricavati da selected_variations
    # (design/taglie) e dai campi producer_*.
    si = lambda b: "Sì" if b else "No"
    design_caricato = bool(order.get("admin_design_url"))
    design_approvato = order.get("design_confirmed") is True
    taglie_confermate = bool(order.get("sizes_selected_at")) or bool(order.get("selected_sizes"))
    producer_file_pronto = bool(order.get("producer_file_uploaded_at")) or bool(order.get("producer_file_path"))
    spedito_produttore = bool(order.get("producer_shipped_at"))
    ship_extra = ""
    if spedito_produttore:
        extra = " | ".join(filter(None, [order.get("producer_courier"), order.get("producer_tracking")]))
        ship_extra = f" ({extra})" if extra else ""

    lines.append("Checklist avanzamento:")
    lines.append(f"- Design caricato: {si(design_caricato)}")
    lines.append(f"- Design approvato dal cliente: {si(design_approvato)}")
    lines.append(f"- Taglie confermate: {si(taglie_confermate)}")
    lines.append(f"- Producer file pronto: {si(producer_file_pronto)}")
    lines.append(f"- Spedito dal produttore: {si(spedito_produttore)}{ship_extra}")
    lines.append("")
    # Fuori dalla checklist di proposito: customer_files/image_url sono vuoti
    # anche su ordini avanzati, quindi non è un Sì/No. Dentro l'elenco veniva
    # rimarcata con una ❌ e letta come "logo non ricevuto".
    lines.append("Logo cliente: dato non presente a sistema (né ricevuto né mancante)")
    lines.append("")
    lines.append(f"URL bozza admin: {order.get('admin_design_url') or 'N/A'}")
    lines.append(f"Bozza admin caricata il: {order.get('admin_design_uploaded_at') or 'N/A'}")
    lines.append(f"Design approvato il: {order.get('design_confirmed_at') or 'N/A'}")
    lines.append(f"Taglie inserite il: {order.get('sizes_selected_at') or 'N/A'}")
    lines.append(f"Dettaglio taglie: {order.get('selected_sizes') or 'N/A'}")
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
        # Stato di business REALE = order_status, NON il campo workflow stale 'status'.
        os_code = order.get("order_status")
        os_label = CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A")
        lines.append(
            f"• {order.get('order_number') or order.get('id') or 'N/A'} | "
            f"stato: {os_label}" + (f" [{os_code}]" if os_code else "") + " | "
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


# Overlap usato in import_knowledge() per lo chunking del manuale. Deve restare
# allineato a quel valore per ricostruire il testo contiguo dai chunk.
KNOWLEDGE_CHUNK_OVERLAP = 200


def _reconstruct_manuale_text() -> str:
    """Ricostruisce il testo contiguo del manuale dai chunk in DB, rimuovendo
    l'overlap. Serve per estrarre blocchi tabellari interi (es. la guida taglie)
    che il retrieval per-riga frammenterebbe."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT title, content FROM knowledge_documents WHERE category = 'manuale'")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return ""

    def _part_num(title):
        m = re.search(r"(\d+)", title or "")
        return int(m.group(1)) if m else 0

    rows = sorted(rows, key=lambda r: _part_num(r[0]))
    text = rows[0][1] or ""
    for _, content in rows[1:]:
        content = content or ""
        text += content[KNOWLEDGE_CHUNK_OVERLAP:]
    return text


def get_size_guide_block() -> str:
    """Restituisce l'intero blocco GUIDA TAGLIE UFFICIALE dal manuale (contiguo)."""
    full = _reconstruct_manuale_text()
    idx = full.find("GUIDA TAGLIE UFFICIALE")
    return full[idx:] if idx >= 0 else ""


# Parole che indicano con certezza una domanda sulle TAGLIE (evita falsi positivi
# tipo "minimo rashguard", che riguarda i minimi, non le misure).
SIZE_QUERY_WORDS = ("taglia", "taglie", "size", "misura", "misure", "statura")


def _is_size_query(text: str) -> bool:
    t = (text or "").lower()
    if any(w in t for w in SIZE_QUERY_WORDS):
        return True
    return "altezza" in t and ("peso" in t or "kg" in t)


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
- Patch standard: min 10 pz, produzione 45–60 gg (come tutti i custom)
- Patch DTF su kimono Team Gi (modello da catalogo): consegna 7–10 gg, min 10 pz
- Patch DTF su altri modelli kimono: 7–10 gg + 2–3 gg aggiuntivi per il modello, min 10 pz

Resi e rimborsi:
- Procedura entro 14 giorni dalla ricezione
- Indirizzo resi (italiani ed esteri): BJJ Store, Via Cavalcanti 4, 30038 Spinea (VE), Italia
- Rimborsi in store credit: solo per B2B, palestre, accademie — mai in denaro
- Cambio taglia: contributo spedizione €5,90
- Errore nostro: reso a nostro carico
- Non proporre rimborso a chi chiede solo cambio taglia
- Difetto di produzione segnalato: conferma SUBITO che sostituiamo l'articolo a nostro carico, POI chiedi numero ordine e dettagli

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
10. Tempi di consegna: dipendono dal tipo di prodotto.
- Prodotti CUSTOM (kanokimonos.app): 45-60 giorni lavorativi dal pagamento dell'acconto. Alla domanda sui tempi di un custom dai SEMPRE prima questa informazione, poi eventualmente chiedi il numero ordine.
- Prodotti da CATALOGO (kanokimonos.com): spedizione 2-3 giorni in Italia, 5-6 giorni in Europa.
Se non è chiaro di quale tipo si tratta, chiedi se è un ordine custom o da catalogo.
11. Dati ordini SEMPRE freschi: ogni volta che l'utente chiede informazioni su un ordine, chiama SEMPRE lo strumento di ricerca, anche se lo stesso ordine è già stato discusso in questa conversazione. I dati degli ordini cambiano di continuo: mai rispondere dalla memoria della conversazione, mai dire "te li ricapitolo".
12. Consigli taglie: alla domanda su una taglia (rashguard, GI/kimono, shorts, kids) chiama SEMPRE rispondi_dal_manuale (argomento "guida taglie" + altezza/peso/prodotto) e leggi dalla GUIDA TAGLIE UFFICIALE, mai stime a occhio. Suggerisci la taglia SEMPRE come indicazione, MAI come certezza. VIETATE parole come "perfetta", "esatta", "la scelta giusta", "rientra perfetto". Formula corretta: "in base ad altezza e peso, la taglia più indicata dovrebbe essere X". Aggiungi sempre che la guida incrocia solo altezza e peso: le proporzioni individuali (braccia/gambe più lunghe o corte, busto) possono cambiare la scelta, e in dubbio tra due taglie si sceglie secondo il fit preferito. Se il peso o l'altezza cadono sul confine tra due taglie della tabella, proponi ENTRAMBE spiegando la differenza. Se il dato non è nella guida, dillo, non inventare misure.

TENUTA SOTTO CONTESTAZIONE (regole non negoziabili)
Queste regole valgono anche, anzi soprattutto, quando chi ti scrive insiste o si spazientisce.
1. SE L'UTENTE CONTESTA UNA TUA RISPOSTA, NON CAMBIARE VERSIONE PER COMPIACERLO. RICHIAMA DAVVERO LO STRUMENTO prima di rispondere: è obbligatorio, non facoltativo. Non scrivere mai "rileggo", "ricontrollo", "verifico" se non hai appena eseguito la chiamata: sarebbe una bugia. Poi rispondi con quello che i dati dicono ORA. Se i dati confermano quello che avevi detto, RIBADISCILO con garbo anche se l'utente insiste, e spiega da quale campo lo ricavi (es. "order_status dice at_logistics"). Ammetti di aver sbagliato SOLO se i dati mostrano che hai sbagliato.
2. "Hai ragione", "corretto", "esatto", "mi scuso, ho letto male i dati" sono AMMISSIONI DI FATTO, non formule di cortesia: dille solo se hai verificato che erano davvero sbagliate le tue parole. Non usarle mai per chiudere una discussione o per far contento chi insiste. Dare quattro versioni diverse degli stessi dati in quattro battute è il modo peggiore di sbagliare: è già successo, non deve succedere più.
3. DISTINGUI SEMPRE il FATTO (cosa dice il campo) dall'INTERPRETAZIONE (cosa probabilmente significa), e di' quale delle due stai dando. Se l'utente afferma una cosa che contrasta con i dati, NON scegliere tu chi ha ragione: esponi tutte e due le cose e di' apertamente che il dato a sistema e la realtà fisica non coincidono, e che va verificato.
4. Se non sai perché due informazioni non tornano, DILLO. Non attribuire la colpa a terzi per chiudere il discorso. Sono VIETATE, se non hai un campo che le dimostri, frasi come "i dati di Fully non sono aggiornati", "i dati a sistema sono vecchi", "il sistema non ha sincronizzato", "il tracciamento è rimasto indietro". Sono vietate anche nella forma "la sincronizzazione fra Fully e i nostri dati a volte rimane indietro" e non devi MAI presentare l'ipotesi come cosa nota o frequente ("capita spesso", "non è raro", "succede"): non hai nessun dato che lo dica. L'UNICO dato che parla dell'età dell'informazione è la data di sincronizzazione della fotografia (fotografia_del / nota_fotografia): se vuoi dire che il dato potrebbe non essere attuale, cita quella data e fermati lì; se la nota non segnala che è vecchia, non dire che lo è. Che il magazzino abbia poi fatto altro NON lo sai: dillo come cosa da verificare, non come spiegazione già trovata.

LE TUE FONTI (dille con precisione)
- Le tue UNICHE fonti sono le API di kanokimonos.app (ordini custom, spedizioni, conteggi) e di btoweb (ordini di fabbrica), lette tramite i tuoi strumenti. Nient'altro.
- NON parli con Fully e non leggi sistemi di Fully. È VIETATO indicare come tua fonte "il sistema di Fully", "il portale Fully", "secondo Fully", "risulta a Fully" — anche in forme sfumate tipo "sincronizzata dal sistema di Fully": la fonte che citi è UNA SOLA, kanokimonos.app. I numeri di conteggio sono una FOTOGRAFIA salvata su kanokimonos.app con la sua data di sincronizzazione: di' "fotografia salvata su kanokimonos.app, sincronizzata il <data>" e basta. Puoi dire che è Fully a contare la merce fisicamente (è un fatto), ma non che Fully sia la fonte da cui leggi.
- Il portale Fully (fullyview.si) è uno strumento che consultano le persone, NON una tua fonte: puoi consigliare a un collega di guardarlo, non puoi dire che i tuoi dati vengono da lì.
- Quando ti chiedono quali sono le tue fonti, rispondi con il nome della PIATTAFORMA e dello STRUMENTO da cui hai letto (es. "ordini custom di kanokimonos.app tramite tracciamento_fully"), senza girarci intorno.

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
- Non promettere mai foto dei prodotti prima della consegna. Le foto si fanno solo occasionalmente al sample in fabbrica: se il cliente le chiede, spiega che non è una prassi standard, senza promettere
- Non offrire mai di "creare un preventivo" né comunicare prezzi: rimanda sempre e solo al listino personale nell'area privata su kanokimonos.app (eccezione super-VIP con prezzi già concordati)

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
- Cinture standard da catalogo: evasione/spedizione in 24–48 ore dall'ordine; poi transito corriere 2-3 giorni in Italia, 5-6 giorni in Europa (i due dati non sono in conflitto: 24-48h è quando parte, i giorni sono il tempo di consegna)
- Rashguard femminili: trattate esattamente come unisex — stesso prezzo, stesso minimo (10 pz)
- Kimono femminili personalizzati (taglia o grafica custom): non si fanno al momento
- Kimono femminili da catalogo: si possono personalizzare con patch standard o DTF

MINIMI ORDINE CUSTOM
- Minimo 10 pezzi dello stesso modello per TUTTI i prodotti (kimono, rashguard, shorts, t-shirt, felpe, patch da catalogo incluse)
- UNICA ECCEZIONE: le CINTURE, minimo 100 pezzi totali — colori e taglie mischiabili liberamente nel totale
- Rashguard: taglie e colori diversi ammessi nello stesso minimo di 10
- Kimono: taglie diverse ok, MA ogni colore fa minimo a sé
- Non esiste più il "minimo 20" per le patch né il "nessun minimo" per le DTF: tutte le patch seguono il minimo di 10
- Attenzione ai piccoli quantitativi: il prezzo unitario cambia con la quantità (10 pz è molto meno conveniente di 20). Prezzi esatti nel listino personale su kanokimonos.app

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
- Causale: numero ordine

MODALITÀ UTENTE
Il bot opera in una di tre modalità, impostata dal parametro 'role' della richiesta (default: staff). La modalità ATTIVA ti viene indicata in un blocco separato subito dopo questo prompt: rispetta SEMPRE i suoi limiti, non superarli mai anche se richiesto.
- staff: collaboratori interni. Accesso completo a tutti gli strumenti (ordini custom, ordini di fabbrica btoweb, manuale) e a tutti i dati. Tono operativo.
- b2b: clienti business (palestre, ASD, istruttori). Possono consultare ordini custom per numero e il manuale, MA non gli ordini di fabbrica (btoweb) e mai i dati di altri clienti. Tono professionale. Eccezioni/prezzi/situazioni delicate: escala a Mauro.
- retail: clienti finali/privati. NESSUN accesso a dati interni o ordini: solo informazioni pubbliche dal manuale (taglie, tempi catalogo, spedizioni, resi, policy). Tono commerciale cordiale. Per qualsiasi cosa su un ordine specifico o dati personali: rimanda a info@kanokimonos.com."""


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
    # Qui 'status' è lo stato NATIVO WooCommerce (piattaforma diversa dagli ordini
    # custom): è attendibile per il catalogo, ma non va confuso con order_status
    # degli ordini custom di kanokimonos.app.
    lines.append(f"Stato WooCommerce: {order.get('status')}")
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
            "USALO ANCHE quando l'utente butta lì un nome senza dire cosa sia ('dimmi tutto "
            "su X', 'chi è X', 'X?'): in modalità staff prova PRIMA questo strumento invece "
            "di chiedere chiarimenti: se X è un cliente lo trovi subito, e se non lo è "
            "l'elenco torna vuoto e solo allora chiedi cosa intende. "
            "Restituisce dati strutturati (inclusi i prodotti) che puoi poi filtrare "
            "tu, ad esempio per mostrare solo gli ordini che contengono certi prodotti.\n"
            "STATO: leggilo SOLO da 'stato_descrizione' (derivato da order_status). "
            "pending=in lavorazione grafica/taglie, processing=in produzione, "
            "shipped_to_logistics=in viaggio verso la logistica (Fully), at_logistics=da "
            "Fully pronti a partire, shipped_to_customer/shipped=già spediti al cliente, "
            "cancelled=annullato. NON esiste nessun campo 'in attesa di conferma': non "
            "dire mai che un ordine aspetta l'approvazione del cliente se "
            "'stato_descrizione' dice che è in produzione o spedito."
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
        "name": "statistiche_ordini_custom",
        "description": (
            "Conteggi AGGREGATI sugli ordini custom (kanokimonos.app). Usalo per domande "
            "quantitative/di riepilogo tipo: 'quanti ordini non pagati?', 'quanti in "
            "produzione?', 'quanti spediti ai clienti?', 'quanti ordini questo mese?', "
            "oppure per sapere se un cliente ha pagato / se i suoi ordini sono partiti.\n"
            "Filtri opzionali:\n"
            "- 'cliente': nome persona, azienda/palestra o parte dell'email (sottostringa). "
            "Se lo passi, ricevi anche l'elenco per-ordine (stato + pagamento) del cliente.\n"
            "- 'mese': 'corrente' per il mese in corso, un nome di mese (es. 'giugno') o "
            "'YYYY-MM'. Filtra per data di creazione dell'ordine.\n"
            "Stati ordine (order_status) e loro significato: pending=in lavorazione grafica/"
            "taglie, processing=in produzione, shipped_to_logistics=in viaggio verso la "
            "logistica (Fully), at_logistics=da Fully pronti a partire, shipped_to_customer="
            "spediti al cliente, shipped=stato storico legacy (conta come spedito ma va "
            "dichiarato a parte), cancelled=annullati. Pagamenti (payment_status): "
            "fully_paid=pagati, advance_paid=acconto, unpaid=non pagati.\n"
            "IMPORTANTE: questo strumento NON fornisce importi in euro. Se ti chiedono "
            "'quanto abbiamo incassato' o importi economici, NON usarlo per inventare numeri: "
            "rispondi che i dati economici si consultano solo su kanokimonos.app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente": {
                    "type": "string",
                    "description": "Nome/azienda/email del cliente per filtrare (sottostringa). Ometti per il totale.",
                },
                "mese": {
                    "type": "string",
                    "description": "'corrente', nome mese italiano, o 'YYYY-MM'. Ometti per tutti i mesi.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "prezzi_listino",
        "description": (
            "Prezzi di LISTINO ufficiali dei prodotti e delle patch (kanokimonos.app). Usalo "
            "SOLO quando serve un prezzo di listino interno (es. 'quanto costa una patch DTF "
            "10x10 a listino?', 'prezzo listino leggings'). Passa in 'query' le parole chiave: "
            "per le PATCH la misura (es. '10x10' o '10 cm' -> lato in cm; il tipo di stampa "
            "come DTF NON incide sul prezzo); per i PRODOTTI il nome del capo (es. 'leggings', "
            "'rashguard'). Usa 'tipo'='patch' per le patch, 'prodotti' per i capi; ometti per "
            "entrambi. Il prezzo dipende dalla FASCIA DI QUANTITÀ: lo strumento restituisce "
            "tutta la scala prezzi/quantità. Riporta la fascia giusta se conosci la quantità, "
            "altrimenti presenta la scala (per i prodotti c'è anche il prezzo VIP)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Parole chiave del prodotto/patch (es. 'DTF 10x10', 'rashguard manica lunga').",
                },
                "tipo": {
                    "type": "string",
                    "enum": ["patch", "prodotti"],
                    "description": "'patch' per patch_pricing, 'prodotti' per product_pricing. Ometti per entrambi.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "catalogo_btoweb",
        "description": (
            "Catalogo di fabbrica btoweb: (1) ANAGRAFICA PRODOTTI — SKU/EAN, nome, taglia, "
            "colore; (2) PRODUZIONE — contatori della pipeline di produzione. Usalo per "
            "'che taglie esistono per <prodotto>?', 'a che prodotto corrisponde lo SKU/EAN "
            "<numero>?', 'quanti <prodotto> sono in produzione?'. "
            "Passa 'sku' per la ricerca esatta di uno SKU/EAN, altrimenti 'query' con le "
            "parole chiave del nome prodotto. "
            "tipo='prodotti' (default) = anagrafica; tipo='produzione' = contatori di produzione. "
            "REGOLA CRITICA su tipo='produzione': i numeri restituiti sono contatori della "
            "PIPELINE DI PRODUZIONE, NON la giacenza vendibile a magazzino. 'in_produzione' = "
            "ordinato al fornitore e non ancora spedito; 'prodotti_e_spediti_dal_fornitore' = "
            "prodotto su spedizioni già partite; 'ricevuti_conformi'/'mancanti'/'danneggiati' = "
            "riconciliazione in ingresso su Fully. Non dire MAI che un capo è 'disponibile', "
            "'in stock' o 'pronto da vendere' sulla base di questi numeri, e riporta sempre "
            "esplicitamente il disclaimer che si tratta di dati di produzione e non di "
            "disponibilità di magazzino. Anche l'anagrafica indica solo che il prodotto esiste "
            "a catalogo, non che sia disponibile. "
            "Questa fonte NON contiene PREZZI: non dedurre né stimare prezzi da qui; per i "
            "prezzi usa prezzi_listino."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Parole chiave del nome prodotto (es. 'BJJ Belt Competition', 't-shirt kano').",
                },
                "sku": {
                    "type": "string",
                    "description": "SKU/EAN esatto da cercare (es. '7427115006810'). Ha priorità su 'query'.",
                },
                "tipo": {
                    "type": "string",
                    "enum": ["prodotti", "produzione"],
                    "description": "'prodotti' = anagrafica/taglie (default); 'produzione' = contatori pipeline di produzione.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "ordini_per_produttore",
        "description": (
            "Ordini di FABBRICA (btoweb) di un PRODUTTORE/FORNITORE: cosa deve ancora "
            "arrivare da lui, con stato, data di arrivo prevista, prodotti e quantità. "
            "Usalo per domande tipo 'cosa deve arrivare da <produttore>?', 'quali ordini "
            "ha in produzione <produttore>?', 'quando arriva la merce di <produttore>?', "
            "'ordini di <produttore>'.\n"
            "PRODUTTORI (sono FORNITORI, NON clienti): Martin, 7punch (chiamato anche "
            "Seventh Punch), Wearica, Tussle (nei dati di btoweb compare come "
            "tusslesports@gmail.com), Fair Tex. Se l'utente nomina uno di questi NON usare "
            "cerca_ordini_per_cliente: non sono clienti. I produttori sono POCHI e NOTI: "
            "sono quelli qui sopra. Un nome che non somiglia a nessuno di loro NON è un "
            "produttore nuovo, è quasi sempre un CLIENTE (persona, palestra, ASD, azienda) "
            "e va cercato con cerca_ordini_per_cliente. Puoi comunque passarlo qui se hai "
            "un dubbio, ma se ricevi 'trovato': false NON chiudere dicendo che non risulta: "
            "riprova SUBITO con cerca_ordini_per_cliente.\n"
            "PASSA IL NOME COSÌ COM'È, anche se ti sembra scritto male: lo strumento tollera "
            "maiuscole, accenti, spazi, punteggiatura e refusi (es. 'wearika', 'martn'). "
            "Non correggerlo tu e non tirare a indovinare. Come leggere la risposta:\n"
            "- 'nota_interpretazione' presente = il nome è stato risolto su un produttore "
            "diverso da quello digitato: dichiaralo all'utente prima dei dati.\n"
            "- 'richiesta_chiarimento': true = più produttori compatibili: NON scegliere e "
            "NON mostrare ordini, elenca i 'candidati' e chiedi quale intende.\n"
            "- 'trovato': false senza candidati = quel nome non è un produttore: prima di "
            "rispondere riprova con cerca_ordini_per_cliente (vedi la 'nota' nel risultato), "
            "e solo se anche lì non c'è nulla dillo elencando "
            "'produttori_presenti_negli_ordini'.\n"
            "Gli ordini tornano già ordinati con i più imminenti in cima. Stati: "
            "'nuovo' = creato ma non ancora avviato, 'in_produzione' = in lavorazione dal "
            "fornitore, 'spedito' = già partito dal produttore. Quando la data di arrivo "
            "prevista o il dettaglio prodotti non sono valorizzati alla fonte, dillo "
            "esplicitamente invece di stimarli. Questa fonte NON contiene prezzi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "produttore": {
                    "type": "string",
                    "description": "Nome del produttore così come lo ha detto l'utente (es. 'Martin', 'Wearica', 'Tussle').",
                },
            },
            "required": ["produttore"],
        },
    },
    {
        "name": "tracciamento_fully",
        "description": (
            "Tracciamento COMPLETO (dalla A alla Z) di un ordine custom kanokimonos: "
            "contenuto e stato -> produttore e partenza dalla fabbrica (corriere/tracking) "
            "-> spedizione ASN verso la logistica Fully (numero ASN + file) -> numero di "
            "carico Fully -> conteggio di Fully riga per riga (buoni/mancanti/danneggiati/"
            "in più) -> pronto o no per il cliente -> eventuale ripartenza verso il cliente. "
            "Usalo per 'traccia l'ordine X', 'a che punto è X con Fully?', 'la merce di X è "
            "arrivata? manca qualcosa?', 'com'è andato il carico/la spedizione ASN-...?'.\n"
            "Ingresso: 'numero' = numero d'ordine custom (es. 0495-05-26-A) OPPURE un numero "
            "di spedizione ASN (es. ASN-Martin-2026-07-20-001); in alternativa 'cliente' = "
            "nome persona/azienda/email, tollerante a refusi e maiuscole: passalo così com'è, "
            "non correggerlo (stesse regole di ordini_per_produttore per nota_interpretazione, "
            "richiesta_chiarimento e candidati).\n"
            "REGOLE OBBLIGATORIE quando riporti i risultati:\n"
            "- ARRIVO DELLA MERCE: l'unica fonte è il blocco 'arrivo_in_fully' "
            "(stato_arrivo + in_parole + come_lo_sappiamo). NON dedurre l'arrivo dallo "
            "'stato_asn' (che riguarda solo la partenza dalla fabbrica) né da date di "
            "ricezione. I tre casi, da riportare con queste parole:\n"
            "  * 'arrived' = Fully ha ricevuto E finito di contare (se 'come_lo_sappiamo' "
            "dice spunta manuale, di' che risulta arrivata per verifica manuale di Bambu "
            "e che il conteggio Fully non esiste);\n"
            "  * 'counting_in_progress' = merce ARRIVATA in magazzino ma conteggio NON "
            "ancora concluso da Fully: dillo esplicitamente, riporta 'avanzamento_conteggio_"
            "fully' (righe contate su totale), di' che i numeri sono PROVVISORI e che si "
            "può SOLLECITARE FULLY. Non dire mai che è 'arrivata e contata';\n"
            "  * 'no_arrival_evidence' = nessuna prova di arrivo: dichiaralo così, senza "
            "dedurre né che sia arrivata né che sia persa.\n"
            "- Dichiara SEMPRE da dove viene il dato di arrivo ('come_lo_sappiamo'): "
            "conteggio automatico di Fully, conteggio parziale in corso, o verifica "
            "manuale dell'admin.\n"
            "- 'dato_storico_spunta_manuale' è solo storia etichettata: NON è una prova "
            "di arrivo, non usarlo per dire che la merce è arrivata.\n"
            "- Al cliente si spedisce quanto Fully ha CONTATO (buoni), ma il prezzo è sulla "
            "quantità ORDINATA. Quindi: 'in_piu' > 0 va segnalato SEMPRE come pezzi in più "
            "da consegnare E DA FATTURARE (anche se la fonte non lo marca come discrepanza); "
            "'mancanti' o 'danneggiati' > 0 = il cliente riceve meno di quanto ha pagato: "
            "dillo con queste parole; una riga con 0 pezzi buoni NON partirà affatto.\n"
            "- Distingui SEMPRE le anomalie DA GESTIRE da quelle GIÀ GESTITE (campi "
            "'anomalie_da_gestire' / 'anomalie_gia_gestite').\n"
            "- 'verifica_manuale_bambu' è una verifica manuale fatta da Bambu, MAI una "
            "conferma di Fully: usa esattamente queste parole.\n"
            "- Il conteggio è una FOTOGRAFIA ('fotografia_del'), non una lettura in diretta: "
            "se la nota dice che è vecchia, dichiaralo.\n"
            "- Se mancano numero di carico, righe di conteggio o spedizione, lo strumento lo "
            "dice esplicitamente: riportalo così, senza dedurre cosa sia successo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {
                    "type": "string",
                    "description": (
                        "Numero d'ordine custom (es. '0495-05-26-A') o numero di "
                        "spedizione ASN (es. 'ASN-Martin-2026-07-20-001')."
                    ),
                },
                "cliente": {
                    "type": "string",
                    "description": (
                        "In alternativa al numero: nome persona, azienda/palestra o email "
                        "del cliente, così come lo ha scritto l'utente."
                    ),
                },
            },
            "required": [],
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
- STATO 'shipped_to_customer': significa che l'ordine è stato MARCATO COME SPEDITO SU kanokimonos.app. NON è la prova che il pacco sia fisicamente partito da Fully né che sia in viaggio. Riportalo con queste parole ("risulta marcato come spedito su kanokimonos.app"); non trasformarlo mai in "in transito", "in consegna", "consegnato", "il cliente ha ricevuto la merce", "è arrivato al cliente". Vale anche a metà risposta: se hai appena detto la frase giusta, non aggiungere due righe dopo una frase che dà la consegna per avvenuta. Il sistema non ha alcuna conferma di ricezione da parte del cliente: l'unica verifica è il tracking della spedizione AL CLIENTE. E attenzione a non scambiare i due viaggi: il tracking del blocco 'fabbrica' è il viaggio produttore -> Fully e NON dice niente sulla consegna al cliente. La spedizione al cliente sta solo nel blocco 'ripartenza_verso_cliente': se quel blocco non c'è, a sistema NON esiste un tracking della spedizione al cliente e devi dirlo apertamente, senza offrire il tracking di fabbrica al suo posto. Se non c'è un tracking valorizzato, dillo: senza tracking non sai dove sia il pacco.
- CONTESTAZIONI, REGOLA MECCANICA: se il messaggio dell'utente contesta, corregge o afferma qualcosa di diverso da quello che hai appena detto su un ordine o una spedizione (segnali tipici: "no", "ma", "guarda", "in realtà", "sei sicuro?", "sono già stati consegnati/spediti/pagati"), la PRIMA cosa che fai è RICHIAMARE LO STRUMENTO. Sempre, senza eccezioni, ANCHE SE lo hai già chiamato nel turno precedente e anche se sei certo della risposta: i dati possono essere cambiati e comunque devi rispondere su dati appena letti, non a memoria. Vietato scrivere "rileggo"/"ricontrollo"/"verifico" senza aver eseguito la chiamata in questo turno.
- CONTESTAZIONI: se l'utente mette in dubbio un dato che hai appena dato, richiama lo strumento e rileggi, non cambiare risposta per assecondarlo. Se il dato è quello che avevi detto, ripetilo citando il campo. Se l'utente afferma un fatto fisico che i dati non confermano (es. "sono già stati consegnati"), non riscrivere lo stato: di' cosa dice il campo, che la sua informazione non risulta a sistema e che le due cose vanno riconciliate.
- Per domande AGGREGATE/di riepilogo sugli ordini custom ("quanti ordini...", "quanti pagati/non pagati/in produzione/spediti", "il cliente X ha pagato / è partito", conteggi per mese) usa statistiche_ordini_custom. Quando riporti gli spediti al cliente e sono presenti ordini con stato storico 'shipped', dichiara SEMPRE la distinzione (es. "123 spediti al cliente + 46 con stato storico legacy 'shipped'").
- DATI ECONOMICI IN EURO: non comunicare MAI importi incassati, somme pagate o totali in euro degli ordini. Se ti chiedono "quanto abbiamo incassato", quanto vale un mese/cliente in euro e simili, rispondi cortesemente che i dati economici sono riservati e si consultano solo su kanokimonos.app. I CONTEGGI (quanti pagati/acconto/non pagati) invece puoi darli.
- PREZZI DI LISTINO: solo in modalità STAFF puoi rispondere sui prezzi di listino usando prezzi_listino. Per clienti B2B/retail continua a rimandare al listino personale nell'area privata, senza comunicare prezzi.
- CATALOGO BTOWEB (catalogo_btoweb, solo STAFF): è la fonte per taglie a catalogo e SKU/EAN. I dati di 'produzione' NON sono giacenza vendibile: quando li riporti dichiara sempre che sono contatori della pipeline di produzione e non disponibilità di magazzino, e non dire mai che un capo è "disponibile" o "in stock" basandoti su di essi. Questa fonte non contiene prezzi: per i prezzi usa solo prezzi_listino.
- ORDINI DI FABBRICA PER PRODUTTORE (ordini_per_produttore, solo STAFF): quando la domanda riguarda cosa deve arrivare da un produttore/fornitore ("cosa deve arrivare da X", "quali ordini ha in produzione X", "quando arriva la merce di X") usa questo strumento e NON cerca_ordini_per_cliente: i produttori sono fornitori, non clienti. Riporta stato, data di arrivo prevista, prodotti e quantità esattamente come tornano dallo strumento; se una data o il dettaglio prodotti non sono valorizzati alla fonte dichiaralo, non stimarli. ATTENZIONE al caso opposto: i produttori sono POCHI e NOTI (Martin, 7punch/Seventh Punch, Wearica, Tussle, Fair Tex), quindi se lo strumento risponde 'trovato: false' quel nome quasi certamente NON è un produttore ma un CLIENTE (persona, palestra, ASD, azienda): riprova SUBITO con cerca_ordini_per_cliente prima di dire all'utente che non risulta nulla. Non chiudere mai con "non lo trovo" avendo provato una sola delle due strade.
- TRACCIAMENTO FULLY (tracciamento_fully, solo STAFF): per "traccia l'ordine X", "è arrivato a Fully?", "manca qualcosa sul carico?" usa questo strumento. Regole fisse: i pezzi in più vanno SEMPRE segnalati come "da consegnare e da fatturare" (si spedisce quanto Fully ha contato, si fattura la quantità ordinata); mancanti/danneggiati = merce che il cliente ha pagato e non riceve; una riga con 0 pezzi buoni non partirà affatto; distingui le anomalie da gestire da quelle già gestite; la verifica manuale di Bambu non è MAI una conferma di Fully; il conteggio è una fotografia, non una lettura in diretta; se un dato (carico, conteggio, spedizione) non esiste a sistema dillo apertamente, non dedurre.
"""


# --- MODALITÀ UTENTE (role) --------------------------------------------------
# Ogni ruolo seleziona: (1) il blocco di prompt attivo iniettato dopo SYSTEM_PROMPT,
# (2) la lista di tool passata a Haiku, (3) le piattaforme ordini consentite.
# staff è l'unico attivo di default; b2b e retail sono predisposti (non attivati).

ROLE_PROMPTS = {
    "staff": (
        "MODALITÀ ATTIVA: STAFF. Stai assistendo un collaboratore interno. "
        "Hai accesso completo a tutti gli strumenti (ordini custom, ordini di fabbrica "
        "btoweb, ricerca clienti, manuale) e a tutti i dati. Tono operativo e diretto."
    ),
    "b2b": (
        "MODALITÀ ATTIVA: B2B. Stai parlando con un cliente business (palestra, ASD, "
        "istruttore). Puoi cercare ordini custom per numero e consultare il manuale. "
        "NON hai accesso agli ordini di fabbrica (btoweb) e NON puoi elencare o rivelare "
        "ordini o dati di ALTRI clienti: se te lo chiedono, rifiuta cortesemente. "
        "Tono professionale e cortese. Per eccezioni, prezzi non a listino o situazioni "
        "delicate: 'verifico con Mauro e ti aggiorno al più presto'."
    ),
    "retail": (
        "MODALITÀ ATTIVA: RETAIL. Stai parlando con un cliente finale/privato. "
        "NON hai accesso a nessun dato interno o ordine: non cercare ordini, non citare "
        "numeri d'ordine, non rivelare dati di clienti. Rispondi SOLO con informazioni "
        "pubbliche dal manuale: taglie, tempi di consegna catalogo, spedizioni, resi, "
        "policy generali. Per qualsiasi richiesta su un ordine specifico, stato spedizione "
        "o dati personali, rimanda gentilmente a info@kanokimonos.com. "
        "Tono commerciale, cordiale e accogliente."
    ),
}

ROLE_TOOLS = {
    "staff": {
        "cerca_ordine_per_numero", "cerca_ordini_per_cliente", "rispondi_dal_manuale",
        "statistiche_ordini_custom", "prezzi_listino", "catalogo_btoweb",
        "ordini_per_produttore", "tracciamento_fully",
    },
    "b2b": {"cerca_ordine_per_numero", "rispondi_dal_manuale"},
    "retail": {"rispondi_dal_manuale"},
}

# Piattaforme ordini vietate per ruolo (enforcement lato esecuzione, difesa in profondità)
ROLE_BLOCKED_PLATFORMS = {
    "staff": set(),
    "b2b": {"btoweb"},
    "retail": {"custom", "woocommerce", "btoweb"},
}

DEFAULT_ROLE = "staff"


def _normalize_role(role: str) -> str:
    return role if role in ROLE_PROMPTS else DEFAULT_ROLE


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
        # Coerente con la scheda ordine: mostra lo stato di business (order_status),
        # non il campo workflow stale 'status'.
        os_code = o.get("order_status")
        os_label = CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A")
        lines.append(f"• {num} | {_first_product_name(o)} | {qty_str} | {os_label}{marker}")
    return "\n".join(lines)


def tool_cerca_ordine_per_numero(numero: str, piattaforma: str = None, blocked_platforms=None) -> str:
    """Opzione (a): restituisce la stringa già formattata dalle funzioni esistenti."""
    numero = (numero or "").strip()
    if not numero:
        return "Nessun numero d'ordine fornito."
    blocked = set(blocked_platforms or ())
    if piattaforma and piattaforma in blocked:
        return f"La ricerca ordini '{piattaforma}' non è disponibile in questa modalità."

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

    # Auto: deduci dal formato (stessa logica del vecchio routing regex),
    # saltando le piattaforme vietate per la modalità corrente.
    if "custom" not in blocked:
        custom = _fmt_custom(numero)
        if custom:
            return custom
    if "woocommerce" not in blocked and numero.isdigit():
        wc = _fmt_wc(search_orders_by_id(numero))
        if wc:
            return wc
    if "btoweb" not in blocked:
        bto = _fmt_bto(_bto_search_by_number(numero))
        if bto:
            return bto
    consentite = [p for p in ("custom", "WooCommerce", "btoweb") if p.lower() not in blocked]
    return f"Non ho trovato l'ordine {numero} su nessuna piattaforma ({', '.join(consentite)})."


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
        # Stato di business REALE = order_status (stesse etichette della scheda
        # ordine e delle statistiche). Il campo workflow 'status' NON viene esposto:
        # è stale (quasi sempre 'pending_confirmation') e faceva dire al bot
        # "in attesa di conferma" su ordini già prodotti e spediti.
        os_code = o.get("order_status")
        ordini.append({
            "order_number": o.get("order_number") or o.get("id"),
            "customer_name": o.get("customer_name"),
            "order_status": os_code,
            "stato_descrizione": CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A"),
            "partito_al_cliente": os_code in ("shipped_to_customer", "shipped"),
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
    return {
        "cliente": nome,
        "totale": len(ordini),
        "ordini": ordini,
        "nota_stato": (
            "Lo stato di avanzamento di ogni ordine è 'stato_descrizione' (ricavato da "
            "order_status). Non esiste nessun altro campo di stato: non dire mai che un "
            "ordine è 'in attesa di conferma' se stato_descrizione dice altro."
        ),
    }


def tool_rispondi_dal_manuale(argomento: str = None, user_message: str = "") -> str:
    query = argomento or user_message or ""
    # Domande sulle taglie: restituisci la GUIDA TAGLIE per intero (contigua),
    # perché il retrieval per-riga frammenta la tabella e la rende inaffidabile.
    if _is_size_query(f"{argomento or ''} {user_message or ''}"):
        guide = get_size_guide_block()
        if guide:
            extra = get_knowledge_context(query)
            return guide + (("\n\n" + extra) if extra else "")
    context = get_knowledge_context(query)
    if not context:
        return "NESSUN_CONTENUTO: il manuale non contiene informazioni su questo argomento."
    return context


# --- STATISTICHE AGGREGATE ORDINI CUSTOM (solo staff) ------------------------
# Mappatura order_status → etichetta di business (dalla macchina a stati kanokimonos.app).
# Nota: 'shipped' è uno stato STORICO/legacy; conta come "spedito" ma va dichiarato a parte.
CUSTOM_STATUS_LABELS = {
    "pending": "in lavorazione (grafica/taglie)",
    "processing": "in produzione",
    "shipped_to_logistics": "in viaggio verso la logistica (Fully)",
    "at_logistics": "da Fully, pronti a partire",
    "shipped_to_customer": "spediti al cliente",
    "shipped": "spediti (stato storico legacy)",
    "cancelled": "annullati",
}

_MESI_IT = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


def _custom_orders_dataset():
    """Scarica gli ordini custom GREZZI (limit 5000) per l'aggregazione.
    Usa i campi raw (order_status, payment_status, customers, ...), NON la normalizzazione
    (che rimappa 'status' sul workflow). Ritorna (righe, None) oppure (None, errore)."""
    data = get_custom_resource("orders", 5000)
    if isinstance(data, dict) and data.get("error"):
        return None, data
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("data") or data.get("orders") or []
    else:
        raw = []
    return [o for o in raw if isinstance(o, dict)], None


def _custom_customer_haystack(o: dict) -> str:
    c = o.get("customers") or {}
    if not isinstance(c, dict):
        c = {}
    parts = [c.get("first_name"), c.get("last_name"), c.get("business_name"), c.get("email")]
    return " ".join(str(p) for p in parts if p).lower()


def _normalize_mese(mese: str):
    """Normalizza il filtro mese in 'YYYY-MM' (o None). Accetta 'corrente'/'questo mese',
    'YYYY-MM', o un nome di mese italiano (anno corrente)."""
    if not mese:
        return None
    m = str(mese).strip().lower()
    now = datetime.now()
    if m in ("corrente", "questo mese", "mese corrente", "current", "this month"):
        return now.strftime("%Y-%m")
    if re.fullmatch(r"\d{4}-\d{2}", m):
        return m
    if m in _MESI_IT:
        return f"{now.year:04d}-{_MESI_IT[m]:02d}"
    return None


def tool_statistiche_ordini_custom(cliente: str = None, mese: str = None) -> dict:
    """Conteggi aggregati sugli ordini custom (kanokimonos.app), filtrabili per cliente
    e per mese. NON restituisce MAI importi in euro: solo conteggi. Se è indicato un
    cliente, allega l'elenco per-ordine per rispondere 'ha pagato?' / 'sono partiti?'."""
    rows, err = _custom_orders_dataset()
    if err:
        return {"error": err.get("error", "Errore API custom")}

    mese_norm = _normalize_mese(mese)
    cliente_clean = (cliente or "").strip().lower()

    def keep(o):
        if cliente_clean and cliente_clean not in _custom_customer_haystack(o):
            return False
        if mese_norm and not str(o.get("created_at") or "").startswith(mese_norm):
            return False
        return True

    sel = [o for o in rows if keep(o)]

    filtro = {}
    if cliente:
        filtro["cliente"] = cliente
    if mese_norm:
        filtro["mese"] = mese_norm

    os_counter = Counter(o.get("order_status") for o in sel)
    ps_counter = Counter(o.get("payment_status") for o in sel)

    per_order_status = {
        (k or "sconosciuto"): {
            "conteggio": v,
            "descrizione": CUSTOM_STATUS_LABELS.get(k, k or "sconosciuto"),
        }
        for k, v in os_counter.items()
    }

    result = {
        "totale_ordini": len(sel),
        "filtro": filtro,
        "per_order_status": per_order_status,
        "per_payment_status": {
            "fully_paid": ps_counter.get("fully_paid", 0),
            "advance_paid": ps_counter.get("advance_paid", 0),
            "unpaid": ps_counter.get("unpaid", 0),
        },
        "legenda_pagamenti": {
            "fully_paid": "pagati per intero",
            "advance_paid": "acconto versato",
            "unpaid": "non ancora pagati",
        },
        "nota_importi": (
            "I dati economici in euro (incassi, importi pagati, totali) NON sono disponibili "
            "qui e non vanno comunicati: si consultano solo su kanokimonos.app."
        ),
    }

    if "shipped" in os_counter:
        result["nota_legacy_shipped"] = (
            f"{os_counter.get('shipped_to_customer', 0)} ordini hanno stato 'spediti al cliente' "
            f"e {os_counter['shipped']} hanno lo stato storico legacy 'shipped'. Entrambi contano "
            "come spediti, ma vanno dichiarati separatamente."
        )

    if cliente:
        elenco = []
        for o in sorted(sel, key=lambda x: str(x.get("order_number") or "")):
            elenco.append({
                "order_number": o.get("order_number"),
                "order_status": o.get("order_status"),
                "stato_descrizione": CUSTOM_STATUS_LABELS.get(
                    o.get("order_status"), o.get("order_status")
                ),
                "payment_status": o.get("payment_status"),
                "partito_al_cliente": o.get("order_status") in ("shipped_to_customer", "shipped"),
                "created_at": o.get("created_at"),
            })
        result["ordini"] = elenco

    return result


def _pricing_rows(res: str):
    """Righe grezze di una risorsa pricing, oppure ('error', payload)."""
    data = get_custom_resource(res, 5000)
    if isinstance(data, dict) and data.get("error"):
        return None, {"error": data.get("error"), "details": data.get("details")}
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data") or data.get("orders") or []
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)], None


def _patch_size_hint(query: str):
    """Estrae la misura in cm da una query patch: '10x10'->10, '10 cm'->10, '10'->10.
    Le patch sono quadrate: la misura è il lato (size_cm)."""
    if not query:
        return None
    m = re.search(r"(\d+)\s*[x×]\s*\d+", query)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*cm", query.lower())
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", query)
    return int(nums[0]) if nums else None


def _product_name(r: dict) -> str:
    p = r.get("products") or {}
    return (p.get("name") if isinstance(p, dict) else "") or ""


def tool_prezzi_listino(query: str = None, tipo: str = None) -> dict:
    """Prezzi di listino ufficiali (solo staff) dalle risorse patch_pricing / product_pricing.
    - patch_pricing: prezzate per size_cm (patch quadrate) + fascia di quantità; il tipo di
      stampa (es. DTF) NON cambia il prezzo di listino. Estrae la misura dalla query.
    - product_pricing: filtra per NOME prodotto (annidato in products.name); restituisce
      prezzo standard e prezzo VIP per fascia di quantità.
    Restituisce la scala prezzi/quantità così Haiku riporta la fascia giusta."""
    if tipo == "patch":
        resources = ["patch_pricing"]
    elif tipo in ("prodotti", "prodotto", "product", "products"):
        resources = ["product_pricing"]
    else:
        resources = ["patch_pricing", "product_pricing"]

    q = (query or "").strip().lower()
    out = {}

    for res in resources:
        rows, err = _pricing_rows(res)
        if err:
            out[res] = err
            continue

        if res == "patch_pricing":
            size = _patch_size_hint(query)
            sel = [r for r in rows if size is None or r.get("size_cm") == size]
            sel = sorted(sel, key=lambda r: (r.get("size_cm") or 0, r.get("min_quantity") or 0))
            out[res] = {
                "filtro_misura_cm": size,
                "nota": (
                    "Patch quadrate: la misura è il lato in cm (size_cm). Il prezzo unitario "
                    "dipende dalla fascia di quantità. Il tipo di stampa (es. DTF) non cambia "
                    "il prezzo di listino."
                ),
                "totale": len(sel),
                "listino": [
                    {
                        "size_cm": r.get("size_cm"),
                        "quantita_da": r.get("min_quantity"),
                        "quantita_a": r.get("max_quantity"),
                        "prezzo_unitario": r.get("price"),
                        "customer_type": r.get("customer_type"),
                    }
                    for r in sel[:60]
                ],
            }
        else:  # product_pricing
            tokens = [t for t in q.split() if t and not t.isdigit()]
            # Il match è in AND su tutti i token, ma il listino usa nomi generici
            # ("T-SHIRT") mentre le domande aggiungono il brand ("t-shirt kano"):
            # l'AND pieno restituiva 0 e il bot dichiarava il prezzo inesistente.
            # Si scartano progressivamente i token finali finché si trova qualcosa.
            sel, usati, ignorati = rows, [], []
            if tokens:
                for taglio in range(len(tokens)):
                    usati = tokens[: len(tokens) - taglio]
                    candidati = [
                        r for r in rows if all(t in _product_name(r).lower() for t in usati)
                    ]
                    if candidati:
                        sel = candidati
                        ignorati = tokens[len(tokens) - taglio :]
                        break
                else:
                    # nemmeno il primo token da solo trova nulla
                    sel, usati, ignorati = [], [], tokens
            sel = sorted(sel, key=lambda r: (_product_name(r), r.get("min_quantity") or 0))
            out[res] = {
                "nota": "Prezzo per fascia di quantità. 'prezzo_vip' è il prezzo riservato ai clienti VIP.",
                "parole_cercate": usati or None,
                "parole_ignorate": ignorati or None,
                "nota_ricerca": (
                    "Nessun prodotto a listino conteneva tutte le parole cercate: la ricerca è "
                    "stata allargata ignorando %s. Verifica che il prodotto elencato sia quello "
                    "giusto prima di comunicare il prezzo." % ", ".join("'%s'" % t for t in ignorati)
                ) if ignorati and usati else None,
                "totale": len(sel),
                "listino": [
                    {
                        "prodotto": _product_name(r),
                        "quantita_da": r.get("min_quantity"),
                        "quantita_a": r.get("max_quantity"),
                        "prezzo": r.get("price"),
                        "prezzo_vip": r.get("vip_price"),
                        "size_variation": r.get("size_variation"),
                        "customer_type": r.get("customer_type"),
                    }
                    for r in sel[:60]
                ],
            }

    return out


_SIZE_SUFFIX_RE = re.compile(r"\s*-\s*[A-Za-z0-9]{1,4}\s*$")


def _bto_base_name(name: str) -> str:
    """'BJJ RASHGUARD COMPETITION ... - XL' -> 'BJJ RASHGUARD COMPETITION ...'.
    Ogni taglia è una riga separata: il nome base serve per raggruppare le varianti."""
    return _SIZE_SUFFIX_RE.sub("", (name or "").strip()).strip()


def _bto_match(name: str, tokens: list) -> bool:
    """AND su tutti i token della query nel nome prodotto (come product_pricing)."""
    n = (name or "").lower()
    return all(t in n for t in tokens)


# La risorsa 'stock' ignora il parametro q lato server (verificato): va scaricata
# intera e filtrata qui. 'products' invece filtra davvero su q e sku.
# La edge function tronca comunque a 500 righe per chiamata: senza paginazione su
# offset si perdono righe (es. 'T-SHIRT KANO PLAIN BLACK' sta oltre la 500ª riga).
_BTO_PAGE_SIZE = 500
_BTO_MAX_ROWS = 3000


def _bto_get_paged(params: dict):
    """Scarica tutte le righe di una risorsa btoweb paginando su offset.
    Restituisce (righe, meta) oppure (None, errore)."""
    rows = []
    meta = {}
    offset = 0
    while offset < _BTO_MAX_ROWS:
        page = dict(params)
        page["limit"] = _BTO_PAGE_SIZE
        page["offset"] = offset
        res = _bto_get(page)
        if res.get("error"):
            return None, res
        if not meta:
            meta = {k: v for k, v in res.items() if k != "data"}
        batch = [r for r in (res.get("data") or []) if isinstance(r, dict)]
        rows.extend(batch)
        if len(batch) < _BTO_PAGE_SIZE:
            break
        total = res.get("total")
        if isinstance(total, int) and len(rows) >= total:
            break
        offset += _BTO_PAGE_SIZE
    return rows, meta


def _bto_rank_key(base: str, q: str):
    """Ordina i gruppi per pertinenza: prima chi contiene la frase esatta cercata,
    poi il nome più corto (il prodotto 'puro' prima delle sue varianti estese).
    Senza questo, cercando 'BJJ Belt Competition' i rashguard sommergono la cintura."""
    b = (base or "").lower()
    phrase = 0 if (q and q.strip().lower() in b) else 1
    return (phrase, len(b), b)

_BTO_NOTA_PREZZI = (
    "Questa fonte NON contiene prezzi: non dedurre né stimare prezzi da qui. "
    "Per i prezzi usa lo strumento prezzi_listino."
)


def tool_catalogo_btoweb(query: str = None, sku: str = None, tipo: str = None) -> dict:
    """Catalogo btoweb (solo staff): anagrafica prodotti (SKU/EAN, nome, taglia, colore)
    e contatori della PIPELINE DI PRODUZIONE. Nessun prezzo in questa fonte.
    - sku: lookup esatto sull'anagrafica (filtro server-side).
    - tipo='prodotti': anagrafica, raggruppata per prodotto con l'elenco taglie.
    - tipo='produzione': risorsa stock = contatori di produzione, NON giacenza vendibile."""
    sku_clean = (sku or "").strip()
    q = (query or "").strip()
    tokens = [t for t in q.lower().split() if t]

    if sku_clean:
        res = _bto_get({"resource": "products", "sku": sku_clean})
        if res.get("error"):
            return res
        rows = [r for r in (res.get("data") or []) if isinstance(r, dict)]
        if not rows:
            return {
                "tipo": "anagrafica_prodotti",
                "sku_cercato": sku_clean,
                "trovato": False,
                "totale": 0,
                "risultati": [],
                "nota": f"Nessun prodotto in anagrafica btoweb con SKU/EAN {sku_clean}.",
                "nota_prezzi": _BTO_NOTA_PREZZI,
            }
        return {
            "tipo": "anagrafica_prodotti",
            "sku_cercato": sku_clean,
            "trovato": True,
            "totale": len(rows),
            "risultati": [
                {
                    "sku": r.get("sku"),
                    "ean": r.get("ean"),
                    "prodotto": r.get("product_name"),
                    "taglia": r.get("size"),
                    "colore": r.get("colour"),
                }
                for r in rows[:20]
            ],
            "nota_prezzi": _BTO_NOTA_PREZZI,
        }

    if tipo in ("produzione", "stock", "production"):
        rows, res = _bto_get_paged({"resource": "stock"})
        if rows is None:
            return res
        sel = [r for r in rows if _bto_match(r.get("product_name"), tokens)] if tokens else rows

        gruppi = {}
        ordine = []
        for r in sel:
            base = _bto_base_name(r.get("product_name")) or "(senza nome)"
            if base not in gruppi:
                gruppi[base] = {
                    "prodotto": base,
                    "in_produzione": 0,
                    "prodotti_e_spediti_dal_fornitore": 0,
                    "ricevuti_conformi": 0,
                    "mancanti": 0,
                    "danneggiati": 0,
                    "attesi_su_fully": 0,
                    "dettaglio_taglie": [],
                }
                ordine.append(base)
            g = gruppi[base]
            g["in_produzione"] += r.get("qty_in_production") or 0
            g["prodotti_e_spediti_dal_fornitore"] += r.get("qty_shipped") or 0
            g["ricevuti_conformi"] += r.get("qty_received_good") or 0
            g["mancanti"] += r.get("qty_missing") or 0
            g["danneggiati"] += r.get("qty_hurt") or 0
            g["attesi_su_fully"] += r.get("qty_expected_on_fully") or 0
            g["dettaglio_taglie"].append(
                {
                    "taglia": r.get("size"),
                    "in_produzione": r.get("qty_in_production") or 0,
                    "prodotti_e_spediti_dal_fornitore": r.get("qty_shipped") or 0,
                    "ricevuti_conformi": r.get("qty_received_good") or 0,
                    "mancanti": r.get("qty_missing") or 0,
                    "danneggiati": r.get("qty_hurt") or 0,
                }
            )

        ordine.sort(key=lambda b: _bto_rank_key(b, q))
        out_gruppi = [gruppi[k] for k in ordine[:25]]
        for g in out_gruppi:
            g["dettaglio_taglie"] = g["dettaglio_taglie"][:30]

        return {
            "tipo": "produzione_pipeline",
            "query": q or None,
            "righe_totali_fonte": res.get("total"),
            "righe_scaricate": len(rows),
            "righe_corrispondenti": len(sel),
            "prodotti_trovati": len(ordine),
            "gruppi_mostrati": len(out_gruppi),
            "prodotti": out_gruppi,
            "disclaimer": res.get("disclaimer"),
            "avvertenza": (
                "ATTENZIONE: questi sono contatori della PIPELINE DI PRODUZIONE, NON la "
                "giacenza vendibile a magazzino. 'in_produzione' = ordinato al fornitore e non "
                "ancora spedito; 'prodotti_e_spediti_dal_fornitore' = prodotto su spedizioni "
                "partite; 'ricevuti_conformi'/'mancanti'/'danneggiati' = riconciliazione in "
                "ingresso su Fully. Non dire mai che un capo è 'disponibile' o 'in stock' "
                "sulla base di questi numeri: dichiara sempre che si tratta di dati di produzione."
            ),
            "nota_prezzi": _BTO_NOTA_PREZZI,
        }

    # default: anagrafica prodotti per parole chiave
    params = {"resource": "products"}
    if q:
        params["q"] = q
    rows, res = _bto_get_paged(params)
    if rows is None:
        return res
    sel = [r for r in rows if _bto_match(r.get("product_name"), tokens)] if tokens else rows

    gruppi = {}
    ordine = []
    for r in sel:
        base = _bto_base_name(r.get("product_name")) or "(senza nome)"
        if base not in gruppi:
            gruppi[base] = {"prodotto": base, "taglie": [], "colori": [], "varianti": []}
            ordine.append(base)
        g = gruppi[base]
        taglia = r.get("size")
        colore = r.get("colour")
        if taglia and taglia not in g["taglie"]:
            g["taglie"].append(taglia)
        if colore and colore not in g["colori"]:
            g["colori"].append(colore)
        g["varianti"].append(
            {"sku": r.get("sku"), "taglia": taglia, "colore": colore}
        )

    ordine.sort(key=lambda b: _bto_rank_key(b, q))
    out_gruppi = [gruppi[k] for k in ordine[:25]]
    for g in out_gruppi:
        g["numero_varianti"] = len(g["varianti"])
        # Alcuni prodotti hanno size/colour a null nel CSV sorgente (es. le varianti
        # UltraLight Kumo): distinguere "nessuna taglia" da "taglia non registrata".
        if not g["taglie"] and g["varianti"]:
            g["taglie_non_valorizzate"] = True
            g["nota_taglie"] = (
                "Questo prodotto ha %d SKU a catalogo ma la taglia NON è valorizzata "
                "in anagrafica btoweb: non concludere che non esistano taglie, "
                "il dato è mancante alla fonte." % len(g["varianti"])
            )
        g["varianti"] = g["varianti"][:40]

    return {
        "tipo": "anagrafica_prodotti",
        "query": q or None,
        "righe_corrispondenti": len(sel),
        "prodotti_trovati": len(ordine),
        "gruppi_mostrati": len(out_gruppi),
        "prodotti": out_gruppi,
        "fonte": (res.get("source") or {}).get("file_name"),
        "nota": (
            "Anagrafica prodotti/EAN. Ogni taglia è uno SKU distinto: 'taglie' elenca le "
            "taglie effettivamente presenti a catalogo per quel prodotto. La presenza a "
            "catalogo NON implica disponibilità a magazzino."
        ),
        "nota_prezzi": _BTO_NOTA_PREZZI,
    }


# --- ORDINI DI FABBRICA PER PRODUTTORE ---------------------------------------
# I nomi dei produttori stanno SOLO nella descrizione del tool (CHAT_TOOLS) e in
# questa tabella di alias: MAI in SYSTEM_PROMPT, che è iniettato anche per b2b e
# retail e non deve esporre i fornitori.
# La tabella serve solo a riconciliare chiavi diverse fra le fonti (btoweb salva
# 'tusslesports@gmail.com' dove kanokimonos ha 'Tussle Production') e NON limita
# la ricerca: un produttore non elencato qui viene comunque trovato dal confronto
# diretto sui valori realmente presenti negli ordini.
_BTO_PRODUCER_ALIASES = {
    "martin": ["martin", "tc-garment", "martin@tc-garment.com"],
    "7punch": ["7punch", "7 punch", "seventh punch", "seventhpunch", "seventhpunch@gmail.com"],
    "wearica": ["wearica", "wearica.clothing", "wearica.clothing@gmail.com"],
    "tussle": ["tussle", "tussle production", "tusslesports", "tusslesports@gmail.com"],
    "fair tex": ["fair tex", "fairtex"],
}

_BTO_ORDER_STATUS_LABELS = {
    "nuovo": "ordine creato, non ancora avviato in produzione",
    "in_produzione": "in produzione dal fornitore",
    "spedito": "già spedito dal produttore (in viaggio verso di noi)",
}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


# Soglia di somiglianza per i refusi ('wearika' -> 'Wearica'). Sotto questo valore
# si preferisce dire che non si è trovato nulla piuttosto che tirare a indovinare.
_BTO_SIMIL_MIN = 0.75


def _bto_norm_producer(value: str) -> str:
    """Minuscolo, senza accenti, senza spazi doppi né ai bordi: nel dato reale il
    valore è 'FAIR TEX ' con lo spazio finale."""
    v = unicodedata.normalize("NFKD", value or "")
    v = "".join(c for c in v if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", v.strip().lower())


def _bto_squash(value: str) -> str:
    """Solo lettere e cifre: 'FAIR TEX ' e 'Fair-Tex' diventano entrambi 'fairtex'."""
    return re.sub(r"[^a-z0-9]+", "", _bto_norm_producer(value))


def _bto_producer_canon(value: str) -> str:
    """Riconduce un valore (nome o email) alla chiave canonica del produttore.
    Se non è in tabella restituisce il valore normalizzato, così i produttori nuovi
    continuano a funzionare."""
    v = _bto_norm_producer(value)
    if not v:
        return ""
    for canon, aliases in _BTO_PRODUCER_ALIASES.items():
        for a in aliases:
            # Confine di parola, non sottostringa nuda: 'martin' deve riconoscere
            # 'martin@tc-garment.com' ma NON 'martini', che è un altro nome (o un
            # refuso) e va trattato come tale, non spacciato per match esatto.
            if v == a or re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(a), v):
                return canon
    return v


def _bto_producer_forms(value: str) -> list:
    """Tutte le stringhe con cui confrontare un produttore: il suo valore reale più
    gli alias noti (nome <-> email). Serve perché 'seventh punsh' possa somigliare a
    '7punch', che come stringa non gli assomiglia per niente."""
    forme = [_bto_norm_producer(value)]
    for a in _BTO_PRODUCER_ALIASES.get(_bto_producer_canon(value), []):
        if a not in forme:
            forme.append(a)
    return [f for f in forme if f]


def _bto_similarity(query: str, value: str) -> float:
    """Somiglianza sui refusi: il massimo fra tutte le forme note del produttore,
    confrontate sia normalizzate sia senza punteggiatura."""
    q, qs = _bto_norm_producer(query), _bto_squash(query)
    best = 0.0
    for f in _bto_producer_forms(value):
        best = max(
            best,
            difflib.SequenceMatcher(None, q, f).ratio(),
            difflib.SequenceMatcher(None, qs, _bto_squash(f)).ratio(),
        )
    return round(best, 3)


def _bto_resolve_producer(query: str, presenti: list) -> dict:
    """Risolve il nome digitato dall'utente su uno dei produttori realmente presenti
    negli ordini, in tre livelli decrescenti:
      esatto      = uguale, o uguale a meno di punteggiatura/accenti, o alias noto
                    ('Tussle' -> 'tusslesports@gmail.com')
      parziale    = uno contiene l'altro ('fair' -> 'FAIR TEX ')
      somiglianza = refusi sopra la soglia ('wearika' -> 'Wearica')
    Vince il livello più alto che produce almeno un candidato: la somiglianza non
    entra mai in gioco se esiste già un match pieno. Nessuna lista chiusa: il
    confronto è sui valori realmente presenti negli ordini, quindi un produttore
    nuovo viene trovato lo stesso."""
    q, qs = _bto_norm_producer(query), _bto_squash(query)
    if not q:
        return {"livello": None, "candidati": []}

    esatti, parziali, simili = [], [], []
    for p in presenti:
        forme = _bto_producer_forms(p)
        ps = _bto_squash(p)
        if q in forme or (qs and qs == ps) or _bto_producer_canon(q) == _bto_producer_canon(p):
            esatti.append({"produttore": p, "match": "esatto", "punteggio": 1.0})
            continue
        # Sottostringa solo da 3 caratteri in su: con 1-2 lettere matcherebbe chiunque.
        if len(q) >= 3 and (
            any(q in f or f in q for f in forme) or (len(qs) >= 3 and (qs in ps or ps in qs))
        ):
            parziali.append(
                {"produttore": p, "match": "parziale", "punteggio": _bto_similarity(q, p)}
            )
            continue
        s = _bto_similarity(q, p)
        if s >= _BTO_SIMIL_MIN:
            simili.append({"produttore": p, "match": "somiglianza", "punteggio": s})

    for livello, gruppo in (("esatto", esatti), ("parziale", parziali), ("somiglianza", simili)):
        if gruppo:
            gruppo.sort(key=lambda c: -c["punteggio"])
            return {"livello": livello, "candidati": gruppo}
    return {"livello": None, "candidati": []}


_BTO_MAX_PRODOTTI_PER_ORDINE = 40


def tool_ordini_per_produttore(produttore: str = None) -> dict:
    """Ordini di fabbrica btoweb di un produttore (solo staff), orientati a
    'cosa deve arrivare da X': stato, data di arrivo prevista, prodotti e quantità.
    Il parametro 'producer' della edge function è IGNORATO lato server (verificato:
    qualsiasi valore restituisce comunque tutte le righe), quindi si scarica la
    risorsa 'orders' con _bto_get_paged e si filtra qui."""
    q = (produttore or "").strip()
    if not q:
        return {"error": "Serve il nome del produttore da cercare."}

    rows, meta = _bto_get_paged({"resource": "orders"})
    if rows is None:
        return meta

    presenti = []
    for r in rows:
        p = (r.get("producer") or "").strip()
        if p and p not in presenti:
            presenti.append(p)

    ris = _bto_resolve_producer(q, presenti)
    candidati = ris["candidati"]

    if not candidati:
        return {
            "tipo": "ordini_per_produttore",
            "produttore_cercato": q,
            "trovato": False,
            "richiesta_chiarimento": False,
            "ordini_totali": 0,
            "ordini": [],
            "produttori_presenti_negli_ordini": presenti,
            "nota": (
                f"'{q}' NON è un produttore: nessuno dei produttori reali gli corrisponde "
                "né gli somiglia. NON inventare ordini e NON attribuirgliene di altri "
                "produttori. Ma NON fermarti qui: i produttori sono pochi e noti, quindi un "
                f"nome che non è tra loro è quasi sempre un CLIENTE. Se '{q}' può essere un "
                "cliente, una palestra, una ASD o un'azienda, RIPROVA SUBITO con "
                "cerca_ordini_per_cliente prima di rispondere. Solo se anche quella ricerca "
                "non trova nulla puoi dire che non risulta, elencando i produttori "
                "effettivamente presenti."
            ),
        }

    # Più valori grezzi possono essere lo STESSO produttore (nome + email): in quel
    # caso non c'è ambiguità, si filtra su tutti. L'ambiguità vera è avere candidati
    # con chiavi canoniche diverse: lì si chiede, non si sceglie.
    canoni = []
    for c in candidati:
        k = _bto_producer_canon(c["produttore"])
        if k not in canoni:
            canoni.append(k)

    if len(canoni) > 1:
        return {
            "tipo": "ordini_per_produttore",
            "produttore_cercato": q,
            "trovato": False,
            "richiesta_chiarimento": True,
            "tipo_match": ris["livello"],
            "candidati": candidati,
            "ordini": [],
            "nota": (
                f"Più produttori compatibili con '{q}' (match per {ris['livello']}). NON "
                "sceglierne uno tu e NON mostrare ordini: elenca i candidati all'utente e "
                "chiedi quale intende, poi richiama lo strumento con il nome scelto."
            ),
        }

    corrispondenti = [c["produttore"] for c in candidati]
    sel = [r for r in rows if (r.get("producer") or "").strip() in corrispondenti]

    gruppi = {}
    ordine_keys = []
    viste = set()
    for r in sel:
        # La fonte ripete la stessa identica riga N volte per ordine (es. 072026-0004:
        # 35 righe uguali): senza dedup le quantità verrebbero moltiplicate.
        firma = json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)
        if firma in viste:
            continue
        viste.add(firma)

        num = str(r.get("order_number") or "").strip() or "(senza numero)"
        g = gruppi.get(num)
        if g is None:
            g = gruppi[num] = {
                "produttore": (r.get("producer") or "").strip(),
                "stato": r.get("status"),
                "data": r.get("expected_arrival_date"),
                "qty": {},
                "keys": [],
            }
            ordine_keys.append(num)
        for p in (r.get("products") or []):
            if not isinstance(p, dict):
                continue
            nome = (p.get("name") or "").strip() or "(senza nome)"
            taglia = (p.get("category") or "").strip() or None
            k = (nome, taglia)
            if k not in g["qty"]:
                g["qty"][k] = 0
                g["keys"].append(k)
            try:
                g["qty"][k] += int(p.get("quantity") or 0)
            except (TypeError, ValueError):
                pass

    oggi = datetime.now().strftime("%Y-%m-%d")
    ordini = []
    for num in ordine_keys:
        g = gruppi[num]
        prodotti = [
            {"prodotto": n, "taglia": t, "quantita": g["qty"][(n, t)]} for (n, t) in g["keys"]
        ]
        data = g["data"] or None
        o = {
            "numero_ordine": num,
            "produttore": g["produttore"],
            "stato": g["stato"],
            "stato_descrizione": _BTO_ORDER_STATUS_LABELS.get(g["stato"], g["stato"]),
            "data_arrivo_prevista": data,
            "pezzi_totali": sum(x["quantita"] for x in prodotti),
            "prodotti": prodotti[:_BTO_MAX_PRODOTTI_PER_ORDINE],
        }
        if len(prodotti) > _BTO_MAX_PRODOTTI_PER_ORDINE:
            o["prodotti_non_mostrati"] = len(prodotti) - _BTO_MAX_PRODOTTI_PER_ORDINE
        if not data:
            o["nota_data"] = "Data di arrivo prevista NON valorizzata alla fonte btoweb."
        elif data < oggi:
            o["data_gia_passata"] = True
        if not prodotti:
            o["nota_prodotti"] = (
                "Dettaglio prodotti/quantità NON valorizzato alla fonte per questo ordine: "
                "dillo, non dedurre cosa contiene."
            )
        if _UUID_RE.match(num):
            o["nota_numero"] = (
                "Numero d'ordine non valorizzato alla fonte: questo è l'id tecnico interno."
            )
        ordini.append(o)

    # I più imminenti in cima; gli ordini senza data prevista vanno in fondo, e lì
    # (nessun criterio di imminenza disponibile) prima quelli che devono ancora
    # arrivare, per ultimi quelli già partiti dal produttore.
    _peso_stato = {"nuovo": 0, "in_produzione": 1, "spedito": 2}
    ordini.sort(
        key=lambda o: (0, o["data_arrivo_prevista"], 0)
        if o["data_arrivo_prevista"]
        else (1, "", _peso_stato.get(o["stato"], 1))
    )

    riepilogo = {}
    for o in ordini:
        k = o["stato"] or "(senza stato)"
        riepilogo[k] = riepilogo.get(k, 0) + 1

    out = {
        "tipo": "ordini_per_produttore",
        "produttore_cercato": q,
        "trovato": True,
        "richiesta_chiarimento": False,
        "produttori_corrispondenti": corrispondenti,
        "tipo_match": ris["livello"],
        "punteggio_match": candidati[0]["punteggio"],
        "ordini_totali": len(ordini),
        "riepilogo_stati": riepilogo,
        "pezzi_totali_elencati": sum(o["pezzi_totali"] for o in ordini),
        "data_odierna": oggi,
        "ordini": ordini,
        "nota": (
            "Ordini di FABBRICA (merce che deve arrivare dal produttore), non ordini "
            "cliente. Sono ordinati per data di arrivo prevista crescente: i più "
            "imminenti per primi, quelli senza data in fondo. 'spedito' = già partito "
            "dal produttore; 'in_produzione' = ancora in lavorazione; 'nuovo' = non "
            "ancora avviato. Riporta solo le date e le quantità presenti qui: dove "
            "manca il dato dichiaralo, non stimarlo."
        ),
        "nota_prezzi": _BTO_NOTA_PREZZI,
    }

    # Se il nome usato non è quello digitato (alias o refuso), va dichiarato:
    # l'utente deve sapere su chi ha risposto lo strumento.
    if _bto_squash(q) != _bto_squash(corrispondenti[0]):
        out["nota_interpretazione"] = (
            f"L'utente ha scritto '{q}' e lo strumento l'ha risolto sul produttore "
            f"'{corrispondenti[0]}' (match per {ris['livello']}). DICHIARALO nella risposta "
            "prima dei dati, es. \"interpreto '%s' come %s\"." % (q, corrispondenti[0])
        )
    return out


# --- TRACCIAMENTO FULLY (catena A-Z di un ordine custom) ----------------------

_FULLY_LIMIT = 5000
_FULLY_SYNC_STALE_ORE = 24
_FULLY_MAX_ORDINI_CLIENTE = 6

# Regola fissa, ripetuta nel payload perché il modello non la ammorbidisca:
# fully_verified_on lo scrive Bambu a mano, non arriva da Fully.
_FULLY_NOTA_VERIFICA = (
    "verifica manuale fatta da Bambu, MAI una conferma di Fully"
)

# --- ARRIVO MERCE: la fonte di verità è la vista fully_replenishments ---------
# arrival_status / arrival_source / arrived_at sono l'UNICO indicatore di arrivo.
# shipments.status e shipments.received_at NON lo sono: la fonte stessa lo
# dichiara (received_at_note = "Not used for Fully shipments — ignore as arrival
# indicator"). Il received_at storico resta solo come dato etichettato.
_FULLY_ARRIVO_PAROLE = {
    "arrived": (
        "ARRIVATA E CONTATA: Fully ha ricevuto la merce E ha concluso il conteggio."
    ),
    # arrival_status=arrived ma la prova è una spunta a mano, non un conteggio:
    # 27 carichi su 37 'arrived' sono di questo tipo, dirlo come sopra sarebbe falso.
    "arrived_legacy": (
        "RISULTA ARRIVATA in base a una spunta manuale storica di Bambu, NON a un "
        "conteggio di Fully: per questo carico il conteggio riga per riga non esiste."
    ),
    "counting_in_progress": (
        "ARRIVATA IN MAGAZZINO MA CONTEGGIO NON ANCORA CONCLUSO DA FULLY: la merce è "
        "al magazzino, il conteggio è ancora APERTO e i numeri sono PROVVISORI (possono "
        "cambiare). È il caso in cui si può SOLLECITARE FULLY perché chiuda il "
        "conteggio: dillo esplicitamente."
    ),
    "no_arrival_evidence": (
        "NESSUNA PROVA DI ARRIVO: non risulta né un conteggio di Fully né una spunta "
        "manuale. NON dedurre che sia arrivata, e nemmeno che sia persa: il dato non c'è."
    ),
}

_FULLY_ARRIVO_FONTE = {
    "fully_count": "conteggio automatico di Fully, concluso",
    "fully_count_partial": "conteggio automatico di Fully ANCORA IN CORSO (parziale)",
    "legacy_manual_received_at": (
        "verifica manuale di un admin Bambu (spunta storica), MAI una conferma di Fully"
    ),
    "none": "nessuna fonte: non esiste alcuna prova di arrivo",
    "None": "nessuna fonte: non esiste alcuna prova di arrivo",
}


def _fully_rows(resource: str, extra_params: dict = None):
    """Scarica una risorsa della edge function e restituisce (righe, None) o
    (None, errore). L'involucro è {resource, count, limit, offset, data}."""
    data = get_custom_resource(resource, _FULLY_LIMIT, extra_params=extra_params)
    if isinstance(data, dict) and data.get("error"):
        return None, data
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)], None
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [r for r in data["data"] if isinstance(r, dict)], None
    return None, {"error": f"Struttura inattesa dalla risorsa {resource}."}


def _fully_norm_num(value) -> str:
    return str(value or "").strip().lower()


def _fully_eta_fotografia(sync_iso: str) -> dict:
    """Il conteggio Fully è una fotografia (fully_synced_at), non una lettura in
    diretta: va sempre dichiarato, e se è vecchia va detto quanto."""
    out = {
        "fotografia_del": sync_iso,
        "nota_fotografia": (
            "Il conteggio Fully è una FOTOGRAFIA sincronizzata a questa data/ora, "
            "non una lettura in diretta."
        ),
    }
    try:
        sync = datetime.fromisoformat(str(sync_iso).replace("Z", "+00:00"))
        ore = (datetime.now(timezone.utc) - sync).total_seconds() / 3600
        if ore > _FULLY_SYNC_STALE_ORE:
            giorni = ore / 24
            out["nota_fotografia"] += (
                " ATTENZIONE: la fotografia è vecchia di %s: il dato reale su Fully "
                "potrebbe essere cambiato, dichiaralo."
                % (f"{giorni:.0f} giorni" if giorni >= 2 else f"{ore:.0f} ore")
            )
    except (ValueError, TypeError):
        pass
    return out


def _fully_riga_conteggio(r: dict) -> dict:
    """Una riga di fully_reconciliation resa leggibile, con le regole di business:
    si spedisce quanto Fully ha contato (buoni), ma si fattura la quantità ORDINATA.
    has_discrepancy alla fonte copre solo mancanti/danneggiati: gli eccessi vanno
    intercettati qui su qty_surplus, altrimenti non emergerebbero mai."""
    dem = r.get("qty_demanded") or 0
    good = r.get("qty_good") or 0
    surplus = r.get("qty_surplus") or 0
    missing = r.get("qty_missing") or 0
    hurt = r.get("qty_hurt") or 0

    riga = {
        "prodotto": r.get("product_name"),
        "taglia": r.get("size_variation"),
        "ean": r.get("ean_code"),
        "richiesti_dal_cliente": dem,
        "attesi_sul_carico": r.get("qty_expected"),
        "contati_buoni_da_fully": good,
        "mancanti": missing,
        "danneggiati": hurt,
        "in_piu": surplus,
        "stato_riga": r.get("replenishment_state"),
    }

    avvisi = []
    if surplus > 0:
        avvisi.append(
            f"PEZZI IN PIÙ: {surplus} da consegnare E DA FATTURARE (si spedisce "
            "quanto contato da Fully, ma il prezzo è sulla quantità ordinata: "
            "l'eccesso va fatturato a parte). Va segnalato SEMPRE, anche se la "
            "fonte non lo marca come discrepanza."
        )
    if missing > 0:
        avvisi.append(
            f"MANCANTI: {missing}. Il cliente riceve meno di quanto ha pagato."
        )
    if hurt > 0:
        avvisi.append(
            f"DANNEGGIATI: {hurt}. Il cliente riceve meno di quanto ha pagato."
        )
    if good == 0 and dem > 0:
        avvisi.append(
            "Questa riga NON partirà affatto: 0 pezzi buoni contati da Fully."
        )
    if avvisi:
        riga["avvisi"] = avvisi
        # 'gestita' distingue le anomalie già sistemate da quelle ancora aperte.
        if r.get("discrepancy_handled"):
            riga["anomalia"] = "GIÀ GESTITA"
            if r.get("discrepancy_handled_at"):
                riga["anomalia_gestita_il"] = r.get("discrepancy_handled_at")
            if r.get("discrepancy_note"):
                riga["nota_gestione"] = r.get("discrepancy_note")
        else:
            riga["anomalia"] = "DA GESTIRE"
    return riga


def _fully_blocco_arrivo(rep: dict, s: dict = None) -> dict:
    """Lo stato di arrivo della merce, letto SOLO da fully_replenishments.
    Restituisce sempre una frase esplicita: arrivata e contata / arrivata ma
    conteggio non concluso / nessuna prova di arrivo."""
    s = s or {}
    dest = s.get("destination") or (rep or {}).get("destination")

    # Le spedizioni dirette al cliente non passano da Fully: la vista arrivi non
    # le contiene affatto (50/50 sono destination='fully'). E nemmeno la
    # spedizione stessa dice quando il cliente ha ricevuto: received_at è la
    # spunta del vecchio modulo logistico interno (confermato dal punto app),
    # NON una consegna al cliente finale. Quindi la data di consegna NON esiste.
    if dest and dest != "fully":
        return {
            "stato_arrivo": "non_applicabile",
            "in_parole": (
                f"Spedizione con destinazione '{dest}': NON passa dalla logistica "
                "Fully, quindi non esistono né conteggio Fully né stato di arrivo Fully."
            ),
            "consegna_al_cliente": (
                "NON esiste a sistema una data di consegna al cliente per le "
                "spedizioni dirette: l'unico modo di saperlo è il tracking del "
                "corriere. Eventuali date di ricezione presenti sulla spedizione "
                "sono spunte del vecchio modulo logistico interno e NON vanno MAI "
                "interpretate come consegna al cliente: non citarle come tali. "
                "Vale anche per uno 'stato_asn' che dice 'received': è la stessa "
                "spunta interna, non una conferma di consegna al cliente."
            ),
            "stato_asn": s.get("status"),
        }

    if not rep:
        return {
            "stato_arrivo": "non_presente_nella_vista_arrivi",
            "in_parole": (
                "Questa spedizione non compare nell'elenco arrivi di Fully "
                "(fully_replenishments): non esiste alcuna prova di arrivo. "
                "Dichiaralo, non dedurre."
            ),
        }

    st = str(rep.get("arrival_status"))
    src = str(rep.get("arrival_source"))
    chiave = "arrived_legacy" if (
        st == "arrived" and src == "legacy_manual_received_at"
    ) else st
    b = {
        "stato_arrivo": st,
        "in_parole": _FULLY_ARRIVO_PAROLE.get(chiave) or (
            f"Stato di arrivo '{st}' non previsto dallo strumento: riportalo così "
            "com'è, senza interpretarlo."
        ),
        "come_lo_sappiamo": _FULLY_ARRIVO_FONTE.get(src, f"fonte non prevista: '{src}'"),
    }
    if rep.get("arrived_at"):
        b["arrivata_il"] = rep.get("arrived_at")

    # Avanzamento del conteggio: ha senso solo se il carico ha righe da contare.
    if (rep.get("fully_count_rows") or 0) > 0:
        b["avanzamento_conteggio_fully"] = (
            f"{rep.get('fully_count_rows_done') or 0} righe contate da Fully su "
            f"{rep.get('fully_count_rows')} righe del carico"
        )
        b["conteggio_concluso"] = bool(rep.get("fully_count_complete"))

    q = {k: rep.get("fully_qty_" + k) for k in ("expected", "good", "missing", "surplus")}
    if any(v is not None for v in q.values()):
        riep = {
            "attesi_sul_carico": q["expected"],
            "contati_buoni_da_fully": q["good"],
            "mancanti": q["missing"],
            "in_piu": q["surplus"],
        }
        if st == "counting_in_progress":
            riep["nota"] = (
                "NUMERI PROVVISORI: il conteggio di Fully non è concluso, questi "
                "totali possono ancora cambiare. Non presentarli come definitivi."
            )
        b["riepilogo_carico_fully"] = riep

    if rep.get("fully_last_synced_at"):
        b["aggiornamento_dati_fully"] = _fully_eta_fotografia(rep["fully_last_synced_at"])

    # Dato storico: etichettato, mai usato come prova di arrivo.
    if rep.get("received_at_legacy_manual"):
        b["dato_storico_spunta_manuale"] = {
            "data": rep.get("received_at_legacy_manual"),
            "nota": (
                "DATO STORICO: spunta di ricezione messa a mano prima dell'integrazione "
                "Fully. NON è una prova di arrivo e NON è una conferma di Fully."
            ),
        }
    return b


def _fully_blocco_spedizione(s: dict, rep: dict = None) -> dict:
    """La spedizione/ASN come blocco leggibile. L'arrivo della merce viene da
    fully_replenishments (rep), non da status/received_at di questa riga.
    I dati mancanti si dichiarano, non si deducono."""
    b = {
        "asn": s.get("shipment_number"),
        "arrivo_in_fully": _fully_blocco_arrivo(rep, s),
        "file_asn": s.get("asn_file_path") or "file ASN non presente a sistema",
        "destinazione": s.get("destination"),
        "stato_asn": s.get("status"),
        "nota_stato_asn": (
            "'stato_asn' descrive la spedizione partita dalla fabbrica: NON dice se la "
            "merce è arrivata in Fully. Per l'arrivo usa solo 'arrivo_in_fully'."
        ),
        "inviata_il": s.get("shipped_at"),
        "corriere": s.get("courier"),
        "tracking": s.get("tracking_number"),
        "numero_carico_fully": (
            s.get("fully_replenishment_id")
            or "NON registrato a sistema: senza numero di carico non esiste "
               "riconciliazione Fully per questa spedizione"
        ),
    }
    if s.get("fully_verified_on"):
        b["verifica_manuale_bambu"] = {
            "data": s.get("fully_verified_on"),
            "nota": _FULLY_NOTA_VERIFICA,
        }
    return b


def _fully_forme_cliente(o: dict) -> list:
    """Le identità con cui un cliente può essere cercato: nome persona,
    ragione sociale, email (stessi campi della ricerca cliente esistente)."""
    c = o.get("customers") or {}
    if not isinstance(c, dict):
        c = {}
    nome = " ".join(f"{c.get('first_name', '')} {c.get('last_name', '')}".split())
    return [f for f in (nome, c.get("business_name"), c.get("email")) if f]


def _fully_match_cliente(query: str, forme: list):
    """(livello, punteggio) migliore fra le forme note del cliente, con gli stessi
    tre livelli del match produttori: esatto / parziale / somiglianza."""
    q, qs = _bto_norm_producer(query), _bto_squash(query)
    rango = {"esatto": 3, "parziale": 2, "somiglianza": 1, None: 0}
    best = (None, 0.0)
    for f in forme:
        fn, fs = _bto_norm_producer(f), _bto_squash(f)
        if q == fn or (qs and qs == fs):
            return ("esatto", 1.0)
        s = round(max(
            difflib.SequenceMatcher(None, q, fn).ratio(),
            difflib.SequenceMatcher(None, qs, fs).ratio(),
        ), 3)
        if len(q) >= 3 and (q in fn or fn in q or (len(qs) >= 3 and (qs in fs or fs in qs))):
            liv = "parziale"
        elif s >= _BTO_SIMIL_MIN:
            liv = "somiglianza"
        else:
            continue
        if (rango[liv], s) > (rango[best[0]], best[1]):
            best = (liv, s)
    return best


def _fully_risolvi_cliente(query: str, raw_orders: list) -> dict:
    """Risolve il nome digitato su uno dei clienti realmente presenti negli ordini,
    raggruppando per cliente (email se c'è, altrimenti nome+azienda). Come per i
    produttori: vince il livello più alto, più clienti allo stesso livello =
    richiesta di chiarimento, mai una scelta al posto dell'utente."""
    clienti = {}
    for o in raw_orders:
        forme = _fully_forme_cliente(o)
        if not forme:
            continue
        c = o.get("customers") or {}
        chiave = _bto_norm_producer(c.get("email") or " | ".join(forme))
        g = clienti.get(chiave)
        if g is None:
            g = clienti[chiave] = {"forme": forme, "ordini": []}
        g["ordini"].append(o)

    rango = {"esatto": 3, "parziale": 2, "somiglianza": 1}
    candidati = []
    for chiave, g in clienti.items():
        liv, punteggio = _fully_match_cliente(query, g["forme"])
        if liv:
            candidati.append({
                "cliente": " | ".join(g["forme"]),
                "match": liv,
                "punteggio": punteggio,
                "_ordini": g["ordini"],
            })
    if not candidati:
        return {"livello": None, "candidati": []}
    top = max(rango[c["match"]] for c in candidati)
    vincenti = [c for c in candidati if rango[c["match"]] == top]
    vincenti.sort(key=lambda c: -c["punteggio"])
    return {"livello": vincenti[0]["match"], "candidati": vincenti}


def _fully_traccia_ordine(o: dict, spedizioni: list, righe_recon: list,
                          righe_lri: list, rep_per_asn: dict = None) -> dict:
    """La catena completa di UN ordine: contenuto -> stato -> fabbrica -> ASN ->
    carico Fully -> conteggio riga per riga -> anomalie -> ripartenza."""
    num = o.get("order_number")
    c = o.get("customers") or {}
    if not isinstance(c, dict):
        c = {}
    prodotti = o.get("products")
    if isinstance(prodotti, dict):
        prodotti = [prodotti]
    elif not isinstance(prodotti, list):
        prodotti = []
    nomi_prodotti = [p.get("name") for p in prodotti if isinstance(p, dict) and p.get("name")]

    os_code = o.get("order_status")
    tr = {
        "ordine": num,
        "cliente": " | ".join(
            v for v in (
                " ".join(f"{c.get('first_name', '')} {c.get('last_name', '')}".split()),
                c.get("business_name"),
            ) if v
        ) or None,
        "stato_ordine": os_code,
        "stato_descrizione": CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A"),
        "pagamento": o.get("payment_status"),
        "contenuto": {
            "prodotti": nomi_prodotti or ["dettaglio prodotti non valorizzato alla fonte"],
            "pezzi_ordinati": o.get("quantity"),
        },
        "produttore": ((o.get("producers") or {}).get("name") if isinstance(o.get("producers"), dict) else None),
        "fabbrica": {
            "file_produzione_confermato_il": o.get("producer_reception_confirmed_at"),
            "partito_dalla_fabbrica_il": o.get("producer_shipped_at"),
            "corriere": o.get("producer_courier"),
            "tracking": o.get("producer_tracking"),
        },
    }

    rep_per_asn = rep_per_asn or {}
    reps_ordine = [
        rep_per_asn.get(_fully_norm_num(s.get("shipment_number"))) for s in spedizioni
    ]
    stati_arrivo = [
        str(r.get("arrival_status")) for r in reps_ordine if isinstance(r, dict)
    ]
    conteggio_aperto = [
        r for r in reps_ordine
        if isinstance(r, dict) and r.get("arrival_status") == "counting_in_progress"
    ]

    if spedizioni:
        tr["spedizioni_asn"] = [
            _fully_blocco_spedizione(s, rep) for s, rep in zip(spedizioni, reps_ordine)
        ]
    else:
        tr["spedizioni_asn"] = []
        tr["nota_spedizioni"] = (
            "Nessuna spedizione/ASN collegata a questo ordine: non è ancora "
            "partito verso la logistica. Dillo apertamente, non dedurre oltre."
        )

    problemi_aperti, problemi_gestiti = [], []
    if righe_recon:
        righe = [_fully_riga_conteggio(r) for r in righe_recon]
        # Totali per RIGA D'ORDINE (ean+taglia), NON per riga di carico: un ordine
        # può comparire su più carichi (es. ammanco sanato da un carico correttivo,
        # come 0329-03-26) e la somma cieca duplicherebbe i richiesti e conterebbe
        # come mancante merce già recuperata.
        linee = {}
        for r in righe_recon:
            k = (r.get("ean_code"), r.get("size_variation"))
            l = linee.setdefault(k, {"dem": 0, "good": 0, "hurt": 0, "surplus": 0, "miss": 0})
            l["dem"] = max(l["dem"], r.get("qty_demanded") or 0)
            l["good"] += r.get("qty_good") or 0
            l["hurt"] += r.get("qty_hurt") or 0
            l["surplus"] += r.get("qty_surplus") or 0
            l["miss"] += r.get("qty_missing") or 0
        tot = {
            "richiesti_dal_cliente": sum(l["dem"] for l in linee.values()),
            "contati_buoni_da_fully": sum(l["good"] for l in linee.values()),
            # Quanto manca DAVVERO al cliente rispetto al pagato, al netto dei
            # carichi correttivi già arrivati.
            "mancanti_rispetto_al_pagato": sum(
                max(0, l["dem"] - l["good"]) for l in linee.values()
            ),
            "mancanti_contati_da_fully": sum(l["miss"] for l in linee.values()),
            "danneggiati": sum(l["hurt"] for l in linee.values()),
            "in_piu": sum(l["surplus"] for l in linee.values()),
        }
        blocco = {"righe": righe, "totali": tot}
        blocco.update(_fully_eta_fotografia(max(
            str(r.get("fully_synced_at") or "") for r in righe_recon
        )))
        tr["conteggio_fully"] = blocco
        for riga in righe:
            if riga.get("avvisi"):
                (problemi_gestiti if riga.get("anomalia") == "GIÀ GESTITA"
                 else problemi_aperti).append(riga)
    else:
        tr["conteggio_fully"] = None
        tr["nota_conteggio"] = (
            "NESSUNA riga di riconciliazione Fully per questo ordine: il conteggio "
            "riga per riga (buoni/mancanti/danneggiati) non esiste a sistema. "
            "Dillo apertamente invece di dedurre se la merce è arrivata integra."
        )
        # Fallback storico: le ricezioni caricate a mano prima dell'integrazione
        # Fully (era Kelmar) vivono in logistics_received_items.
        if righe_lri:
            tr["ricezione_storica_manuale"] = {
                "righe": [
                    {
                        "prodotto": r.get("product_name"),
                        "taglia": r.get("size"),
                        "sku": r.get("sku"),
                        "quantita_ricevuta": r.get("quantity_received"),
                        "caricata_il": r.get("uploaded_at"),
                    }
                    for r in righe_lri
                ],
                "nota": (
                    "Ricezione STORICA caricata a mano dalla logistica prima "
                    "dell'integrazione Fully: NON è il conteggio Fully e non "
                    "distingue mancanti o danneggiati."
                ),
            }

    tr["anomalie_da_gestire"] = [
        {"prodotto": r.get("prodotto"), "taglia": r.get("taglia"), "avvisi": r["avvisi"]}
        for r in problemi_aperti
    ]
    tr["anomalie_gia_gestite"] = [
        {"prodotto": r.get("prodotto"), "taglia": r.get("taglia"), "avvisi": r["avvisi"],
         "gestita_il": r.get("anomalia_gestita_il"), "nota_gestione": r.get("nota_gestione")}
        for r in problemi_gestiti
    ]

    if o.get("logistics_shipped_at"):
        tr["ripartenza_verso_cliente"] = {
            "spedito_da_fully_il": o.get("logistics_shipped_at"),
            "corriere": o.get("logistics_courier"),
            "tracking": o.get("logistics_tracking"),
        }

    # Valutazione "pronto o no per il cliente": SOLO composizione di fatti presenti,
    # niente deduzioni dove il dato non c'è.
    if os_code in ("shipped_to_customer", "shipped"):
        val = "Già spedito al cliente" + (
            f" il {o.get('logistics_shipped_at')}" if o.get("logistics_shipped_at") else ""
        ) + "."
    elif righe_recon:
        tot = tr["conteggio_fully"]["totali"]
        if not tot["mancanti_rispetto_al_pagato"] and not tot["danneggiati"]:
            val = (
                f"Fully ha contato {tot['contati_buoni_da_fully']} pezzi buoni su "
                f"{tot['richiesti_dal_cliente']} richiesti. "
                f"Stato ordine: {tr['stato_descrizione']}."
            )
        else:
            val = (
                f"Spedibile solo IN PARTE: {tot['contati_buoni_da_fully']} pezzi buoni su "
                f"{tot['richiesti_dal_cliente']} richiesti/pagati (il cliente riceverebbe "
                f"{tot['mancanti_rispetto_al_pagato']} pezzi in meno di quanto ha pagato; "
                f"danneggiati {tot['danneggiati']})."
            )
        if tot["in_piu"]:
            val += (
                f" ATTENZIONE: {tot['in_piu']} pezzi in più da consegnare e da fatturare."
            )
    elif spedizioni:
        if stati_arrivo and all(x == "no_arrival_evidence" for x in stati_arrivo):
            val = (
                "Merce partita verso Fully ma NESSUNA PROVA DI ARRIVO (né conteggio "
                "Fully né spunta manuale) e nessun conteggio riga per riga: non si può "
                "dire che sia arrivata. Non dedurre."
            )
        else:
            val = (
                "Spedizione verso la logistica presente ma NESSUN conteggio Fully: "
                "non è possibile dire se la merce è completa. Non dedurre."
            )
    else:
        val = "Non ancora partito verso la logistica: nessun ASN collegato."

    # Il conteggio aperto vince su qualunque verdetto "pronto": i numeri sopra
    # sono una fotografia parziale, non il conteggio finale di Fully.
    if conteggio_aperto:
        dettagli = "; ".join(
            f"{r.get('shipment_number')}: {r.get('fully_count_rows_done') or 0} righe "
            f"contate su {r.get('fully_count_rows')}"
            for r in conteggio_aperto
        )
        val += (
            " ATTENZIONE: Fully NON ha ancora concluso il conteggio di questa merce "
            f"({dettagli}). I numeri qui sopra sono PROVVISORI e possono cambiare: "
            "non dare l'ordine per verificato e segnala che si può SOLLECITARE FULLY."
        )
    tr["valutazione_spedibilita"] = val
    return tr


def tool_tracciamento_fully(numero: str = None, cliente: str = None) -> dict:
    """Tracciamento A-Z di un ordine custom (solo staff): ordine -> fabbrica ->
    ASN -> carico Fully -> conteggio riga per riga -> anomalie -> ripartenza.
    Accetta un numero d'ordine, un numero di spedizione/ASN o un nome cliente
    (match tollerante come per i produttori). I filtri della edge function
    vengono inoltrati MA il risultato è comunque rifiltrato qui: la correttezza
    non dipende dal comportamento del server."""
    numero = (numero or "").strip()
    cliente = (cliente or "").strip()
    if not numero and not cliente:
        return {"error": "Serve un numero d'ordine, un numero ASN o un nome cliente."}

    raw_orders, err = _fully_rows("orders")
    if err:
        return err
    spedizioni_tutte, err = _fully_rows("shipments")
    if err:
        return err
    # Vista arrivi: unica fonte di verità su "la merce è arrivata?".
    # Copre le sole spedizioni verso Fully (destination='fully'); le dirette al
    # cliente non ci sono e vengono gestite in _fully_blocco_arrivo.
    arrivi, err = _fully_rows("fully_replenishments")
    if err:
        return err
    rep_per_asn = {
        _fully_norm_num(r.get("shipment_number")): r for r in arrivi
    }

    # Mappe di collegamento: ordine.id -> spedizioni (dalla lista annidata
    # shipment_orders dentro shipments) e numero ASN -> spedizione.
    ship_per_ordine = {}
    ship_per_asn = {}
    for s in spedizioni_tutte:
        ship_per_asn[_fully_norm_num(s.get("shipment_number"))] = s
        for so in (s.get("shipment_orders") or []):
            if isinstance(so, dict) and so.get("custom_order_id"):
                ship_per_ordine.setdefault(so["custom_order_id"], []).append(s)

    def _spedizioni_di(o, recon_ordine):
        """Unione dei due agganci: link shipment_orders + ASN citati nelle righe
        di riconciliazione (un carico correttivo può non avere il link diretto)."""
        sped = list(ship_per_ordine.get(o.get("id"), []))
        visti = {_fully_norm_num(s.get("shipment_number")) for s in sped}
        for r in recon_ordine:
            k = _fully_norm_num(r.get("shipment_number"))
            if k and k not in visti and k in ship_per_asn:
                sped.append(ship_per_asn[k])
                visti.add(k)
        sped.sort(key=lambda s: str(s.get("shipped_at") or ""))
        return sped

    def _recon_di(order_number=None, shipment_number=None):
        """Righe di riconciliazione: il filtro viene inoltrato alla edge function
        e comunque riapplicato qui (difesa: finché il filtro server non è
        verificato in produzione, un server che lo ignorasse restituirebbe tutto)."""
        extra = {}
        if order_number:
            extra["order_number"] = order_number
        if shipment_number:
            extra["shipment_number"] = shipment_number
        righe, err2 = _fully_rows("fully_reconciliation", extra_params=extra or None)
        if err2:
            return None, err2
        if order_number:
            righe = [r for r in righe if _fully_norm_num(r.get("order_number")) == _fully_norm_num(order_number)]
        if shipment_number:
            righe = [r for r in righe if _fully_norm_num(r.get("shipment_number")) == _fully_norm_num(shipment_number)]
        return righe, None

    _lri_cache = {}

    def _lri_di(order_number):
        # Un solo scarico per invocazione anche se serve per più ordini.
        if "righe" not in _lri_cache:
            righe, err2 = _fully_rows("logistics_received_items")
            _lri_cache["righe"] = righe or [] if not err2 else []
        return [
            r for r in _lri_cache["righe"]
            if _fully_norm_num(r.get("order_number")) == _fully_norm_num(order_number)
        ]

    base = {"tipo": "tracciamento_fully", "nota_verifica": _FULLY_NOTA_VERIFICA}

    # --- Caso ASN: si traccia la spedizione intera, con tutti i suoi ordini ---
    nq = _fully_norm_num(numero)
    if numero and (nq.startswith(("asn-", "cross-")) or nq in ship_per_asn):
        s = ship_per_asn.get(nq)
        if not s:
            return {
                **base, "cercato": numero, "trovato": False,
                "nota": (
                    f"Nessuna spedizione con numero '{numero}'. Non dedurre: chiedi "
                    "il numero ASN esatto o il numero d'ordine."
                ),
            }
        righe, err2 = _recon_di(shipment_number=s.get("shipment_number"))
        if err2:
            return err2
        ordini_ids = [so.get("custom_order_id") for so in (s.get("shipment_orders") or []) if isinstance(so, dict)]
        ordini = [o for o in raw_orders if o.get("id") in ordini_ids]
        per_ordine = {}
        for r in righe:
            per_ordine.setdefault(r.get("order_number"), []).append(r)
        dettaglio = []
        for o in sorted(ordini, key=lambda x: str(x.get("order_number") or "")):
            num_o = o.get("order_number")
            recon_o = per_ordine.get(num_o, [])
            righe_fmt = [_fully_riga_conteggio(r) for r in recon_o]
            dettaglio.append({
                "ordine": num_o,
                "stato_ordine": o.get("order_status"),
                "stato_descrizione": CUSTOM_STATUS_LABELS.get(o.get("order_status"), o.get("order_status")),
                "righe_conteggio_fully": righe_fmt or "nessuna riga di riconciliazione per questo ordine",
                "anomalie": [r for r in righe_fmt if r.get("avvisi")],
            })
        rep = rep_per_asn.get(nq)
        out = {
            **base, "cercato": numero, "trovato": True,
            "spedizione": _fully_blocco_spedizione(s, rep),
            "ordini_nella_spedizione": len(ordini),
            "righe_conteggio_totali": len(righe),
            "dettaglio_per_ordine": dettaglio,
        }
        if isinstance(rep, dict) and rep.get("arrival_status") == "counting_in_progress":
            out["nota_conteggio_in_corso"] = (
                "Fully NON ha ancora concluso il conteggio di questo carico "
                f"({rep.get('fully_count_rows_done') or 0} righe contate su "
                f"{rep.get('fully_count_rows')}). Le righe qui sotto sono PROVVISORIE: "
                "dillo prima dei numeri e segnala che si può SOLLECITARE FULLY perché "
                "chiuda il conteggio. NON dire che la merce è arrivata e contata."
            )
        if righe:
            out.update(_fully_eta_fotografia(max(str(r.get("fully_synced_at") or "") for r in righe)))
        else:
            out["nota_conteggio"] = (
                "Nessuna riga di riconciliazione Fully per questa spedizione "
                "(manca il numero di carico?): dillo apertamente."
            )
        return out

    # --- Caso numero d'ordine ---
    if numero:
        o = next(
            (x for x in raw_orders if _fully_norm_num(x.get("order_number")) == nq),
            None,
        )
        if not o:
            return {
                **base, "cercato": numero, "trovato": False,
                "nota": (
                    f"Nessun ordine custom con numero '{numero}'. NON dedurre e non "
                    "ripiegare su altre piattaforme: chiedi il numero completo "
                    "(formato NNNN-MM-YY, con eventuale suffisso)."
                ),
            }
        recon_o, err2 = _recon_di(order_number=o.get("order_number"))
        if err2:
            return err2
        lri_o = _lri_di(o.get("order_number")) if not recon_o else []
        return {
            **base, "cercato": numero, "trovato": True,
            "tracciamento": _fully_traccia_ordine(
                o, _spedizioni_di(o, recon_o), recon_o, lri_o, rep_per_asn
            ),
        }

    # --- Caso cliente (match tollerante, stesso schema dei produttori) ---
    ris = _fully_risolvi_cliente(cliente, raw_orders)
    if not ris["candidati"]:
        return {
            **base, "cercato": cliente, "trovato": False,
            "richiesta_chiarimento": False,
            "nota": (
                f"Nessun cliente corrisponde né somiglia a '{cliente}'. Dillo "
                "apertamente e chiedi nome, azienda o email esatti."
            ),
        }
    if len(ris["candidati"]) > 1:
        return {
            **base, "cercato": cliente, "trovato": False,
            "richiesta_chiarimento": True,
            "candidati": [
                {"cliente": c["cliente"], "match": c["match"], "punteggio": c["punteggio"]}
                for c in ris["candidati"]
            ],
            "nota": (
                f"Più clienti compatibili con '{cliente}' (match per {ris['livello']}): "
                "NON scegliere tu, elenca i candidati e chiedi quale intende."
            ),
        }

    scelto = ris["candidati"][0]
    ordini = sorted(
        scelto["_ordini"], key=lambda x: str(x.get("created_at") or ""), reverse=True
    )
    righe_tutte, err2 = _recon_di()
    if err2:
        return err2
    recon_per_num = {}
    for r in righe_tutte:
        recon_per_num.setdefault(_fully_norm_num(r.get("order_number")), []).append(r)

    tracce = []
    for o in ordini[:_FULLY_MAX_ORDINI_CLIENTE]:
        recon_o = recon_per_num.get(_fully_norm_num(o.get("order_number")), [])
        lri_o = _lri_di(o.get("order_number")) if not recon_o else []
        tracce.append(
            _fully_traccia_ordine(
                o, _spedizioni_di(o, recon_o), recon_o, lri_o, rep_per_asn
            )
        )

    out = {
        **base, "cercato": cliente, "trovato": True,
        "cliente_risolto": scelto["cliente"],
        "tipo_match": scelto["match"],
        "punteggio_match": scelto["punteggio"],
        "ordini_totali_cliente": len(ordini),
        "tracciamenti": tracce,
    }
    if len(ordini) > _FULLY_MAX_ORDINI_CLIENTE:
        out["ordini_non_tracciati"] = [
            {"ordine": o.get("order_number"), "stato": o.get("order_status")}
            for o in ordini[_FULLY_MAX_ORDINI_CLIENTE:]
        ]
        out["nota_limite"] = (
            f"Tracciati in dettaglio solo i {_FULLY_MAX_ORDINI_CLIENTE} ordini più "
            "recenti; gli altri sono elencati con il solo stato. Dichiaralo."
        )
    if scelto["match"] != "esatto":
        out["nota_interpretazione"] = (
            f"L'utente ha scritto '{cliente}' e lo strumento l'ha risolto sul cliente "
            f"'{scelto['cliente']}' (match per {scelto['match']}). DICHIARALO prima dei dati."
        )
    return out


def _execute_chat_tool(name: str, tool_input: dict, user_message: str, role: str = DEFAULT_ROLE):
    role = _normalize_role(role)
    allowed = ROLE_TOOLS[role]
    blocked_platforms = ROLE_BLOCKED_PLATFORMS[role]
    try:
        # Difesa in profondità: se il tool non è consentito per la modalità, rifiuta.
        if name not in allowed:
            return {"error": f"Strumento '{name}' non disponibile nella modalità {role}."}
        if name == "cerca_ordine_per_numero":
            return tool_cerca_ordine_per_numero(
                tool_input.get("numero"), tool_input.get("piattaforma"), blocked_platforms
            )
        if name == "cerca_ordini_per_cliente":
            return tool_cerca_ordini_per_cliente(tool_input.get("nome"))
        if name == "statistiche_ordini_custom":
            return tool_statistiche_ordini_custom(
                tool_input.get("cliente"), tool_input.get("mese")
            )
        if name == "prezzi_listino":
            return tool_prezzi_listino(tool_input.get("query"), tool_input.get("tipo"))
        if name == "catalogo_btoweb":
            return tool_catalogo_btoweb(
                tool_input.get("query"), tool_input.get("sku"), tool_input.get("tipo")
            )
        if name == "ordini_per_produttore":
            return tool_ordini_per_produttore(tool_input.get("produttore"))
        if name == "tracciamento_fully":
            return tool_tracciamento_fully(
                tool_input.get("numero"), tool_input.get("cliente")
            )
        if name == "rispondi_dal_manuale":
            return tool_rispondi_dal_manuale(tool_input.get("argomento"), user_message)
        return {"error": f"Strumento sconosciuto: {name}"}
    except Exception as e:
        return {"error": f"Errore nell'esecuzione di {name}: {str(e)}"}


def chat_with_tools(chat_id: str, user_message: str, role: str = DEFAULT_ROLE) -> str:
    """Loop tool use: Haiku decide, eseguiamo le funzioni esistenti, Haiku compone."""
    if not ANTHROPIC_API_KEY:
        return "Errore: ANTHROPIC_API_KEY non configurata."

    role = _normalize_role(role)
    active_tools = [t for t in CHAT_TOOLS if t["name"] in ROLE_TOOLS[role]]

    history = get_recent_messages(chat_id)
    system = SYSTEM_PROMPT + "\n\n" + ROLE_PROMPTS[role] + "\n\n" + TOOL_SYSTEM_SUFFIX

    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        for _ in range(4):  # cap iterazioni tool
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                system=system,
                tools=active_tools,
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
                result = _execute_chat_tool(block.name, block.input or {}, user_message, role)
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
        # role seleziona modalità utente (staff default, b2b/retail predisposti).
        bot_reply = chat_with_tools(request.chat_id, request.message, request.role)

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
def custom_debug(request: Request, limit: int = 3, resource: str = "orders"):
    try:
        # Inoltra alla edge function TUTTI i parametri extra della query string
        # (order_number, shipment_number, only_discrepancies, ...): prima venivano
        # scartati in silenzio da FastAPI e la risposta tornava non filtrata.
        extra = {
            k: v for k, v in request.query_params.items()
            if k not in ("resource", "limit")
        }
        data = get_custom_resource(resource, limit, extra_params=extra or None)
        return {
            "resource": resource,
            "forwarded_params": extra,
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
            WHERE category = 'manuale'
            ORDER BY title ASC
            """
        )

        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return {"result": "no knowledge"}

        # Match per-parola su TUTTI i chunk, come get_knowledge_context
        query_words = [w.strip().lower() for w in q.split() if len(w.strip()) > 2]
        scored = []
        seen = set()

        for row in rows:
            text = row[0] or ""
            for line in text.split("\n"):
                line_clean = line.strip()
                if not line_clean or line_clean in seen:
                    continue
                line_lower = line_clean.lower()
                score = sum(1 for word in query_words if word in line_lower)
                if score > 0:
                    scored.append((score, line_clean))
                    seen.add(line_clean)

        scored.sort(key=lambda x: x[0], reverse=True)

        return {
            "query": q,
            "matches": [line for _, line in scored[:10]]
        }

    except Exception as e:
        return {"error": str(e)}
