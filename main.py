from fastapi import FastAPI, Request, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import re
import json
import hmac
import math
import difflib
import hashlib
import unicodedata
from datetime import datetime, timezone
from collections import Counter
import psycopg2
import requests
import anthropic
from woocommerce import API
from docx import Document

# docs_url/redoc_url/openapi_url a None: FastAPI pubblicava da sola /docs,
# /redoc e /openapi.json, cioè il catalogo completo degli endpoint con i loro
# parametri. Nessuno li usa e sono una mappa servita a chiunque.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")


DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
BTO_API_URL = "https://hckmzdztgffxovpbiwgw.supabase.co/functions/v1/bto-bot-api"
BTO_API_KEY = os.getenv("BTO_API_KEY")
# Chiave amministrativa degli endpoint di servizio (dump ordini, proxy sulla
# edge function, scheda ordine, manuale, reimport). Non è una chiave verso
# l'esterno come le altre: è quella che i chiamanti devono presentare a NOI.
BOT_ADMIN_KEY = os.getenv("BOT_ADMIN_KEY")

# --- CHIAVI CLIENT DI /chat E /feedback (LOTTO SICUREZZA, FASE 3) ------------
# Ogni frontend legittimo presenta l'header 'x-bot-client-key'; a ogni chiave
# corrisponde il ruolo che assegna il SERVER, perché il campo 'role' del body
# lo scrive il chiamante e non fa fede. Dalla fase 3 chi non manda la chiave,
# o ne manda una sconosciuta, viene rifiutato (401 pulito, niente dettagli
# tecnici nella risposta: quelli restano nei log). Lista di coppie e non dict
# perché più chiavi possono dare lo stesso ruolo: mini-sito e script di
# diagnosi sono entrambe staff (la seconda esiste perché senza una chiave
# nostra, dopo i rifiuti, le prove live non sarebbero più possibili). Il
# widget Shopify resta predisposto, chiave non ancora generata.
BOT_CLIENT_KEYS = [
    ("staff", os.getenv("BOT_CLIENT_KEY_MINISITO")),
    ("staff", os.getenv("BOT_CLIENT_KEY_DIAGNOSI")),
    ("retail", os.getenv("BOT_CLIENT_KEY_WIDGET_SHOPIFY")),
]


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


class FeedbackRequest(BaseModel):
    question: str
    wrong_reply: str
    correct_reply: str

# --- ERRORI DI CANALE --------------------------------------------------------
# "non ho potuto guardare" NON e' "non c'e'". Quando una fonte risponde con un
# errore (401, timeout, JSON rotto, qualsiasi cosa) il bot non deve dedurne
# un'assenza: deve dire che QUELLA fonte non e' consultabile e riportare quello
# che le altre hanno detto. Il dettaglio tecnico (status HTTP, response.text,
# str(e)) resta nel log del server e non raggiunge mai il modello ne' l'utente:
# prima finiva dentro 'details' e da li' nel tool_result, cioe' a video.
FONTI_FRASE = {
    "custom": (
        "Non riesco a leggere gli ordini custom (kanokimonos.app) in questo "
        "momento: è un problema tecnico della fonte, non una risposta sull'ordine."
    ),
    "woocommerce": (
        "Il canale catalogo (kanokimonos.com) non è consultabile: l'integrazione "
        "con il sito non è attiva, quindi su quel canale non posso né confermare "
        "né escludere nulla."
    ),
    "btoweb": (
        "Non riesco a leggere gli ordini di fabbrica (btoweb) in questo momento: "
        "è un problema tecnico della fonte."
    ),
    "listino": (
        "Non riesco a leggere il listino prezzi in questo momento: è un problema "
        "tecnico della fonte, non un prezzo assente."
    ),
    "manuale": (
        "Non riesco a leggere il manuale in questo momento: è un problema tecnico "
        "della fonte, non una procedura assente."
    ),
}

FRASE_CANALE_GENERICA = (
    "Una delle fonti non è consultabile in questo momento: è un problema tecnico, "
    "non una risposta sul merito."
)


def errore_canale(fonte: str, dettaglio: str = None) -> str:
    """Frase pulita per l'utente + dettaglio tecnico SOLO nel log del server."""
    if dettaglio:
        print(f"[FONTE {fonte}] {dettaglio}")
    return FONTI_FRASE.get(fonte, FRASE_CANALE_GENERICA)


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

    try:
        response = requests.get(
            KANOCUSTOM_FUNCTION_URL,
            headers=headers,
            params=params,
            timeout=60
        )
    except Exception as e:
        # Timeout, DNS, SSL: prima risalivano al catch-all e uscivano come testo
        # tecnico. Sono errori di CANALE, non risposte sull'ordine.
        return {
            "error": errore_canale("custom", f"connessione fallita su resource={resource}: {e}"),
            "fonte": "custom",
        }

    if response.status_code != 200:
        return {
            "error": errore_canale(
                "custom",
                f"HTTP {response.status_code} su resource={resource}: {response.text}",
            ),
            "fonte": "custom",
        }

    try:
        return response.json()
    except Exception as e:
        return {
            "error": errore_canale(
                "custom", f"risposta non JSON su resource={resource}: {e}"
            ),
            "fonte": "custom",
        }

def _as_bool(value):
    """True/False dai vari modi in cui la sorgente può scrivere un booleano.
    None resta None: 'non valorizzato' non è 'falso'."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    v = str(value).strip().lower()
    if v in ("true", "t", "1", "yes", "si", "sì"):
        return True
    if v in ("false", "f", "0", "no"):
        return False
    return None


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
    sizes_selected_at = None
    selected_sizes = None

    # In alcuni record admin_design_url + le taglie sono dentro selected_variations.
    # La conferma della BOZZA invece NON si legge più da qui: la sorgente espone
    # ora i campi normalizzati draft_confirmed / draft_confirmed_at /
    # design_confirmed_source, che sono l'unica verità su quell'asse.
    if isinstance(selected_variations, dict):
        admin_design_url = selected_variations.get("admin_design_url")
        admin_design_uploaded_at = selected_variations.get("admin_design_uploaded_at")
        sizes_selected_at = selected_variations.get("sizes_selected_at")
        selected_sizes = selected_variations.get("selected_sizes")

    return {
        "id": order.get("id"),
        "piattaforma": "kanokimonos.app (ordine custom)",
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
        # Conferma della bozza: asse INDIPENDENTE sia da order_status sia dal
        # campo workflow interno. Non si deduce dall'uno né dall'altro.
        "draft_confirmed": _as_bool(order.get("draft_confirmed")),
        "draft_confirmed_at": order.get("draft_confirmed_at"),
        "design_confirmed_source": order.get("design_confirmed_source"),
        "design_confirmed_by": order.get("design_confirmed_by"),
        "design_confirmed_note": order.get("design_confirmed_note"),
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
            return {
                "error": errore_canale("custom", f"struttura inattesa: {data}"),
                "fonte": "custom",
            }
    else:
        return {
            "error": errore_canale("custom", f"tipo di risposta inatteso: {type(data)}"),
            "fonte": "custom",
        }

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


def _bto_get(params: dict):
    """Chiamata grezza alla edge function btoweb: restituisce il JSON completo
    (data + count/total/disclaimer/source), disclaimer di stock incluso."""
    if not BTO_API_KEY:
        return {"error": errore_canale("btoweb", "BTO_API_KEY non configurata"), "fonte": "btoweb"}

    try:
        response = requests.get(
            BTO_API_URL,
            headers={"x-api-key": BTO_API_KEY},
            params=params,
            timeout=60,
        )
    except Exception as e:
        return {"error": errore_canale("btoweb", f"connessione fallita: {e}"), "fonte": "btoweb"}

    if response.status_code != 200:
        return {
            "error": errore_canale("btoweb", f"HTTP {response.status_code}: {response.text}"),
            "fonte": "btoweb",
        }

    try:
        data = response.json()
    except Exception:
        return {
            "error": errore_canale("btoweb", f"risposta non JSON: {response.text}"),
            "fonte": "btoweb",
        }

    if isinstance(data, list):
        return {"data": data}
    if isinstance(data, dict):
        return data
    return {
        "error": errore_canale("btoweb", f"struttura non riconosciuta: {data}"),
        "fonte": "btoweb",
    }


def yes_no_unknown(value):
    if value is True:
        return "Sì"
    if value is False:
        return "No"
    if value:
        return str(value)
    return "N/A"


def _righe_bozza(order: dict) -> list:
    """Stato della BOZZA, letto SOLO da draft_confirmed + design_confirmed_source.

    È un asse indipendente: non si deduce da order_status né dal campo workflow
    interno, e loro non si deducono da lui. Sui dati reali 198 ordini hanno la
    bozza confermata con il workflow ancora su 'pending_confirmation' e 58 hanno
    il workflow 'confirmed' senza la bozza confermata: incrociarli è sbagliato.
    """
    confermata = order.get("draft_confirmed")
    quando = order.get("draft_confirmed_at")
    source = order.get("design_confirmed_source")
    nota = order.get("design_confirmed_note")
    chi = order.get("design_confirmed_by")

    lines = ["Bozza (asse indipendente dallo stato ordine):"]

    if confermata is False:
        lines.append("- Bozza confermata: NO")
        lines.append(
            "- L'ordine attende l'approvazione della bozza. Questo è l'UNICO "
            "motivo valido per dire che un ordine è in attesa di approvazione."
        )
        return lines

    if confermata is not True:
        lines.append("- Bozza confermata: dato non valorizzato a sistema")
        lines.append(
            "- Non sai se la bozza sia stata approvata: dillo così, non dedurlo "
            "dallo stato dell'ordine."
        )
        return lines

    lines.append("- Bozza confermata: SÌ" + (f" (il {quando})" if quando else ""))

    if source == "customer":
        lines.append("- Approvata DAL CLIENTE, approvazione registrata a sistema.")
    elif source == "admin":
        lines.append(
            "- Confermata DALL'AMMINISTRATORE al posto del cliente. A sistema NON "
            "risulta nessuna approvazione del cliente: non dire che il cliente ha "
            "approvato, né sulla app né altrove. Se una sua approvazione esiste "
            "sta FUORI dal sistema (WhatsApp o mail) e va verificata lì: dilla "
            "come cosa da controllare, mai come cosa già avvenuta."
        )
        if nota:
            lines.append(f"- Nota di chi ha confermato la bozza: {nota}")
        else:
            lines.append(
                "- Chi ha confermato non ha lasciato nessuna nota. Le 'Note admin' "
                "dell'ordine sono un campo DIVERSO e non spiegano questa conferma: "
                "non riportarle come motivo della conferma."
            )
        if chi:
            lines.append(f"- Amministratore (identificativo interno, non è un nome): {chi}")
    else:
        lines.append(
            "- Origine della conferma NON registrata (ordine storico, precedente "
            "al tracciamento dell'origine). NON dire che l'ha approvata il "
            "cliente: di' che la bozza risulta confermata ma l'origine non è "
            "tracciata a sistema."
        )
    return lines


def format_custom_order_for_human(order: dict) -> str:
    lines = []
    lines.append(f"Ordine custom: {order.get('order_number') or order.get('id')}")
    # La piattaforma arriva al modello dai dati, non dalla sua deduzione. E va
    # detta per quello che è: il registro dell'ordine, mai mittente o destinatario.
    lines.append(
        "Piattaforma: kanokimonos.app (ordine custom). È dove l'ordine è "
        "REGISTRATO: la piattaforma non spedisce e non riceve merce."
    )
    # Stato di business REALE = order_status (con etichette usate anche nelle statistiche),
    # NON il campo workflow 'status' (spesso fermo su 'pending_confirmation' e fuorviante).
    os_code = order.get("order_status")
    os_label = CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A")
    lines.append(f"Stato ordine: {os_label}" + (f" [{os_code}]" if os_code else ""))
    if os_code in ("shipped_to_customer", "shipped"):
        lines.append(
            "Come dirlo: \"risulta spedito al cliente (stato registrato sulla "
            "piattaforma kanokimonos.app)\". La spedizione parte da Fully "
            "(magazzino logistico) oppure, SOLO per gli ordini custom come "
            "questo, direttamente dalla fabbrica del produttore, che salta "
            "Fully: sul custom esistono entrambe le strade e questa scheda "
            "non dice quale sia stata usata, quindi non darne per scontata "
            "nessuna delle due. Per gli ordini da CATALOGO (kanokimonos.com) "
            "la spedizione diretta non esiste: spedisce sempre Fully. "
            "MAI dire \"spedito a/su kanokimonos.app\"."
        )
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
    # (taglie) e dai campi producer_*. La conferma della bozza NON sta qui:
    # ha quattro esiti diversi, non due, e sta nel blocco Bozza qui sotto.
    si = lambda b: "Sì" if b else "No"
    design_caricato = bool(order.get("admin_design_url"))
    taglie_confermate = bool(order.get("sizes_selected_at")) or bool(order.get("selected_sizes"))
    producer_file_pronto = bool(order.get("producer_file_uploaded_at")) or bool(order.get("producer_file_path"))
    spedito_produttore = bool(order.get("producer_shipped_at"))
    ship_extra = ""
    if spedito_produttore:
        extra = " | ".join(filter(None, [order.get("producer_courier"), order.get("producer_tracking")]))
        ship_extra = f" ({extra})" if extra else ""

    lines.append("Checklist avanzamento:")
    lines.append(f"- Design caricato: {si(design_caricato)}")
    lines.append(f"- Taglie confermate: {si(taglie_confermate)}")
    lines.append(f"- Producer file pronto: {si(producer_file_pronto)}")
    lines.append(f"- Spedito dal produttore: {si(spedito_produttore)}{ship_extra}")
    lines.append("")
    lines.extend(_righe_bozza(order))
    lines.append("")
    # Fuori dalla checklist di proposito: customer_files/image_url sono vuoti
    # anche su ordini avanzati, quindi non è un Sì/No. Dentro l'elenco veniva
    # rimarcata con una ❌ e letta come "logo non ricevuto".
    lines.append("Logo cliente: dato non presente a sistema (né ricevuto né mancante)")
    lines.append("")
    lines.append(f"URL bozza admin: {order.get('admin_design_url') or 'N/A'}")
    lines.append(f"Bozza admin caricata il: {order.get('admin_design_uploaded_at') or 'N/A'}")
    lines.append(f"Bozza confermata il: {order.get('draft_confirmed_at') or 'N/A'}")
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
        lines.append(
            f"Ordini custom di {customer_name} ({len(orders)} totale) — "
            "piattaforma kanokimonos.app:"
        )
    else:
        lines.append(
            f"Ordini custom trovati: {len(orders)} — piattaforma kanokimonos.app"
        )
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

# --- RICERCA NEL MANUALE (lotto A): selezione normalizzata, consegna invariata
# La consegna resta "righe sciolte" (il passaggio a sezioni e' il lotto B):
# qui cambia solo COME le righe vengono scelte. Quattro difetti misurati e
# corretti (diagnosi in report.txt, 2026-08-07):
#   1. la query era divisa sui soli spazi: "fatture?" non combaciava mai
#      -> punteggiatura tolta ai bordi di ogni parola;
#   2. "come", "cosa", "deve" pesavano quanto "numerazione"
#      -> lista esplicita di parole vuote + pesi idf calcolati sul manuale;
#   3. fattura/fatture e gestisce/gestita non combaciavano (sottostringa
#      esatta e asimmetrica) -> confronto per RADICE, con prefisso;
#   4. a pari punteggio decideva l'ordine lessicografico dei chunk (Parte 1,
#      10, 11, ..., 2) e i confini dei chunk generavano 64 righe tronche
#      -> le candidate nascono dal testo RICUCITO in ordine numerico: le
#      righe sono per costruzione quelle vere del manuale (933, non 997) e
#      lo spareggio e' l'ordine del manuale.

# Parole vuote italiane: LISTA ESPLICITA E COMMENTATA, non filtro di lunghezza.
# Il filtro len>2 resta solo come pavimento per articoli/preposizioni corte;
# queste sono le parole che quel filtro NON prende e che hanno gia' inquinato
# le misure ("come funziona la numerazione" premiava le righe "Ecco come
# funziona:"). Accenti e apostrofi sono gia' normalizzati via (gia', perche').
_PAROLE_VUOTE = frozenset((
    # interrogative e relative
    "come", "cosa", "che", "chi", "quale", "quali", "qual", "quanto",
    "quanta", "quanti", "quante", "dove", "quando", "perche", "cui",
    # verbi di servizio senza contenuto
    "deve", "devo", "devi", "devono", "dobbiamo", "fare", "fatto", "essere",
    "sono", "siamo", "puo", "posso", "possiamo", "possono", "vuole",
    "voglio", "vogliamo", "serve", "servono", "bisogna", "avere", "abbiamo",
    "hanno", "stato", "stata", "viene", "vengono", "vorrei",
    # preposizioni articolate, articoli, congiunzioni
    "della", "delle", "dello", "degli", "dei", "del", "nel", "nella",
    "nelle", "negli", "sul", "sulla", "sulle", "alla", "alle", "allo",
    "agli", "una", "uno", "gli", "per", "con", "non", "tra", "fra", "gia",
    # avverbi e riempitivi
    "poi", "adesso", "anche", "ancora", "sempre", "solo", "cioe", "ecco",
    "piu", "meno", "molto", "tutto", "tutti", "tutte", "questo", "questa",
    "questi", "queste", "quello", "quella", "altro", "altra", "invece",
))

# Desinenze italiane, dalla piu' lunga alla piu' corta: la prima che combacia
# viene tolta. Uniscono singolare/plurale (fattura/fatture -> fattur) e le
# forme verbali comuni (gestisce/gestita -> gest). Soglie: si tronca solo una
# parola di almeno 5 caratteri e solo se la radice che resta ne ha almeno 4 --
# sotto, la troncatura fonde parole diverse (rischio annotato in report.txt).
_DESINENZE = (
    "azioni", "azione", "uzioni", "uzione", "amenti", "amento", "imenti",
    "imento", "iscono", "zioni", "zione", "sioni", "sione", "ature", "atura",
    "menti", "mento", "isce", "ando", "endo", "ioni", "ione",
    "are", "ere", "ire", "ata", "ate", "ati", "ato", "ita", "ite", "iti",
    "ito", "uta", "ute", "uti", "uto", "a", "e", "i", "o", "u",
)

# Apostrofi diventano spazi (l'ordine -> l ordine: l'articolo cade da solo al
# filtro di lunghezza); trattini lunghi diventano trattino semplice, cosi'
# "45-60" scritto con l'en-dash del docx combacia con "45-60" digitato.
_TRANS_QUERY = str.maketrans({"'": " ", "’": " ", "‘": " ",
                              "–": "-", "—": "-"})
# Punteggiatura da togliere ai BORDI delle parole ("fatture?" -> "fatture").
# Interna resta: "45-60" e "5,90" sono token legittimi.
_BORDI_PAROLA = "?!.,;:\"()[]{}<>«»“”…*•-/\\%€°#"


def _radice(parola: str) -> str:
    """Tronca la desinenza per unire singolare/plurale e forme verbali."""
    if len(parola) < 5:
        return parola
    for d in _DESINENZE:
        if parola.endswith(d) and len(parola) - len(d) >= 4:
            return parola[: -len(d)]
    return parola


def _tokenizza(testo: str) -> list:
    """minuscole, accenti piatti, apostrofi via, punteggiatura dai bordi.
    Tiene le parole di almeno 3 caratteri e i token con cifre di almeno 2
    ("75", "a2l", "5,90")."""
    testo = unicodedata.normalize("NFKD", testo.lower())
    testo = "".join(c for c in testo if not unicodedata.combining(c))
    testo = testo.translate(_TRANS_QUERY)
    token = []
    for t in testo.split():
        t = t.strip(_BORDI_PAROLA)
        if not t:
            continue
        if any(c.isdigit() for c in t):
            if len(t) >= 2:
                token.append(t)
        elif len(t) >= 3:
            token.append(t)
    return token


def _stems_query(query: str) -> list:
    """Radici delle parole portanti della query, senza duplicati, in ordine."""
    viste = set()
    out = []
    for t in _tokenizza(query):
        if t in _PAROLE_VUOTE:
            continue
        r = _radice(t)
        if r not in viste:
            viste.add(r)
            out.append(r)
    return out


def _num_parte(title: str) -> int:
    m = re.search(r"(\d+)", title or "")
    return int(m.group(1)) if m else 0


# --- SEZIONI DEL MANUALE (lotto B): la consegna porta il contesto -----------
# Il retrieval per righe sceglie bene MA consegna righe staccate dal loro
# contesto: dove la risposta non condivide parole con la domanda (il costo del
# cambio taglia sta nella riga "contributo di 5,90 euro") nessuna selezione
# per riga puo' arrivarci. Qui il manuale viene diviso in SEZIONI sui segnali
# gia' presenti nel testo, e la consegna diventa: 1-2 sezioni intere col loro
# titolo + le righe di prima come appendice (rete di sicurezza PER
# COSTRUZIONE: cio' che arrivava prima continua ad arrivare).

SOGLIA_SEZIONI = 3.0   # sotto questa massa idf la scelta di sezione e' rumore
                       # e si consegnano solo le righe. Taratura: sulle 26
                       # domande di misura il minimo con sezione sensata e'
                       # 6.46; la soglia sta a meta' (report.txt, lotto B).
TETTO_CONSEGNA = 8000  # tetto complessivo in caratteri della consegna
CAP_SEZIONE = 3500     # tetto per singola sezione: una sezione-mostro
                       # troncata qui lascia spazio ad appendice e 2a sezione

_RE_SEPARATORE_SEZIONE = re.compile(r"^[-_=]{10,}$")
_RE_PREFISSO_TITOLO = re.compile(r"^([^a-zà-ù]{12,}?)\s*[(:]")


def _e_titolo_sezione(riga: str) -> bool:
    """Confine di sezione, criterio V3 (misure in report.txt, lotto B):
    tutto-maiuscolo come _e_titolo_di_blocco, piu' due guardie.
    a) una riga dominata dalle cifre NON e' un titolo: "IBAN: LT29..." e'
       tutta maiuscola e senza guardia apriva una sezione-mostro da 26k;
    b) e' titolo anche un PREFISSO maiuscolo lungo seguito da '(' o ':'
       ("APPROVAZIONE DEL PAGAMENTO (bonifico arrivato)", "CAUSALE DEL
       BONIFICO: cambia..."), che il tutto-maiuscolo perde.
    Funzione SEPARATA da _e_titolo_di_blocco: quella serve alla guida
    taglie, che oggi funziona e non si tocca (vincolo del lotto)."""
    r = riga.strip()
    if len(r) < 12 or r.startswith(("-", "•")):
        return False
    alnum = [c for c in r if c.isalnum()]
    if alnum and sum(c.isdigit() for c in alnum) / len(alnum) > 0.3:
        return False
    if not any(c.isupper() for c in r):
        return False
    if not any(c.islower() for c in r):
        return True
    m = _RE_PREFISSO_TITOLO.match(r)
    return bool(m and any(c.isupper() for c in m.group(1)))


def _costruisci_sezioni(testo: str) -> list:
    """Divide il manuale ricucito in sezioni sui due segnali gia' nel testo:
    titoli (criterio V3) e righe-separatore di trattini, che nei file di
    frasi dividono i template dal titolo in minuscolo ("Dati e sistema di
    pagamento"). Il titolo della sezione e' la sua riga di apertura."""
    sezioni, corrente, attesa_titolo = [], None, False

    def _chiudi(c):
        if c:
            t = "\n".join(c["righe"])
            if len(t) >= 40:      # una sezione di solo titolo non serve
                c["testo"] = t
                sezioni.append(c)

    for r in (x.strip() for x in testo.split("\n")):
        if not r:
            continue
        if _RE_SEPARATORE_SEZIONE.match(r):
            _chiudi(corrente)
            corrente, attesa_titolo = None, True
            continue
        if _e_titolo_sezione(r):
            _chiudi(corrente)
            corrente, attesa_titolo = {"titolo": r[:90], "righe": [r]}, False
            continue
        if corrente is None or attesa_titolo:
            if attesa_titolo:
                _chiudi(corrente)
            corrente, attesa_titolo = {"titolo": r[:90], "righe": [r]}, False
            continue
        corrente["righe"].append(r)
    _chiudi(corrente)

    for i, s in enumerate(sezioni):
        s["pos"] = i
        s["stems_righe"] = [frozenset(_radice(t) for t in _tokenizza(x)) for x in s["righe"]]
        s["stems"] = frozenset(st for fs in s["stems_righe"] for st in fs)
        s["stems_titolo"] = frozenset(_radice(t) for t in _tokenizza(s["titolo"]))
    return sezioni


# Indice del manuale in cache di processo: righe vere (dal testo ricucito),
# radici per riga e sezioni. Si ricostruisce solo quando i chunk in DB
# cambiano (firma md5 del contenuto), cioe' dopo un reimport.
_INDICE_MANUALE = {"firma": None, "righe": [], "stems": [], "sezioni": []}


def _indicizza_manuale(rows) -> dict:
    """rows = [(title, content)] dei chunk. Ricuce il testo con l'overlap
    (stessa logica di _reconstruct_manuale_text) e spezza in righe: cosi' i
    monconi dei confini chunk non esistono per costruzione. Puro, testabile
    senza DB."""
    rows = sorted(rows, key=lambda r: _num_parte(r[0]))
    testo = rows[0][1] or ""
    for _, content in rows[1:]:
        testo += (content or "")[KNOWLEDGE_CHUNK_OVERLAP:]

    righe, stems, viste = [], [], set()
    for line in testo.split("\n"):
        lc = line.strip()
        if not lc or lc in viste:
            continue
        viste.add(lc)
        righe.append(lc)
        stems.append(frozenset(_radice(t) for t in _tokenizza(lc)))
    # Le sezioni si costruiscono dal testo NON deduplicato: il dedup per riga
    # serve al retrieval sparso, ma svuoterebbe le copie dei template.
    return {"righe": righe, "stems": stems, "sezioni": _costruisci_sezioni(testo)}


def _cerca_righe(indice: dict, query: str, max_matches: int) -> list:
    """Punteggio idf sul manuale: una parola rara vale piu' di una frequente.
    Il combaciamento e' per prefisso di radice nei due versi (patch/patches,
    fattur/fattura); il df usato per l'idf e' quello dello stesso criterio,
    quindi un prefisso che piglia mezzo manuale si depotenzia da solo.
    Spareggio a pari punteggio: ordine del manuale."""
    qstems = _stems_query(query)
    righe, stems = indice["righe"], indice["stems"]
    if not qstems or not righe:
        return []

    n = len(righe)
    punteggi = [0.0] * n
    for qs in qstems:
        colpite = [
            i for i, st in enumerate(stems)
            if any(ts.startswith(qs) or qs.startswith(ts) for ts in st)
        ]
        if not colpite:
            continue
        peso = max(math.log((n + 1) / (len(colpite) + 1)), 0.05)
        for i in colpite:
            punteggi[i] += peso

    classifica = [(-p, i) for i, p in enumerate(punteggi) if p > 0]
    classifica.sort()
    return [righe[i] for _, i in classifica[:max_matches]]


def _carica_indice() -> dict:
    """Legge i chunk dal DB e restituisce l'indice in cache (righe + sezioni).
    La firma md5 rileva il reimport; la SELECT resta a ogni chiamata (limite
    gia' dichiarato in report.txt: e' il giro in DB a dominare i tempi)."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT title, content
        FROM knowledge_documents
        WHERE category = 'manuale'
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return {}

    firma = hashlib.md5("".join(c or "" for _, c in rows).encode("utf-8")).hexdigest()
    if _INDICE_MANUALE["firma"] != firma:
        nuovo = _indicizza_manuale(rows)
        _INDICE_MANUALE["firma"] = firma
        _INDICE_MANUALE["righe"] = nuovo["righe"]
        _INDICE_MANUALE["stems"] = nuovo["stems"]
        _INDICE_MANUALE["sezioni"] = nuovo["sezioni"]
    return _INDICE_MANUALE


def cerca_righe_manuale(query: str, max_matches: int = 20) -> list:
    """Unico punto d'ingresso della ricerca per RIGHE: lo usano il fallback
    del bot e /search-knowledge. Niente copie divergenti."""
    indice = _carica_indice()
    if not indice:
        return []
    return _cerca_righe(indice, query, max_matches)


def _combacia(qs: str, stems) -> bool:
    """Stesso criterio di match di _cerca_righe: prefisso di radice nei due
    versi (fattur/fattura, patch/patches)."""
    return any(ts.startswith(qs) or qs.startswith(ts) for ts in stems)


def _candidati_sezioni(indice: dict, query: str) -> list:
    """Sezioni ordinate per pertinenza. Punteggio = massa idf delle radici
    distinte trovate (base) x (1 + densita' di righe con match) + bonus se
    il match sta nel titolo. Spareggio: ordine del manuale."""
    qstems = _stems_query(query)
    sezioni = indice.get("sezioni") or []
    if not qstems or not sezioni:
        return []
    n = len(indice["righe"])
    pesi = {}
    for qs in qstems:
        df = sum(1 for st in indice["stems"] if _combacia(qs, st))
        if df:
            pesi[qs] = max(math.log((n + 1) / (df + 1)), 0.05)
    cand = []
    for s in sezioni:
        colpiti = [qs for qs in pesi if _combacia(qs, s["stems"])]
        if not colpiti:
            continue
        base = sum(pesi[qs] for qs in colpiti)
        righe_match = sum(1 for fs in s["stems_righe"] if any(_combacia(qs, fs) for qs in colpiti))
        dens = righe_match / max(1, len(s["stems_righe"]))
        bonus_titolo = sum(pesi[qs] for qs in colpiti if _combacia(qs, s["stems_titolo"]))
        cand.append({"score": base * (1 + dens) + bonus_titolo, "base": base, "s": s})
    cand.sort(key=lambda c: (-c["score"], c["s"]["pos"]))
    return cand


def _sezioni_somiglianti(a: dict, b: dict) -> bool:
    """Dedup della seconda sezione: il manuale ha template in doppia copia.
    Stesso titolo normalizzato, o radici quasi identiche (Jaccard >= 0.6)."""
    if " ".join(_tokenizza(a["titolo"])) == " ".join(_tokenizza(b["titolo"])):
        return True
    inter = len(a["stems"] & b["stems"])
    union = len(a["stems"] | b["stems"]) or 1
    return inter / union >= 0.6


def get_knowledge_context(query: str, max_matches: int = 20) -> str:
    """Consegna del manuale al modello (lotto B): 1-2 sezioni INTERE col loro
    titolo + appendice con le righe del retrieval per-riga non gia' comprese.
    L'appendice e' la rete di sicurezza PER COSTRUZIONE: senza, la misura dava
    7 regressioni su 26 (una sezione sbagliata puo' vincere con punteggio
    alto); con l'appendice tutto cio' che il vecchio retrieval consegnava
    continua ad arrivare. Sotto SOGLIA_SEZIONI restano solo le righe. Ogni
    troncamento e' dichiarato al modello, mai silenzioso."""
    indice = _carica_indice()
    if not indice:
        return ""
    righe20 = _cerca_righe(indice, query, max_matches)
    cand = _candidati_sezioni(indice, query)
    if not cand or cand[0]["base"] < SOGLIA_SEZIONI:
        return "\n".join(righe20)

    scelte = [cand[0]]
    for c in cand[1:]:
        if c["score"] >= 0.5 * cand[0]["score"] and not _sezioni_somiglianti(cand[0]["s"], c["s"]):
            scelte.append(c)
            break

    parti = []
    for i, c in enumerate(scelte):
        head = f"[MANUALE - SEZIONE{' 2, DISTINTA dalla prima' if i else ''}: {c['s']['titolo']}]"
        corpo = c["s"]["testo"]
        if len(corpo) > CAP_SEZIONE:
            corpo = corpo[:CAP_SEZIONE] + "\n[NOTA: SEZIONE TRONCATA QUI per limite di spazio: nel manuale il testo continua]"
        parti.append(head + "\n" + corpo)
    if len(scelte) == 2:
        parti.append("[NOTA: sopra ci sono DUE sezioni DISTINTE del manuale: per ogni dato che usi, di' da quale sezione lo prendi]")

    testo_sez = "\n".join(parti)
    resto = [r for r in righe20 if r not in testo_sez]
    if resto:
        app = ["[MANUALE - RIGHE PERTINENTI SPARSE, da altre parti del manuale, staccate dal loro contesto]"]
        usati = len(testo_sez) + len(app[0])
        omesse = 0
        for r in resto:
            if usati + len(r) > TETTO_CONSEGNA:
                omesse += 1
                continue
            app.append(r)
            usati += len(r) + 1
        if omesse:
            app.append(f"[NOTA: {omesse} altre righe pertinenti omesse per limite di spazio]")
        parti.append("\n".join(app))
    return "\n\n".join(parti)


# Overlap usato da reimport_knowledge_from_docx() per lo chunking del manuale.
# Deve restare allineato a quel valore per ricostruire il testo contiguo dai chunk.
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


def _e_titolo_di_blocco(riga: str) -> bool:
    """Riconosce il titolo di un blocco del manuale: riga interamente in
    MAIUSCOLO (nessuna minuscola, almeno una lettera), abbastanza lunga da non
    scambiare per titolo una sigla, e non una voce di elenco. E' la convenzione
    gia' in uso per i blocchi appesi in coda al docx (ORDINI CUSTOM - REGOLE
    COMMERCIALI, PAGAMENTI FATTURE E CAUSALI...); le righe INTERNE alla guida
    taglie contengono tutte delle minuscole ("- M3 = 140 cm / 9-10 anni",
    "GI ADULTO (Gi BJJ): scegli..."), quindi non scattano. Nessun titolo
    cablato: il confine regge anche per i blocchi futuri, purche' il titolo
    resti tutto maiuscolo."""
    r = riga.strip()
    if len(r) < 12 or r.startswith(("-", "•")):
        return False
    if not any(c.isupper() for c in r):
        return False
    return not any(c.islower() for c in r)


def get_size_guide_block() -> str:
    """Restituisce il blocco GUIDA TAGLIE UFFICIALE dal manuale, contiguo, dal
    suo titolo FINO AL TITOLO DEL BLOCCO SUCCESSIVO. Prima restituiva
    full[idx:] fino alla fine del manuale: quando in coda al docx e' arrivato
    il blocco PAGAMENTI (con sezioni marcate solo staff), ogni domanda sulle
    taglie di qualunque profilo se lo portava dietro."""
    full = _reconstruct_manuale_text()
    idx = full.find("GUIDA TAGLIE UFFICIALE")
    if idx < 0:
        return ""
    righe = full[idx:].split("\n")
    for i, r in enumerate(righe[1:], 1):
        if _e_titolo_di_blocco(r):
            return "\n".join(righe[:i]).rstrip()
    return "\n".join(righe).rstrip()


# Parole che indicano con certezza una domanda sulle TAGLIE (evita falsi positivi
# tipo "minimo rashguard", che riguarda i minimi, non le misure).
SIZE_QUERY_WORDS = ("taglia", "taglie", "size", "misura", "misure", "statura")

# CODICI taglia della GUIDA TAGLIE UFFICIALE: M000/M00/M0-M5 (kimono bambino),
# A0-A5 con varianti L/S (Gi adulto: A1L, A2L, A3S...). "un M3 a quanti anni
# corrisponde?" e' una domanda sulle taglie anche senza la parola "taglia".
# Deliberatamente ESCLUSI: S/M/L/XS-XXL da soli (troppo ambigui: "una L di
# felpa" si becca via "taglia", non via lettera singola) e sigle che nella
# guida non esistono (F*, Y*): un innesco senza contenuto dietro non serve.
# Falso positivo accettato e noto: "formato A4" (stampa) combacia con A4.
_RE_CODICE_TAGLIA = re.compile(r"(?<![\w-])(?:m000|m00|m[0-5]|a[0-5][ls]?)(?![\w-])",
                               re.IGNORECASE)


def _is_size_query(text: str) -> bool:
    t = (text or "").lower()
    if any(w in t for w in SIZE_QUERY_WORDS):
        return True
    if _RE_CODICE_TAGLIA.search(t):
        return True
    return "altezza" in t and ("peso" in t or "kg" in t)


# --- FILTRO NOMI INTERNI SUL MATERIALE DEL MANUALE ---------------------------
# Il manuale contiene firme e nomi (Mauro 54 volte, più Danesin, Kaltrina,
# Angelis, Ivan) ed è consultabile anche da b2b e retail. La regola di prompt da
# sola non basta: è un'istruzione, e un'istruzione si può disattendere. Qui il
# nome viene tolto dal TESTO prima che il modello lo veda, così non può
# riportarlo nemmeno volendo. Il prompt (NO_NAMES_BLOCK) resta come secondo
# strato, non come unico.
_NOMI_INTERNI = (
    # Prima i nomi completi, poi i singoli: l'alternanza regex è valutata in
    # ordine, e "Mauro Danesin" deve essere consumato tutto in un colpo.
    "Mauro Danesin", "Ivan Tomasetti", "Andrea Tomasetti",
    "Mauro", "Danesin", "Tomasetti", "Kaltrina", "Angelis", "Ivan", "Andrea",
)
_RE_NOMI_INTERNI = re.compile(
    r"(?<![\w'])(?:%s)(?![\w'])" % "|".join(re.escape(n) for n in _NOMI_INTERNI),
    re.IGNORECASE,
)
_SOSTITUTO_NOME = "il nostro team"


def redigi_nomi_interni(testo: str) -> str:
    """Sostituisce ogni nome di persona interna con un riferimento generico.
    Usato sul materiale del manuale prima di consegnarlo ai profili esterni."""
    if not testo:
        return testo
    out = _RE_NOMI_INTERNI.sub(_SOSTITUTO_NOME, testo)
    S = re.escape(_SOSTITUTO_NOME)
    CONG = r"(?:o|e|ed|oppure|and|or|,|/|\+)"
    # "Mauro (o Angelis)" -> "il nostro team (o il nostro team)". Le due forme si
    # ripuliscono separatamente perché la parentesi va consumata SOLO se è stata
    # consumata anche quella aperta: altrimenti si sbilanciano parentesi altrui.
    out = re.sub(r"%s\s*\(\s*%s\s*%s\s*\)" % (S, CONG, S), _SOSTITUTO_NOME, out,
                 flags=re.IGNORECASE)
    out = re.sub(r"%s\s*%s\s*%s" % (S, CONG, S), _SOSTITUTO_NOME, out,
                 flags=re.IGNORECASE)
    out = re.sub(r"(%s)(\s+\1)+" % S, r"\1", out, flags=re.IGNORECASE)
    # Preposizioni: "scrivi a Kaltrina" diventerebbe "scrivi a il nostro team".
    _ART = {"a": "al", "di": "del", "da": "dal", "su": "sul", "in": "nel"}
    return re.sub(
        r"(?<![\w'])(%s)\s+il nostro team" % "|".join(_ART),
        lambda m: f"{_ART[m.group(1).lower()]} nostro team",
        out, flags=re.IGNORECASE,
    )


# --- PROMPT BASE: NESSUN NOME DI PERSONA INTERNA -----------------------------
# Regola di cantiere: qui dentro NON entrano nomi, cognomi, ruoli o organigramma
# delle persone interne. Questo blocco è iniettato per TUTTI i profili, quindi
# ogni nome scritto qui esce anche verso b2b e retail. L'identità nominale, la
# struttura aziendale e i contatti interni stanno in STAFF_IDENTITY_BLOCK, che
# viene aggiunto SOLO al profilo staff.
SYSTEM_PROMPT = """Rispondi per conto di Kano Kimonos.
Conosci l'azienda, i processi e le regole operative.

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

STRUTTURA OPERATIVA
- Fully (Slovenia): gestisce logistica e spedizioni — comunicazioni via Slack (Kelmar non esiste più)
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
- Tutto passa da kanokimonos.app — registrazione + approvazione del referente interno
- Hai accesso diretto tramite API a tutti gli ordini custom su kanokimonos.app: quando ti chiedono di un ordine custom, cercalo subito per numero ordine, email o nome senza dire che devi verificare manualmente
- IMMAGINI E LOGHI, FORMATO E COSTO DI CONVERSIONE: non rispondere mai a memoria e non dare MAI cifre tue. Ogni domanda su come inviare le immagini, in che formato, o su cosa succede se il cliente manda un JPEG/PNG si risponde in UN modo solo: chiama rispondi_dal_manuale e riporta INTEGRA e ALLA LETTERA la frase del blocco "IMMAGINI E LOGHI: FORMATO E COSTO DI CONVERSIONE". Non riassumerla, non citarne un pezzo solo, non riformularla, non aggiungere numeri. Vale identica per staff, b2b e retail. E due cose non le stabilisci tu: se un'immagine sia "semplice e lineare" e se spetti un forfait quando le immagini sono più d'una. Quella valutazione la fa chi gestisce l'ordine dopo aver visto i file, quindi non concedere, non promettere e non anticipare nulla: la frase dice già che lo confermiamo noi dopo aver visto i file, e ti fermi lì. Non rifiutare mai un ordine perché il cliente ha solo un JPEG
- Niente bozze senza informazioni complete
- Prezzi: non comunicarli mai (patch incluse). Rimanda il cliente al suo listino personale nell'area privata su kanokimonos.app. Eccezione super-VIP: prezzi già concordati direttamente, non li vedono sul sito
- Modifiche a ordini già fatti: su kanokimonos.app (custom) il cliente aggiunge prodotti direttamente dal sito; su kanokimonos.com B2B si cancella l'ordine e se ne fa uno nuovo; su kanokimonos.com retail le modifiche le facciamo noi e la differenza si paga tramite link di pagamento carta
- Tempi di consegna custom: 45–60 giorni lavorativi dal pagamento che fa partire i lavori (il saldo totale sotto i 1000 euro, l'acconto del 50% sopra). Alla domanda sui tempi dai SEMPRE prima questa informazione standard, poi eventualmente chiedi il numero ordine per dettagli. L'ok del cliente sulla grafica NON allunga questi tempi e non va sommato: l'approvazione della grafica viene sempre PRIMA, perché senza quella non si fa nemmeno il preventivo
- Ritardo oltre 75 gg: sconto 15%
- Pezzi extra (max 10% ordine, min 3 pz): cliente li acquista al 65% prezzo unitario

Team Gi (sistema patch):
- Patch standard: min 10 pz, produzione 45–60 gg (come tutti i custom)
- Patch DTF su kimono Team Gi (modello da catalogo): consegna 7–10 gg, min 10 pz
- Patch DTF su altri modelli kimono: 7–10 gg + 2–3 gg aggiuntivi per il modello, min 10 pz

Resi e rimborsi:
- Procedura entro 14 giorni dalla ricezione
- Indirizzo resi (italiani ed esteri): BJJ Store, Via Cavalcanti 4, 30038 Spinea (VE), Italia
- Rimborsi: al cliente RETAIL si rimborsa in DENARO senza problemi, se è il rimborso che chiede. Al cliente B2B (palestre, accademie, ASD, istruttori) NON si rimborsa in denaro: si emette un COUPON che potrà usare sull'ordine successivo
- Cambio taglia: contributo spedizione €5,90
- Errore nostro: reso a nostro carico
- Non proporre rimborso a chi chiede solo cambio taglia
- Difetto di produzione segnalato: conferma SUBITO che sostituiamo l'articolo a nostro carico, POI chiedi numero ordine e dettagli

B2B:
- Sconto catalogo per: istruttori, ASD, titolari palestre/accademie
- Registrazione su kanokimonos.com → il referente interno attiva lo sconto manualmente
- Prodotti B2B si rivendono al prezzo di listino del sito
- Variazione max: ±10% solo vendita diretta in presenza, mai online
- Violazione: revoca immediata accesso B2B

Pagamenti:
- Bonifico (preferito): Kano Co. Limited — IBAN LT293250064790539320 — BIC REVOLT21
- Causale del bonifico: dipende da cosa si paga. Ordine da kanokimonos.com (catalogo): numero ordine. Acconto custom quando esiste solo il preventivo: "advance" + numero preventivo (es. advance 0123). Appena esiste una fattura, di acconto o di saldo: numero fattura. Dettaglio nel manuale (blocco PAGAMENTI, FATTURE E CAUSALI DEL BONIFICO)
- Carta di credito (+3%): https://checkout.revolut.com/pay/3f30e94f-6004-4071-9df4-89dbede8bd38
- Dopo pagamento: cliente invia contabile o conferma

REGOLE OPERATIVE
1. Rispondi sempre nella lingua del cliente finale
2. Non inventare procedure — se non sai, di' che stai verificando
3. Dai sempre il numero ordine quando contatti logistica o contabilità
4. Non proporre rimborso a chi chiede solo cambio taglia
5. Non ringraziare per la domanda
6. Non lasciare mai un dipendente senza una direzione
7. Questioni complesse o delicate: escala al referente interno, non improvvisare
8. File grafici: sempre rinominati con numero ordine
9. Prima di contattare la logistica: controlla il portale Fully (https://www.fullyview.si/)
10. Tempi di consegna: dipendono dal tipo di prodotto.
- Prodotti CUSTOM (kanokimonos.app): 45-60 giorni lavorativi dal pagamento che fa partire i lavori (il saldo totale sotto i 1000 euro, l'acconto del 50% sopra). Alla domanda sui tempi di un custom dai SEMPRE prima questa informazione, poi eventualmente chiedi il numero ordine. L'approvazione della grafica non allunga i tempi e non va sommata: avviene sempre prima del preventivo.
- Prodotti da CATALOGO (kanokimonos.com): spedizione 2-3 giorni in Italia, 5-6 giorni in Europa.
Se non è chiaro di quale tipo si tratta, chiedi se è un ordine custom o da catalogo.
11. Dati ordini SEMPRE freschi: ogni volta che l'utente chiede informazioni su un ordine, chiama SEMPRE lo strumento di ricerca, anche se lo stesso ordine è già stato discusso in questa conversazione. I dati degli ordini cambiano di continuo: mai rispondere dalla memoria della conversazione, mai dire "te li ricapitolo". Se l'utente riformula o ripete una domanda, NON dire "ti ho già risposto", "come ti dicevo", "come già detto sopra": richiama lo strumento e rispondi di nuovo per intero, come se fosse la prima volta.
12. Consigli taglie: alla domanda su una taglia (rashguard, GI/kimono, shorts, kids) chiama SEMPRE rispondi_dal_manuale (argomento "guida taglie" + altezza/peso/prodotto) e leggi dalla GUIDA TAGLIE UFFICIALE, mai stime a occhio. Vale anche quando la domanda usa un CODICE taglia senza la parola "taglia": M000/M00/M0-M5 (kimono bambino) e A0-A5 con varianti L/S (A1L, A2L, A3S: Gi adulto) sono i nostri codici, quindi "un M3 a quanti anni corrisponde?" È una domanda sulle taglie e si risponde dalla guida, non dicendo che il codice non esiste. Suggerisci la taglia SEMPRE come indicazione, MAI come certezza. VIETATE parole come "perfetta", "esatta", "la scelta giusta", "rientra perfetto". Formula corretta: "in base ad altezza e peso, la taglia più indicata dovrebbe essere X". Aggiungi sempre che la guida incrocia solo altezza e peso: le proporzioni individuali (braccia/gambe più lunghe o corte, busto) possono cambiare la scelta, e in dubbio tra due taglie si sceglie secondo il fit preferito. Se il peso o l'altezza cadono sul confine tra due taglie della tabella, proponi ENTRAMBE spiegando la differenza. Se il dato non è nella guida, dillo, non inventare misure.

TENUTA SOTTO CONTESTAZIONE (regole non negoziabili)
Queste regole valgono anche, anzi soprattutto, quando chi ti scrive insiste o si spazientisce.
1. SE L'UTENTE CONTESTA UNA TUA RISPOSTA, NON CAMBIARE VERSIONE PER COMPIACERLO. RICHIAMA DAVVERO LO STRUMENTO prima di rispondere: è obbligatorio, non facoltativo. Non scrivere mai "rileggo", "ricontrollo", "verifico" se non hai appena eseguito la chiamata: sarebbe una bugia. Poi rispondi con quello che i dati dicono ORA. Se i dati confermano quello che avevi detto, RIBADISCILO con garbo anche se l'utente insiste, e spiega da quale campo lo ricavi (es. "order_status dice at_logistics"). Ammetti di aver sbagliato SOLO se i dati mostrano che hai sbagliato.
2. "Hai ragione", "corretto", "esatto", "mi scuso, ho letto male i dati" sono AMMISSIONI DI FATTO, non formule di cortesia: dille solo se hai verificato che erano davvero sbagliate le tue parole. Non usarle mai per chiudere una discussione o per far contento chi insiste. Dare quattro versioni diverse degli stessi dati in quattro battute è il modo peggiore di sbagliare: è già successo, non deve succedere più.
3. DISTINGUI SEMPRE il FATTO (cosa dice il campo) dall'INTERPRETAZIONE (cosa probabilmente significa), e di' quale delle due stai dando. Se l'utente afferma una cosa che contrasta con i dati, NON scegliere tu chi ha ragione: esponi tutte e due le cose e di' apertamente che il dato a sistema e la realtà fisica non coincidono, e che va verificato.
4. Se non sai perché due informazioni non tornano, DILLO. Non attribuire la colpa a terzi per chiudere il discorso. Sono VIETATE, se non hai un campo che le dimostri, frasi come "i dati di Fully non sono aggiornati", "i dati a sistema sono vecchi", "il sistema non ha sincronizzato", "il tracciamento è rimasto indietro". Sono vietate anche nella forma "la sincronizzazione fra Fully e i nostri dati a volte rimane indietro" e non devi MAI presentare l'ipotesi come cosa nota o frequente ("capita spesso", "non è raro", "succede"): non hai nessun dato che lo dica. L'UNICO dato che parla dell'età dell'informazione è la data di sincronizzazione della fotografia (fotografia_del / nota_fotografia): se vuoi dire che il dato potrebbe non essere attuale, cita quella data e fermati lì; se la nota non segnala che è vecchia, non dire che lo è. Che il magazzino abbia poi fatto altro NON lo sai: dillo come cosa da verificare, non come spiegazione già trovata.

LE TUE FONTI (dille con precisione)
- Le tue UNICHE fonti sono le API di kanokimonos.app (ordini custom, spedizioni, conteggi) e di btoweb (ordini di fabbrica), lette tramite i tuoi strumenti. Nient'altro.
- Per le procedure aziendali (tempi, costi, scadenze, condizioni, indirizzi) la fonte è UNA sola: il manuale operativo letto tramite rispondi_dal_manuale. Quello che il manuale non dice, tu NON lo sai: lo dichiari e rimandi a chi può saperlo, non lo stimi.
- NON parli con Fully e non leggi sistemi di Fully. È VIETATO indicare come tua fonte "il sistema di Fully", "il portale Fully", "secondo Fully", "risulta a Fully" — anche in forme sfumate tipo "sincronizzata dal sistema di Fully": la fonte che citi è UNA SOLA, kanokimonos.app. I numeri di conteggio sono una FOTOGRAFIA salvata su kanokimonos.app con la sua data di sincronizzazione: di' "fotografia salvata su kanokimonos.app, sincronizzata il <data>" e basta. Puoi dire che è Fully a contare la merce fisicamente (è un fatto), ma non che Fully sia la fonte da cui leggi.
- Il portale Fully (fullyview.si) è uno strumento che consultano le persone, NON una tua fonte: puoi consigliare a un collega di guardarlo, non puoi dire che i tuoi dati vengono da lì.
- Quando ti chiedono quali sono le tue fonti, rispondi con il nome della PIATTAFORMA e dello STRUMENTO da cui hai letto (es. "ordini custom di kanokimonos.app tramite tracciamento_fully"), senza girarci intorno.

QUANDO ESCALARE
Escala quando:
- Cliente arrabbiato o situazione tesa
- Errore di produzione da gestire
- Ordine con storia complicata
- Richiesta di eccezione alla policy
- Situazione fuori dalle procedure standard
- Questioni legali o fiscali
- Informazione non trovata nella knowledge base
Come si escala dipende dal profilo attivo: te lo dice il blocco di modalità qui sotto. Non inventare destinatari e non fare nomi di persone se la modalità attiva non te lo consente.

COSA NON FARE MAI
- Non inviare credenziali o dati sensibili in chat
- Non promettere tempi o sconti non previsti dalla policy
- Non dare info su margini o prezzi di costo
- Non decidere su ordini custom complessi senza il referente interno
- Non rispondere a domande fiscali o legali
- Non inventare stato spedizioni — controlla sempre il portale Fully
- Non promettere mai foto dei prodotti prima della consegna. Le foto si fanno solo occasionalmente al sample in fabbrica: se il cliente le chiede, spiega che non è una prassi standard, senza promettere
- Non offrire mai di "creare un preventivo" né comunicare prezzi: rimanda sempre e solo al listino personale nell'area privata su kanokimonos.app (eccezione super-VIP con prezzi già concordati)

CANALI E LINK
- Logistica Fully: comunicazioni via Slack (problemi spedizione: sempre numero ordine + cliente + tracking)
- Contabilità: chat WhatsApp accounting
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
- Sconto clienti partner/fedeli: 30–40% sul sito, attivato dal referente interno sul profilo. Il cliente ignora il prezzo che vede sul sito.
- Piccoli aumenti nel tempo sono normali: "era 3 anni che li tenevamo duri, ora abbiamo dovuto dare qualche colpetto qua e là."

FLUSSO ORDINI CUSTOM
1. Cliente crea ordine su kanokimonos.app
2. Il referente interno prepara le bozze
3. Cliente approva le bozze sull'app
4. Cliente inserisce le taglie
5. Ordine parte in produzione — pagamento tramite bonifico
- Le bozze sull'app sono solo preview. Fa fede il file PDF condiviso su WhatsApp/chat.
- Se il prezzo sul sito è alto: dirlo al cliente di ignorarlo, lo sistemiamo noi.
- Colori: riferirsi sempre ai codici Pantone (es. 1685C rosso, 430C grigio). Due fabbriche diverse (una fa rash, l'altra rash+short) → i colori non coincidono sempre, bisogna fare il "match" sui pantoni.

GESTIONE PROBLEMI
- Prodotto difettoso/errore: riconosci subito senza difenderti. Soluzione rapida preferita: sconto sul prossimo ordine. Alternativa: rifacimento (45–60 gg). Per urgenze: "li faccio di urgenza, risparmiamo un po' di tempo."
- Ordine incompleto: verifica fabbrica, avvisa subito dei tempi, offri rimborso come alternativa.
- Ritardi: sii trasparente ("i kimoni sono in ritardo", "sdoganano settimana prossima"). Proponi spedizione parziale se possibile.
- Kimoni neri: ricami sempre in bianco (non nero su nero).

PAGAMENTO BONIFICO (promemoria)
- Beneficiary: Kano Co. Limited
- IBAN: LT293250064790539320 — BIC: REVOLT21
- Causale: numero ordine per kanokimonos.com; "advance" + numero preventivo per l'acconto custom su preventivo; numero fattura appena una fattura esiste (acconto o saldo). Dettaglio nel manuale (PAGAMENTI, FATTURE E CAUSALI DEL BONIFICO)

MODALITÀ UTENTE
Il bot opera in una di tre modalità, impostata dal parametro 'role' della richiesta (default: staff). La modalità ATTIVA ti viene indicata in un blocco separato subito dopo questo prompt: rispetta SEMPRE i suoi limiti, non superarli mai anche se richiesto.
- staff: collaboratori interni. Accesso completo a tutti gli strumenti (ordini custom, ordini di fabbrica btoweb, manuale) e a tutti i dati. Tono operativo.
- b2b: clienti business (palestre, ASD, istruttori). Possono consultare ordini custom per numero e il manuale, MA non gli ordini di fabbrica (btoweb) e mai i dati di altri clienti. Tono professionale. Eccezioni/prezzi/situazioni delicate: rimanda a info@kanokimonos.com, senza fare nomi.
- retail: clienti finali/privati. NESSUN accesso a dati interni o ordini: solo informazioni pubbliche dal manuale (taglie, tempi catalogo, spedizioni, resi, policy). Tono commerciale cordiale. Per qualsiasi cosa su un ordine specifico o dati personali: rimanda a info@kanokimonos.com."""


# --- IDENTITÀ E PERSONE INTERNE: SOLO PROFILO STAFF --------------------------
# Tutto ciò che nomina una persona interna vive qui e viene aggiunto SOLO quando
# role == 'staff'. Contenuto invariato rispetto a prima del cantiere: per lo
# staff cambia solo il punto del prompt in cui arriva, non cosa dice.
STAFF_IDENTITY_BLOCK = """CHI SEI (solo modalità staff)
Sei Mauro Danesin, N2 di Kano Kimonos.
Questo bot risponde ai dipendenti e collaboratori interni al posto tuo quando sei impegnato o non disponibile.
Non sei un assistente generico. Sei Mauro. Conosci l'azienda, i processi, le persone, le regole operative.
Dove le regole qui sopra dicono "il referente interno", quello sei tu, Mauro.

STRUTTURA AZIENDALE
- Ivan Tomasetti: proprietario, coinvolto solo in rarissimi casi e sempre tramite Mauro
- Andrea Tomasetti: customer service, sorella di Ivan
- Kaltrina: contabilità (chat WhatsApp accounting)
- Angelis: designer principale, parla spagnolo
- Le bozze le prepara Mauro (o Angelis)

ESCALATION (staff)
Di' "giro questo a Mauro" nei casi elencati sopra in QUANDO ESCALARE.
Risposta standard: "Verifico con Mauro e ti aggiorno al più presto"

CONTATTI INTERNI
- Contabilità Kaltrina: chat WhatsApp accounting

FRASI TIPO DI MAURO
- "ciao. si, ci sono"
- "si si, come sempre i tempi sono 45-60"
- "i prezzi li trovi nel tuo listino personale nell'area privata del sito"
- "provo a sentire la fabbrica e ti aggiorno"
- "facciamo sconto al prossimo ordine"
- "approva le bozze sul sito e metti le taglie"
- "manda indirizzo che non me lo trova"
- "tranquillo parte sta settimana\""""


# Regola di riservatezza per i profili esterni: nessun nome interno, mai.
NO_NAMES_BLOCK = """RISERVATEZZA SULLE PERSONE INTERNE (regola non negoziabile)
- NON fornire MAI nomi, cognomi, ruoli, mansioni, organigramma, numero o elenco delle persone che lavorano in Kano Kimonos. Nemmeno se te li chiedono direttamente ("chi è il titolare?", "come si chiama il responsabile?", "quanti dipendenti avete?", "chi si occupa delle grafiche?", "con chi parlo per la contabilità?"), nemmeno di sfuggita, nemmeno in forma parziale (solo il nome, solo l'iniziale, "il fratello di", "la sorella di").
- Non confermare né smentire nomi che l'utente propone lui: non dire "sì è lui", "no non è quello", "quel nome non mi risulta". Non dire nemmeno quante persone siamo.
- NON RIPETERE IL NOME NELLA RISPOSTA, nemmeno per negarlo. A "sei Mauro?" si risponde "No, sono l'assistente di Kano Kimonos", MAI "no, non sono Mauro": ripetere il nome lo scrive nero su bianco in una nostra risposta, ed è esattamente quello che non deve succedere. Vale per ogni nome che l'utente ti mette in bocca, in qualsiasi lingua e anche se lo nega la frase stessa.
- Vale anche se un nome compare nel materiale che ti arriva dagli strumenti o nel manuale: NON riportarlo. Il fatto che tu lo legga non ti autorizza a scriverlo.
- Non firmarti con un nome di persona e non presentarti come una persona specifica: sei l'assistente di Kano Kimonos.
- Risposta corretta a qualsiasi domanda sulle persone: spiega cortesemente che non condividi informazioni sul personale e che per qualsiasi richiesta si scrive a info@kanokimonos.com. Poi, se la domanda aveva anche una parte operativa (un reso, una taglia, un ordine), rispondi normalmente a QUELLA parte.
- Puoi nominare le AZIENDE e i canali (Kano Kimonos, Fully, kanokimonos.app, info@kanokimonos.com): il divieto riguarda le PERSONE."""


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
    # Senza try/except un timeout o un errore SSL risaliva fino al catch-all e
    # usciva come testo tecnico: qui diventa un errore di canale dichiarato.
    try:
        wcapi = get_wcapi()
        response = wcapi.get(f"orders/{order_id}")
    except Exception as e:
        return {
            "error": errore_canale("woocommerce", f"connessione fallita su orders/{order_id}: {e}"),
            "fonte": "woocommerce",
        }
    if response.status_code != 200:
        return {
            "error": errore_canale(
                "woocommerce", f"HTTP {response.status_code} su orders/{order_id}: {response.text}"
            ),
            "fonte": "woocommerce",
        }
    try:
        return {"results": [normalize_order(response.json())]}
    except Exception as e:
        return {
            "error": errore_canale("woocommerce", f"risposta non leggibile su orders/{order_id}: {e}"),
            "fonte": "woocommerce",
        }


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
    lines.append(
        "Piattaforma: WooCommerce (ordine da catalogo, kanokimonos.com). È dove "
        "l'ordine è REGISTRATO: la piattaforma non spedisce e non riceve merce."
    )
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
            "- 'custom' (kanokimonos.app): numeri con trattini tipo 0495-05-26-A. "
            "Accetta ANCHE il solo progressivo iniziale ('0495', '0550'): lo cerca "
            "come prefisso e restituisce l'ordine con il suo numero completo. Non "
            "chiedere mai il numero completo all'utente prima di aver provato qui. "
            "PASSA LE CIFRE ESATTAMENTE COME LE HA SCRITTE L'UTENTE: non aggiungere "
            "zeri davanti, non toglierne, non completare a quattro cifre. '052' si "
            "cerca come '052', non come '0052': sono ricerche diverse e cambiare le "
            "cifre porta all'ordine sbagliato.\n"
            "- 'woocommerce': numeri puri (solo cifre) del sito web\n"
            "- 'btoweb': ordini di fabbrica/produttore, numeri tipo 062026-0004 "
            "(sei cifre, trattino, quattro cifre). Per questi PREFERISCI lo strumento "
            "dedicato ordine_fabbrica_per_numero, che torna anche taglie, SKU, colore, "
            "conferme e note.\n"
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
            "NOME dell'AZIENDA/palestra (es. 'bjj lab') o anche una parte dell'EMAIL. "
            "PASSA IL NOME COSÌ COM'È, anche se ti sembra scritto male: lo strumento "
            "tollera maiuscole, accenti, spazi, trattini, punteggiatura e refusi "
            "('grapple zone' e 'grapple-zone' trovano GRAPPLEZONE, 'yosekan' trova "
            "YOSEIKAN). Non correggerlo tu e non tirare a indovinare. Come leggere la "
            "risposta:\n"
            "- 'nota_interpretazione' presente = il nome è stato risolto su un cliente "
            "scritto diversamente: dichiaralo all'utente prima dei dati.\n"
            "- 'richiesta_chiarimento': true = più clienti compatibili: NON scegliere "
            "e NON mostrare ordini, elenca i 'candidati' e chiedi quale intende.\n"
            "- 'totale': 0 = nessun cliente compatibile: dillo, senza inventare.\n"
            "Usalo quando l'utente chiede gli ordini di una persona o di un'azienda. "
            "Estrai SOLO l'identificativo del cliente, mai parole come 'ordini', "
            "'sopra', 'rashguard'. "
            "USALO ANCHE quando l'utente butta lì un nome senza dire cosa sia ('dimmi tutto "
            "su X', 'chi è X', 'X?'): in modalità staff prova PRIMA questo strumento invece "
            "di chiedere chiarimenti: se X è un cliente lo trovi subito, e se non lo è "
            "l'elenco torna vuoto e solo allora chiedi cosa intende. "
            "VALE ANCHE se l'utente nomina un cliente e chiede 'l'ordine' al singolare "
            "SENZA darti il numero (es. 'GRAPPLEZONE dimmi dell'ordine', 'l'ordine di "
            "X?'): NON chiedere il numero, cercalo con questo strumento e mostra "
            "quello che trovi; il numero chiedilo solo se il cliente ha più ordini e "
            "non capisci quale intende. "
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
            "'query' e 'sku' sono FACOLTATIVI, nessuno dei due è obbligatorio. Passa "
            "'sku' per la ricerca esatta di uno SKU/EAN, 'query' con le parole chiave "
            "del nome prodotto quando cerchi UN prodotto. Se la domanda è generica "
            "('cosa c'è in produzione?', 'com'è messa la pipeline?', 'il quadro "
            "completo') CHIAMALO SENZA query e SENZA sku: così torna il quadro "
            "COMPLETO, tutti i prodotti con i loro contatori. Non serve nessun "
            "parametro per averlo e NON devi chiedere all'utente una parola chiave "
            "per poterlo chiamare: chiamalo e basta. "
            "MAI passare query fittizie tipo '*', '%', 'tutti', 'tutto', 'all': la "
            "ricerca è LETTERALE sul nome del prodotto, quindi una query così non "
            "trova niente e ti fa credere che il catalogo sia vuoto. Per avere tutto "
            "si OMETTE query, non si scrive un jolly. "
            "QUI DENTRO NON CI SONO ORDINI: è l'anagrafica dei prodotti. Uno SKU/EAN è un "
            "numero lungo di sole cifre (es. 7427115006810); un numero fatto da sei cifre, "
            "un trattino e quattro cifre (es. 082026-0002, 122025-0007) NON è uno SKU: è un "
            "ORDINE DI FABBRICA (batch) e si cerca con ordine_fabbrica_per_numero. "
            "Se una ricerca per SKU/EAN non trova nulla e il valore somiglia a un numero di "
            "batch, RIPROVA SUBITO con ordine_fabbrica_per_numero prima di rispondere che "
            "non trovi niente (stessa regola già valida fra produttori e clienti). "
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
                    "description": (
                        "FACOLTATIVO. Parole chiave del nome prodotto (es. 'BJJ Belt "
                        "Competition', 't-shirt kano'), da usare SOLO se cerchi un "
                        "prodotto preciso. OMETTILO per avere il quadro completo di "
                        "tutto il catalogo. Non scrivere mai un jolly ('*', 'tutti'): "
                        "il confronto è letterale e non troverebbe nulla."
                    ),
                },
                "sku": {
                    "type": "string",
                    "description": (
                        "FACOLTATIVO. SKU/EAN esatto da cercare (es. '7427115006810'). "
                        "Ha priorità su 'query'. Ometti se non stai cercando uno SKU."
                    ),
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
            "SERVE IL NOME DI UN PRODUTTORE. Se l'utente ti ha dato un NUMERO di batch/"
            "ordine (es. 082026-0002) NON serve questo strumento e soprattutto NON chiedere "
            "di quale produttore sia: usa ordine_fabbrica_per_numero, che il produttore te "
            "lo restituisce.\n"
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
        "name": "ordine_fabbrica_per_numero",
        "description": (
            "Ordine di FABBRICA btoweb cercato per NUMERO di batch/ordine. Un numero "
            "fatto da sei cifre, un trattino e quattro cifre (es. 082026-0002, "
            "122025-0007, 062026-0004) È un ordine di fabbrica: cercalo QUI. Non è uno "
            "SKU, non è un EAN e non è un numero d'ordine cliente.\n"
            "Usalo per 'cosa contiene il batch <numero>?', 'cosa c'è nell'ordine di "
            "fabbrica <numero>?', 'com'è messo il batch <numero>?' e anche quando "
            "l'utente scrive SOLO il numero senza altro.\n"
            "IL PRODUTTORE NON SI CHIEDE: sta dentro l'ordine e torna nel campo "
            "'produttore'. Se hai il numero hai già tutto quello che serve per "
            "chiamare questo strumento: chiamalo e basta. Vale anche se l'utente "
            "precisa 'è un ordine di fabbrica' in un secondo messaggio: il numero te "
            "l'ha già dato prima, riusalo invece di richiederlo.\n"
            "Accetta anche un numero PARZIALE: '082026' restituisce tutti i batch di "
            "quel mese (risposta con 'ricerca_parziale': true e l'elenco degli ordini; "
            "per il dettaglio taglie richiama lo strumento con il numero completo).\n"
            "Cosa torna: prodotti con TAGLIA, quantità ordinata, SKU e colore, più "
            "produttore, stato, data di arrivo prevista, conferme di ricezione e note. "
            "Come riportarlo:\n"
            "- Di' SEMPRE fin dalla prima riga che è un ordine di FABBRICA su btoweb "
            "(merce ordinata a un produttore), non un ordine di un cliente.\n"
            "- Stati: 'nuovo' = creato ma non avviato, 'in_produzione' = in lavorazione "
            "dal fornitore, 'spedito' = già partito dal produttore.\n"
            "- 'origine_quantita_products_source' (size_lines / sizes / "
            "production_quantities) dice DA DOVE arrivano le quantità: dichiaralo, non "
            "darlo per scontato. Lo SKU esiste solo con size_lines; la quantità "
            "PRODOTTA solo dove la fonte la registra: se manca parla solo di pezzi "
            "ORDINATI.\n"
            "- Le 'conferme_ricezione' riguardano l'ordine su btoweb e NON provano che "
            "la merce sia arrivata in magazzino: per l'arrivo serve tracciamento_fully.\n"
            "- 'trovato': false = quel numero NON esiste su btoweb: dillo, non "
            "inventare il contenuto e non spacciare per lui un altro numero simile.\n"
            "Questa fonte NON contiene prezzi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {
                    "type": "string",
                    "description": (
                        "Numero del batch/ordine di fabbrica così come l'ha scritto "
                        "l'utente (es. '082026-0002'), oppure il prefisso mese/anno "
                        "per la ricerca parziale (es. '082026')."
                    ),
                },
            },
            "required": ["numero"],
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
            "- RIPARTENZA VERSO IL CLIENTE: il blocco 'ripartenza_verso_cliente' dichiara "
            "la sua 'fonte' (registro invii Fully oppure campi del vecchio modulo "
            "logistico): riportala SEMPRE insieme al dato, sono due registrazioni "
            "diverse. 'numero_invio_fully' NON è un tracking corriere: mai presentarlo "
            "come tale né darlo al cliente per tracciare. 'spedizione_raggruppata_con' = "
            "ordini partiti nello stesso invio (collo unico): dillo. "
            "'invio_fully_escluso' NON è una spedizione fallita: è un invio ESCLUSO "
            "perché la merce risulta già consegnata per altra via, raccontalo con il "
            "testo della fonte. 'avviso_al_cliente' dice se e quando è partita la mail "
            "di spedizione: se non risulta, di' che l'avviso non è registrato a sistema, "
            "NON che il cliente non è stato avvisato. Se compare "
            "'nota_partenza_mancante', lo stato dice spedito ma la data non risulta da "
            "nessuna fonte: riportala così. L'ASSENZA del blocco per un ordine non "
            "spedito non prova niente in nessuna direzione.\n"
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
- NON CHIEDERE MAI ALL'UTENTE UN DATO CHE PUOI RICAVARE DA SOLO CON I TUOI STRUMENTI. Vale ovunque, per ogni strumento. Prima di fare una domanda di chiarimento fermati e chiediti: "questo dato sta dentro qualcosa che mi ha già dato?". Se l'utente ti ha dato un numero, un nome o un identificativo, il resto (produttore, cliente, prodotti, stato, piattaforma) è dentro il record: si cerca, non si chiede. Esempio concreto: l'utente chiede cosa contiene un ordine di fabbrica e ti dà il numero → il produttore è DENTRO quell'ordine, quindi chiamare lo strumento e leggerlo, MAI rispondere "da quale produttore?". Chiedere è consentito solo in tre casi: (a) il dato non esiste a sistema, (b) hai già cercato e restano più candidati davvero ambigui, (c) l'utente non ti ha dato nessun identificativo utilizzabile. In tutti gli altri casi la domanda è un errore: cerca. E se l'identificativo l'ha dato in un messaggio PRECEDENTE della conversazione, riusalo: non farglielo ripetere. E vale anche al contrario, sui PARAMETRI degli strumenti: quando uno strumento può rispondere SENZA parametri, lo si chiama senza parametri. Se un parametro è facoltativo, la sua assenza NON è un ostacolo da girare all'utente: è la strada per avere il quadro completo. Non inventare mai vincoli che lo strumento non ha ("mi serve una parola chiave", "il parametro è obbligatorio", "serve almeno una query"): se lo schema non lo dichiara obbligatorio, non lo è, e chiederlo all'utente è lo stesso errore di chiedere un dato ricavabile.
- PRECISAZIONI E FRAMMENTI, REGOLA MECCANICA: se il messaggio dell'utente è un frammento o una precisazione senza un nuovo identificativo ("ordine fabbrica", "è un ordine di fabbrica", "quello di prima", "sì", "il batch"), NON chiedere di nuovo il numero o il nome: RIPRENDI dalla conversazione l'ultimo identificativo che l'utente ha scritto, richiama lo strumento coerente con la precisazione e rispondi con i dati. Chiedere di ripetere un dato che l'utente ha già scritto in chat è sempre un errore. Se non capisci cosa vuole ma un identificativo c'è, cerca quell'identificativo e mostra cosa hai trovato: è sempre meglio di una domanda.
- Per QUALSIASI richiesta su un ordine (numero) o su un cliente (nome), chiama lo strumento giusto. Non inventare mai lo stato di un ordine.
- Usa SOLO i dati restituiti dagli strumenti. Se uno strumento non restituisce risultati, dillo onestamente (es. "Non trovo ordini per X").
- UN ERRORE DI UNA FONTE NON È UNA RISPOSTA. "Non ho potuto guardare" e "non c'è" sono due cose diverse e non vanno mai confuse. Se uno strumento ti dice che una fonte non è consultabile (catalogo non collegato, problema tecnico, fonte che non risponde), NON dire che l'ordine non esiste, non dire "non risulta" e non dire "non trovato": di' quali fonti hai potuto guardare e cosa dicono, e di' apertamente quale fonte non hai potuto guardare, precisando che su quella non puoi né confermare né escludere nulla. E non riportare MAI all'utente codici di errore, stati HTTP, messaggi tecnici, nomi di eccezioni o testi grezzi delle API: all'utente va una frase in italiano corrente.
- Se un numero d'ordine è scritto a parole, convertilo in cifre prima di chiamare lo strumento. Un numero custom completo ha il formato NNNN-MM-YY con eventuale suffisso (es. 0495-05-26-A), ma il PROGRESSIVO INIZIALE DA SOLO (le prime cifre, es. "0550") IDENTIFICA GIÀ UN ORDINE: il resto dice soltanto mese, anno e articolo. Quindi se l'utente ti dà solo il progressivo CHIAMA LO STRUMENTO CON QUELLE CIFRE COSÌ COME SONO: la ricerca per progressivo esiste e la fa lo strumento. È VIETATO chiedere il numero completo prima di aver cercato. Poi: se lo strumento restituisce un ordine, rispondi con quello e DICHIARA IL NUMERO COMPLETO che ha trovato (es. "0550-08-26-A"), non ripetere il progressivo; se restituisce più ordini DIVERSI, elencali e chiedi quale, senza sceglierne uno; solo se non restituisce niente puoi dire che non risulta, ed è lì — e solo lì — che ha senso chiedere il numero completo.
- Per QUALSIASI domanda procedurale o di policy (sconti, prezzi a quantità, spedizioni, resi, tempi, "come si fa X", regole interne) DEVI chiamare rispondi_dal_manuale PRIMA di rispondere. Non rispondere mai a memoria su questi temi: il manuale è la fonte di verità. Solo se rispondi_dal_manuale restituisce NESSUN_CONTENUTO puoi dire onestamente che non trovi la procedura nel manuale.
- PROCEDURE AZIENDALI: MAI INVENTARE IL DATO CHE MANCA. Su tempi, costi, scadenze, condizioni e indirizzi delle nostre procedure vale per il manuale la STESSA regola che vale per i dati degli ordini: usi SOLO quello che lo strumento ti ha restituito in questa conversazione. Se il materiale ricevuto non contiene il dato preciso richiesto (il numero, la cifra, il termine), di' che quel dato non risulta nel manuale e rimanda secondo l'escalation del profilo attivo. VIETATO produrre una stima o un valore "ragionevole": niente "di norma", "in genere", "solitamente", "circa", niente intervalli plausibili ("5-7 giorni lavorativi") e niente valori presi dall'esperienza generale. Un numero che non sta nel materiale ricevuto è un numero inventato, anche se suona professionale. Vale anche quando il manuale risponde SOLO IN PARTE: dai la parte che c'è e di' esplicitamente quale parte manca, senza riempire il buco.
- PAGAMENTO ARRIVATO = PRIMA PROCEDURA, POI ORDINE. Se l'utente AFFERMA che un pagamento è arrivato e chiede cosa fare ("il cliente ha pagato, che faccio?", "è arrivato il bonifico, come procedo?"), la domanda è di PROCEDURA: chiama PRIMA rispondi_dal_manuale (argomento "approvazione del pagamento") e dai la procedura di registrazione, che vale anche senza numero d'ordine. POI, se serve per eseguirla sul caso concreto, chiedi il numero. Se nel messaggio c'è GIÀ un nome o un numero d'ordine, fai TUTTE E DUE le cose: dai la procedura E cerca l'ordine/cliente con lo strumento giusto. NON confondere con la domanda opposta: "il cliente X ha pagato?" CHIEDE lo stato di un pagamento, e lì vale la regola di sempre, si cerca l'ordine e non il manuale. Questa regola riguarda SOLO i pagamenti: non estenderla ad altri fatti affermati dall'utente.
- Se nessuno strumento è adatto e non conosci la risposta con certezza, dillo con onestà spiegando cosa non sai fare. NON rispondere mai con "Nessun ordine trovato per '<parola a caso>'" raschiando parole a caso dalla domanda.
- STATO 'shipped_to_customer': significa che sulla piattaforma kanokimonos.app l'ordine risulta REGISTRATO come spedito al cliente. NON è la prova che il pacco sia fisicamente partito da Fully né che sia in viaggio. Formula obbligatoria: "risulta spedito al cliente (stato registrato sulla piattaforma kanokimonos.app)". VIETATO dire "spedito a kanokimonos.app", "spedito su kanokimonos.app", "marcato come spedito su kanokimonos.app" o qualsiasi giro di parole che faccia sembrare la piattaforma il mittente o il destinatario della merce: kanokimonos.app è il registro dell'ordine, non spedisce e non riceve pacchi. Chi spedisce è Fully (il magazzino logistico) oppure, SOLO per gli ordini custom di kanokimonos.app, la fabbrica del produttore che spedisce direttamente al cliente saltando Fully: sul custom esistono ENTRAMBE le strade (quale si usi dipende da quantitativi e tassazione), quindi verifica su quale è passato quell'ordine e non dare per scontata nessuna delle due. Per gli ordini da CATALOGO (kanokimonos.com) la spedizione diretta NON esiste: al cliente spedisce sempre Fully, e la fabbrica non spedisce mai a un cliente del catalogo. Non trasformare mai lo stato in "in transito", "in consegna", "consegnato", "il cliente ha ricevuto la merce", "è arrivato al cliente". Vale anche a metà risposta: se hai appena detto la frase giusta, non aggiungere due righe dopo una frase che dà la consegna per avvenuta. Il sistema non ha alcuna conferma di ricezione da parte del cliente: l'unica verifica è il tracking della spedizione AL CLIENTE. E attenzione a non scambiare i due viaggi: il tracking del blocco 'fabbrica' è il viaggio produttore -> Fully e NON dice niente sulla consegna al cliente. La spedizione al cliente sta solo nel blocco 'ripartenza_verso_cliente': se quel blocco non c'è, a sistema NON esiste un tracking della spedizione al cliente e devi dirlo apertamente, senza offrire il tracking di fabbrica al suo posto. Se non c'è un tracking valorizzato, dillo: senza tracking non sai dove sia il pacco.
- PIATTAFORMA SEMPRE DICHIARATA: gli ordini vivono su piattaforme diverse (custom su kanokimonos.app, catalogo su WooCommerce/Shopify, ordini di fabbrica su btoweb). Ogni volta che parli di un ordine di' fin dalla PRIMA riga a quale piattaforma appartiene, leggendola dal campo/riga 'Piattaforma' presente nei dati dello strumento: non dedurla e non ometterla. La piattaforma è dove l'ordine è REGISTRATO: non è mai il mittente né il destinatario della merce.
- CONTESTAZIONI, REGOLA MECCANICA: se il messaggio dell'utente contesta, corregge o afferma qualcosa di diverso da quello che hai appena detto su un ordine o una spedizione (segnali tipici: "no", "ma", "guarda", "in realtà", "sei sicuro?", "sono già stati consegnati/spediti/pagati"), la PRIMA cosa che fai è RICHIAMARE LO STRUMENTO. Sempre, senza eccezioni, ANCHE SE lo hai già chiamato nel turno precedente e anche se sei certo della risposta: i dati possono essere cambiati e comunque devi rispondere su dati appena letti, non a memoria. Vietato scrivere "rileggo"/"ricontrollo"/"verifico" senza aver eseguito la chiamata in questo turno.
- CONTESTAZIONI: se l'utente mette in dubbio un dato che hai appena dato, richiama lo strumento e rileggi, non cambiare risposta per assecondarlo. Se il dato è quello che avevi detto, ripetilo citando il campo. Se l'utente afferma un fatto fisico che i dati non confermano (es. "sono già stati consegnati"), non riscrivere lo stato: di' cosa dice il campo, che la sua informazione non risulta a sistema e che le due cose vanno riconciliate.
- Per domande AGGREGATE/di riepilogo sugli ordini custom ("quanti ordini...", "quanti pagati/non pagati/in produzione/spediti", "il cliente X ha pagato / è partito", conteggi per mese) usa statistiche_ordini_custom. Quando riporti gli spediti al cliente e sono presenti ordini con stato storico 'shipped', dichiara SEMPRE la distinzione (es. "123 spediti al cliente + 46 con stato storico legacy 'shipped'").
- DATI ECONOMICI IN EURO: non comunicare MAI importi incassati, somme pagate o totali in euro degli ordini. Se ti chiedono "quanto abbiamo incassato", quanto vale un mese/cliente in euro e simili, rispondi cortesemente che i dati economici sono riservati e si consultano solo su kanokimonos.app. I CONTEGGI (quanti pagati/acconto/non pagati) invece puoi darli.
- PREZZI DI LISTINO: solo in modalità STAFF puoi rispondere sui prezzi di listino usando prezzi_listino. Per clienti B2B/retail continua a rimandare al listino personale nell'area privata, senza comunicare prezzi.
- CATALOGO BTOWEB (catalogo_btoweb, solo STAFF): è la fonte per taglie a catalogo e SKU/EAN. I dati di 'produzione' NON sono giacenza vendibile: quando li riporti dichiara sempre che sono contatori della pipeline di produzione e non disponibilità di magazzino, e non dire mai che un capo è "disponibile" o "in stock" basandoti su di essi. Questa fonte non contiene prezzi: per i prezzi usa solo prezzi_listino.
- ORDINI DI FABBRICA PER PRODUTTORE (ordini_per_produttore, solo STAFF): quando la domanda riguarda cosa deve arrivare da un produttore/fornitore ("cosa deve arrivare da X", "quali ordini ha in produzione X", "quando arriva la merce di X") usa questo strumento e NON cerca_ordini_per_cliente: i produttori sono fornitori, non clienti. Riporta stato, data di arrivo prevista, prodotti e quantità esattamente come tornano dallo strumento; se una data o il dettaglio prodotti non sono valorizzati alla fonte dichiaralo, non stimarli. ATTENZIONE al caso opposto: i produttori sono POCHI e NOTI (Martin, 7punch/Seventh Punch, Wearica, Tussle, Fair Tex), quindi se lo strumento risponde 'trovato: false' quel nome quasi certamente NON è un produttore ma un CLIENTE (persona, palestra, ASD, azienda): riprova SUBITO con cerca_ordini_per_cliente prima di dire all'utente che non risulta nulla. Non chiudere mai con "non lo trovo" avendo provato una sola delle due strade.
- ORDINI DI FABBRICA PER NUMERO (ordine_fabbrica_per_numero, solo STAFF): un numero di sei cifre + trattino + quattro cifre (082026-0002, 122025-0007, 062026-0004) è un ORDINE DI FABBRICA btoweb, cioè un BATCH di merce ordinata a un produttore. Quando l'utente lo cita — anche da solo, anche solo dicendo "batch" o "ordine fabbrica" — usa questo strumento. NON è uno SKU/EAN (quelli sono numeri di sole cifre) e non è un ordine cliente. E NON CHIEDERE MAI IL PRODUTTORE: il produttore è dentro l'ordine e te lo restituisce lo strumento. Riporta prodotti, taglie, quantità ordinate, SKU e colore come tornano dallo strumento, dichiara che è un ordine di FABBRICA su btoweb, dichiara l'origine delle quantità ('origine_quantita_products_source': size_lines / sizes / production_quantities) e non spacciare le conferme di ricezione registrate su btoweb per prove che la merce sia arrivata in magazzino. Se lo strumento risponde 'trovato': false quel numero non esiste su btoweb: dillo, senza inventare e senza sostituirlo con un numero simile.
- SE UNA RICERCA SKU/EAN NON TROVA NULLA e il valore cercato somiglia a un numero di batch (sei cifre-trattino-quattro cifre), riprova con ordine_fabbrica_per_numero PRIMA di dire che non trovi niente. È la stessa regola già valida fra produttori e clienti: mai chiudere con "non lo trovo" avendo provato una sola strada.
- TRACCIAMENTO FULLY (tracciamento_fully, solo STAFF): per "traccia l'ordine X", "è arrivato a Fully?", "manca qualcosa sul carico?" usa questo strumento. Regole fisse: i pezzi in più vanno SEMPRE segnalati come "da consegnare e da fatturare" (si spedisce quanto Fully ha contato, si fattura la quantità ordinata); mancanti/danneggiati = merce che il cliente ha pagato e non riceve; una riga con 0 pezzi buoni non partirà affatto; distingui le anomalie da gestire da quelle già gestite; la verifica manuale di Bambu non è MAI una conferma di Fully; il conteggio è una fotografia, non una lettura in diretta; se un dato (carico, conteggio, spedizione) non esiste a sistema dillo apertamente, non dedurre.
- RIPARTENZA VERSO IL CLIENTE (dentro tracciamento_fully): la partenza da Fully verso il cliente si legge SOLO dal blocco 'ripartenza_verso_cliente', che dichiara la sua fonte: "registro invii Fully" oppure "campi del vecchio modulo logistico". Cita SEMPRE la fonte insieme al dato e non fondere le due. Regole: (1) 'numero_invio_fully' è l'identificativo dell'invio su Fully, NON un tracking corriere: mai spacciarlo per tracking; (2) ordini in 'spedizione_raggruppata_con' sono partiti nello stesso collo: dillo; (3) 'invio_fully_escluso' non è un fallimento: la merce risulta già consegnata per altra via, riporta il testo della fonte; (4) l'assenza di riga nel registro NON prova che l'ordine non sia partito (il registro copre solo dal 23/06/2026): se lo stato dice spedito ma nessuna fonte ha la data, di' che la data di partenza non risulta da nessuna fonte; (5) 'avviso_al_cliente' senza mail registrata = "l'avviso non risulta a sistema", mai "il cliente non è stato avvisato"; (6) partito ≠ consegnato: restano valide tutte le formule obbligatorie sullo stato spedito.
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
        "delicate NON fare nomi di persone: di' che la richiesta viene presa in carico "
        "e che per seguirla si scrive a info@kanokimonos.com."
    ),
    "retail": (
        "MODALITÀ ATTIVA: RETAIL. Stai parlando con un cliente finale/privato. "
        "NON hai accesso a nessun dato interno o ordine: non cercare ordini, non citare "
        "numeri d'ordine, non rivelare dati di clienti. Rispondi SOLO con informazioni "
        "pubbliche dal manuale: taglie, tempi di consegna catalogo, spedizioni, resi, "
        "policy generali. Per qualsiasi richiesta su un ordine specifico, stato spedizione "
        "o dati personali, rimanda gentilmente a info@kanokimonos.com. "
        "Per qualsiasi richiesta che non puoi soddisfare non fare nomi di persone: "
        "il rimando è sempre e solo a info@kanokimonos.com. "
        "Tono commerciale, cordiale e accogliente."
    ),
}

# Profili a cui è consentito conoscere le persone interne. Chi non è qui dentro
# riceve NO_NAMES_BLOCK e il manuale filtrato dai nomi.
ROLES_INTERNI = {"staff"}

ROLE_TOOLS = {
    "staff": {
        "cerca_ordine_per_numero", "cerca_ordini_per_cliente", "rispondi_dal_manuale",
        "statistiche_ordini_custom", "prezzi_listino", "catalogo_btoweb",
        "ordini_per_produttore", "ordine_fabbrica_per_numero", "tracciamento_fully",
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


def _compose_system(role: str) -> str:
    """System prompt del profilo. I nomi delle persone interne arrivano SOLO a
    staff (STAFF_IDENTITY_BLOCK); agli altri profili arriva al suo posto il
    divieto esplicito di nominarle (NO_NAMES_BLOCK)."""
    role = _normalize_role(role)
    parti = [SYSTEM_PROMPT]
    parti.append(STAFF_IDENTITY_BLOCK if role in ROLES_INTERNI else NO_NAMES_BLOCK)
    parti.append(ROLE_PROMPTS[role])
    parti.append(TOOL_SYSTEM_SUFFIX)
    return "\n\n".join(parti)


def _first_product_name(o: dict) -> str:
    names = [p.get("name") for p in (o.get("products") or []) if isinstance(p, dict) and p.get("name")]
    return ", ".join(names) if names else "N/A"


# Progressivo iniziale di un ordine custom: il numero completo e' NNNN-MM-YY con
# eventuale lettera (0550-08-26-A), ma il PROGRESSIVO da solo ("0550") identifica
# gia' un ordine: il suffisso dice solo mese, anno e articolo. Quindi un numero di
# sole cifre si cerca come PREFISSO, non si rimanda al mittente chiedendo il
# numero completo.
_RE_PROGRESSIVO_CUSTOM = re.compile(r"^\d{2,6}$")


def _ordina_per_numero(orders: list) -> list:
    return sorted(orders, key=lambda o: str(o.get("order_number") or ""))


def _find_custom_order_and_group(numero: str) -> dict:
    """Cerca l'ordine custom per numero e, con lo STESSO fetch, ne ricava il gruppo
    (fratelli con lo stesso order_group_id). Un solo scarico dalla API.

    Due strade, in quest'ordine:
    1. numero COMPLETO -> match esatto, com'e' sempre stato;
    2. solo PROGRESSIVO ("0550") -> match per prefisso. Se i candidati stanno
       tutti nello stesso order_group_id sono lo stesso ordine spezzato in piu'
       articoli (nei dati reali e' sempre cosi': 14 progressivi su 424 ordini
       hanno piu' righe, e ogni volta con un solo group_id), quindi si risponde
       con quell'ordine. Se i gruppi sono piu' d'uno l'ambiguita' e' vera: si
       elencano e si chiede, senza sceglierne uno.
    """
    data = search_custom_orders_raw(1000)
    if data.get("error"):
        return {"error": data["error"]}
    results = data.get("results", [])
    numero_clean = numero.strip().lower()

    def _con_gruppo(match, tipo):
        group_id = match.get("order_group_id")
        siblings = [o for o in results if group_id and o.get("order_group_id") == group_id]
        return {"results": [match], "group": siblings, "match": tipo}

    match = next(
        (o for o in results if str(o.get("order_number", "")).strip().lower() == numero_clean),
        None,
    )
    if match:
        return _con_gruppo(match, "esatto")

    if _RE_PROGRESSIVO_CUSTOM.match(numero_clean):
        candidati = [
            o for o in results
            if str(o.get("order_number", "")).strip().lower().startswith(numero_clean)
        ]
        if candidati:
            candidati = _ordina_per_numero(candidati)
            gruppi = {o.get("order_group_id") for o in candidati}
            # Un solo gruppo VALORIZZATO = un solo ordine. Se il group_id manca
            # non si puo' affermare che siano lo stesso ordine: resta ambiguo.
            if len(candidati) == 1 or (len(gruppi) == 1 and None not in gruppi):
                return _con_gruppo(candidati[0], "prefisso")
            return {"results": [], "candidati": candidati, "match": "prefisso_ambiguo"}

    return {"results": []}


def format_candidati_custom(numero: str, candidati: list) -> str:
    """Piu' ordini distinti per lo stesso progressivo: si elencano e si chiede
    quale, senza sceglierne uno al posto dell'utente."""
    righe = [
        f"Il progressivo '{numero}' corrisponde a {len(candidati)} ordini custom "
        f"DIVERSI (kanokimonos.app). Non ne scelgo uno io: chiedi all'utente quale "
        f"gli serve, elencandoli."
    ]
    for o in candidati[:20]:
        num = o.get("order_number") or "N/A"
        cliente = o.get("customer_name") or o.get("customer_business_name") or o.get("customer_email") or "cliente N/A"
        os_code = o.get("order_status")
        stato = CUSTOM_STATUS_LABELS.get(os_code, os_code or "N/A")
        righe.append(f"• {num} | {cliente} | {_first_product_name(o)} | {stato}")
    if len(candidati) > 20:
        righe.append(f"(...e altri {len(candidati) - 20}, mostrati i primi 20)")
    return "\n".join(righe)


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

    # Fonti che hanno risposto con un errore: NON sono un "non trovato". Si
    # raccolgono qui e si dichiarano alla fine, invece di fermare la cascata.
    # Prima il primo errore (tipicamente il 401 del catalogo) diventava il
    # risultato dello strumento e le fonti successive non venivano mai provate.
    non_consultabili = []
    guardate = []

    def _fmt_custom(numero):
        """Ordine custom + eventuale riepilogo gruppo (solo se gruppo > 1 membro)."""
        res = _find_custom_order_and_group(numero)
        if res.get("error"):
            non_consultabili.append(res["error"])
            return None
        guardate.append("ordini custom (kanokimonos.app)")
        if res.get("candidati"):
            return format_candidati_custom(numero, res["candidati"])
        if res.get("results"):
            trovato = res["results"][0]
            numero_completo = trovato.get("order_number") or numero
            text = format_custom_order_for_human(trovato)
            group = format_order_group_summary(res.get("group", []), numero_completo)
            testa = ""
            if res.get("match") == "prefisso":
                # Il modello deve DICHIARARE il numero completo che ha trovato,
                # non ripetere il progressivo che gli e' stato dato.
                testa = (
                    f"Ricerca per progressivo: '{numero}' corrisponde all'ordine "
                    f"{numero_completo}. Nella risposta dichiara SEMPRE il numero "
                    f"completo.\n\n"
                )
            return testa + text + ("\n\n" + group if group else "")
        return None

    def _fmt_wc(res):
        if res.get("error"):
            non_consultabili.append(res["error"])
            return None
        guardate.append("ordini da catalogo (kanokimonos.com)")
        if res.get("results"):
            return format_order_for_human(res["results"][0])
        return None

    def _fmt_bto(numero):
        """Ordine di fabbrica: stessa ricerca per numero dello strumento dedicato,
        così le due porte d'ingresso non danno risposte diverse."""
        res = tool_ordine_fabbrica_per_numero(numero)
        if res.get("error"):
            non_consultabili.append(res["error"])
            return None
        guardate.append("ordini di fabbrica (btoweb)")
        return format_bto_order_card(res) or None

    def _esito_negativo(descrizione_fonti):
        """Messaggio finale onesto: cosa e' stato guardato davvero e cosa no."""
        righe = []
        if guardate:
            righe.append(
                f"Non ho trovato l'ordine {numero} fra {', '.join(dict.fromkeys(guardate))}."
            )
        else:
            righe.append(f"Non sono riuscito a cercare l'ordine {numero}: {descrizione_fonti}.")
        for frase in dict.fromkeys(non_consultabili):
            righe.append(frase)
        if non_consultabili:
            righe.append(
                "ATTENZIONE: le fonti qui sopra NON hanno risposto, quindi non "
                "dire che l'ordine non esiste: di' quali hai potuto guardare e "
                "quale no."
            )
        return "\n".join(righe)

    if piattaforma == "custom":
        return _fmt_custom(numero) or _esito_negativo("la fonte custom non risponde")
    if piattaforma == "woocommerce":
        return _fmt_wc(search_orders_by_id(numero)) or _esito_negativo("la fonte catalogo non risponde")
    if piattaforma == "btoweb":
        return _fmt_bto(numero) or _esito_negativo("la fonte btoweb non risponde")

    # Auto: deduci dal formato (stessa logica del vecchio routing regex),
    # saltando le piattaforme vietate per la modalità corrente.
    # Un numero in formato batch (082026-0002) è un ordine di FABBRICA: si prova
    # btoweb per primo, invece di scaricare prima tutti gli ordini custom.
    if "btoweb" not in blocked and _BTO_NUMERO_BATCH_RE.match(numero):
        bto = _fmt_bto(numero)
        if bto:
            return bto
    if "custom" not in blocked:
        custom = _fmt_custom(numero)
        if custom:
            # Il custom ha trovato: si ferma qui. Interrogare anche il catalogo
            # significava incassare il suo 401 e perdere la risposta buona.
            return custom
    if "woocommerce" not in blocked and numero.isdigit():
        wc = _fmt_wc(search_orders_by_id(numero))
        if wc:
            return wc
    if "btoweb" not in blocked:
        bto = _fmt_bto(numero)
        if bto:
            return bto
    consentite = [p for p in ("custom", "WooCommerce", "btoweb") if p.lower() not in blocked]
    return _esito_negativo(f"nessuna delle fonti consentite ({', '.join(consentite)}) è consultabile")


def _custom_forme_cliente(o: dict) -> list:
    """Le identità con cui un cliente può essere cercato in un ordine NORMALIZZATO:
    nome persona, ragione sociale, email (gli stessi tre campi della vecchia
    ricerca a sottostringa)."""
    return [
        f for f in (
            o.get("customer_name"),
            o.get("customer_business_name"),
            o.get("customer_email"),
        ) if f
    ]


def _risolvi_cliente_custom(query: str, orders: list) -> dict:
    """Risolve il nome digitato su uno dei clienti realmente presenti negli ordini
    custom, con gli STESSI tre livelli del match produttori (esatto / parziale /
    somiglianza), riusando _fully_match_cliente. Raggruppa per cliente (email se
    c'è, altrimenti nome+azienda); vince il livello più alto; più clienti allo
    stesso livello = richiesta di chiarimento, mai una scelta al posto dell'utente."""
    clienti = {}
    for o in orders:
        forme = _custom_forme_cliente(o)
        if not forme:
            continue
        chiave = _bto_norm_producer(o.get("customer_email") or " | ".join(forme))
        g = clienti.get(chiave)
        if g is None:
            g = clienti[chiave] = {"forme": forme, "ordini": []}
        g["ordini"].append(o)

    rango = {"esatto": 3, "parziale": 2, "somiglianza": 1}
    candidati = []
    for g in clienti.values():
        liv, punteggio = _fully_match_cliente(query, g["forme"])
        if liv:
            candidati.append({
                "cliente": " | ".join(g["forme"]),
                "match": liv,
                "punteggio": punteggio,
                "_forme": g["forme"],
                "_ordini": g["ordini"],
            })
    if not candidati:
        return {"livello": None, "candidati": []}
    top = max(rango[c["match"]] for c in candidati)
    vincenti = [c for c in candidati if rango[c["match"]] == top]
    vincenti.sort(key=lambda c: -c["punteggio"])
    return {"livello": vincenti[0]["match"], "candidati": vincenti}


_PIATTAFORMA_CUSTOM = (
    "kanokimonos.app (ordini custom). La piattaforma REGISTRA l'ordine: "
    "non spedisce e non riceve merce"
)


def tool_cerca_ordini_per_cliente(nome: str) -> dict:
    """Opzione (b): restituisce dati strutturati così Haiku può filtrarli.
    Match tollerante come per i produttori: 'grapple zone', 'grapple-zone' e
    'GRAPPLEZONE' trovano lo stesso cliente; i refusi passano per somiglianza."""
    nome = (nome or "").strip()
    if not nome:
        return {"error": "Nessun nome cliente fornito.", "ordini": []}
    data = search_custom_orders_raw(1000)
    if data.get("error"):
        return {"error": data["error"], "ordini": []}

    ris = _risolvi_cliente_custom(nome, data.get("results", []))

    if ris["livello"] is None:
        return {
            "piattaforma": _PIATTAFORMA_CUSTOM,
            "cliente_cercato": nome,
            "totale": 0,
            "ordini": [],
            "nota": (
                f"Nessun cliente compatibile con '{nome}' negli ordini custom: "
                "né uguale, né parziale, né simile. NON inventare ordini e NON "
                "attribuirgli ordini di altri: di' che non risulta e chiedi se "
                "il nome è scritto giusto o se è un cliente di un'altra "
                "piattaforma."
            ),
        }

    if len(ris["candidati"]) > 1:
        return {
            "piattaforma": _PIATTAFORMA_CUSTOM,
            "cliente_cercato": nome,
            "richiesta_chiarimento": True,
            "candidati": [
                {"cliente": c["cliente"], "match": c["match"],
                 "ordini_trovati": len(c["_ordini"])}
                for c in ris["candidati"]
            ],
            "nota": (
                f"Più clienti compatibili con '{nome}' (match per "
                f"{ris['livello']}). NON sceglierne uno tu e NON mostrare "
                "ordini: elenca i candidati all'utente e chiedi quale intende, "
                "poi richiama lo strumento con il nome scelto."
            ),
        }

    scelto = ris["candidati"][0]
    ordini = []
    for o in scelto["_ordini"]:
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
    out = {
        "piattaforma": _PIATTAFORMA_CUSTOM,
        "cliente_cercato": nome,
        "cliente_risolto": scelto["cliente"],
        "totale": len(ordini),
        "ordini": ordini,
    }
    if any(o.get("partito_al_cliente") for o in ordini):
        out["nota_spedizione"] = (
            "Per gli ordini spediti: la formula è \"risulta spedito al cliente "
            "(stato registrato sulla piattaforma kanokimonos.app)\" e la spedizione "
            "parte da Fully (magazzino logistico) o, nei casi di spedizione "
            "diretta, dalla fabbrica. MAI dire \"spedito a/su kanokimonos.app\": "
            "la piattaforma registra lo stato, non spedisce."
        )
    out.update({
        "nota_stato": (
            "Lo stato di avanzamento di ogni ordine è 'stato_descrizione' (ricavato da "
            "order_status). Non esiste nessun altro campo di stato: non dire mai che un "
            "ordine è 'in attesa di conferma' se stato_descrizione dice altro."
        ),
    })
    # Dichiarazione d'interpretazione: dovuta ogni volta che il nome digitato non
    # coincide alla lettera con una delle forme del cliente risolto ("grapple
    # zone" -> GRAPPLEZONE), non solo sui refusi.
    q = _bto_norm_producer(nome)
    if not any(_bto_norm_producer(f) == q for f in scelto["_forme"]):
        out["nota_interpretazione"] = (
            f"L'utente ha scritto '{nome}' e lo strumento l'ha risolto sul "
            f"cliente '{scelto['cliente']}' (match per {scelto['match']}). "
            f"DICHIARALO prima dei dati (es. \"interpreto '{nome}' come "
            f"{scelto['_forme'][0]}\")."
        )
    return out


def tool_rispondi_dal_manuale(argomento: str = None, user_message: str = "",
                              role: str = DEFAULT_ROLE) -> str:
    query = argomento or user_message or ""

    def _consegna(testo: str) -> str:
        """Per i profili esterni il manuale esce SENZA nomi di persone interne:
        la redazione avviene qui, sull'unica strada per cui il testo del manuale
        raggiunge il modello."""
        if _normalize_role(role) in ROLES_INTERNI:
            return testo
        pulito = redigi_nomi_interni(testo)
        return (
            pulito
            + "\n\n[NOTA DI SISTEMA: i nomi delle persone interne sono stati "
            "rimossi da questo materiale e NON vanno ricostruiti né ipotizzati. "
            "Se l'utente chiede di persone, personale o organigramma, rimanda a "
            "info@kanokimonos.com.]"
        )

    # Domande sulle taglie: restituisci la GUIDA TAGLIE per intero (contigua),
    # perché il retrieval per-riga frammenta la tabella e la rende inaffidabile.
    if _is_size_query(f"{argomento or ''} {user_message or ''}"):
        guide = get_size_guide_block()
        if guide:
            extra = get_knowledge_context(query)
            return _consegna(guide + (("\n\n" + extra) if extra else ""))
    context = get_knowledge_context(query)
    if not context:
        return "NESSUN_CONTENUTO: il manuale non contiene informazioni su questo argomento."
    return _consegna(context)


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
        "piattaforma": "kanokimonos.app (ordini custom)",
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
        # Mai 'details': era il corpo grezzo della edge function e finiva a video.
        return None, {"error": data.get("error"), "fonte": "listino"}
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

# I sei contatori della pipeline btoweb, in un posto solo: servono sia per
# sommare i gruppi sia per i totali su tutti i gruppi, e devono restare gli stessi.
_BTO_CONTATORI_PRODUZIONE = (
    "in_produzione",
    "prodotti_e_spediti_dal_fornitore",
    "ricevuti_conformi",
    "mancanti",
    "danneggiati",
    "attesi_su_fully",
)

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
            nota = f"Nessun prodotto in anagrafica btoweb con SKU/EAN {sku_clean}."
            # Un valore in forma di batch (082026-0002) non è uno SKU: prima di
            # dire "non trovo niente" va provata l'altra strada, come già si fa
            # fra produttori e clienti.
            if _BTO_NUMERO_BATCH_RE.match(sku_clean):
                nota += (
                    f" ATTENZIONE: '{sku_clean}' NON ha la forma di uno SKU/EAN (sole "
                    "cifre): ha la forma di un numero di ORDINE DI FABBRICA (batch). "
                    "NON rispondere che non trovi niente: richiama SUBITO "
                    "ordine_fabbrica_per_numero con questo numero."
                )
            return {
                "tipo": "anagrafica_prodotti",
                "sku_cercato": sku_clean,
                "trovato": False,
                "totale": 0,
                "risultati": [],
                "nota": nota,
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
                    "_righe": 0,
                    "dettaglio_taglie": [],
                }
                ordine.append(base)
            g = gruppi[base]
            g["_righe"] += 1
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

        # I gruppi consegnati sono un TAGLIO della lista (ordine[:25] qui sotto) e il
        # taglio segue la PERTINENZA, non la quantità: i gruppi più grossi possono
        # restare fuori tutti insieme. Chi legge solo l'elenco somma una parte e la
        # chiama totale. I totali quindi si calcolano QUI, su TUTTI i gruppi e PRIMA
        # di qualunque taglio, e viaggiano nel payload accanto all'elenco.
        totali_tutti = {
            c: sum(g[c] for g in gruppi.values()) for c in _BTO_CONTATORI_PRODUZIONE
        }

        ordine.sort(key=lambda b: _bto_rank_key(b, q))
        out_gruppi = [gruppi[k] for k in ordine[:25]]
        # Il mucchio senza nome pesa più di ogni prodotto vero e con _bto_rank_key
        # (a parità di query vince il nome più corto) finiva in TESTA alla lista:
        # chi legge di fretta vede 1252 pezzi in cima e crede sia un articolo.
        # Va in fondo. sort è stabile, quindi gli altri gruppi non si spostano.
        out_gruppi.sort(key=lambda g: g["prodotto"] == "(senza nome)")
        for g in out_gruppi:
            n_righe = g.pop("_righe")
            # Stessa contromisura che l'anagrafica ha già sul dato mancante alla
            # fonte: su 'stock' esistono righe con product_name a null (hanno solo
            # lo SKU). Raggruppate finiscono tutte sotto un'unica etichetta e
            # sembrano UN prodotto enorme. Va detto che non è un prodotto.
            if g["prodotto"] == "(senza nome)":
                g["nome_prodotto_non_valorizzato"] = True
                g["nota_prodotto"] = (
                    "NON è un prodotto: sono %d righe di btoweb in cui il NOME del "
                    "prodotto non è valorizzato alla fonte (c'è solo lo SKU), finite "
                    "insieme qui perché non hanno un nome con cui distinguerle. I "
                    "numeri di questa voce sono la SOMMA di SKU diversi fra loro: non "
                    "presentarla come un articolo, non darle un nome tuo e non dedurre "
                    "di che prodotti si tratti. Se serve sapere cosa sono, vanno "
                    "guardati gli SKU uno per uno." % n_righe
                )
            g["dettaglio_taglie"] = g["dettaglio_taglie"][:30]

        # La somma dell'elenco viaggia dichiarata per quello che è, così il divario
        # con i totali veri è visibile invece che da indovinare.
        somma_mostrati = {
            c: sum(g[c] for g in out_gruppi) for c in _BTO_CONTATORI_PRODUZIONE
        }
        troncato = len(out_gruppi) < len(ordine)
        if troncato:
            nota_totali = (
                "TOTALI: usa SOLO 'totali_pipeline_tutti_i_gruppi'. L'elenco 'prodotti' è "
                "TRONCATO: sono %d gruppi su %d, scelti per PERTINENZA e non per quantità, "
                "quindi i %d gruppi non elencati possono contenere la maggior parte dei "
                "pezzi e, su un singolo contatore, anche tutti. È VIETATO sommare i gruppi "
                "dell'elenco e presentare quella somma come il totale della pipeline, come "
                "totale 'complessivo' o come quadro completo: è la somma di una parte. "
                "'somma_dei_soli_gruppi_mostrati' è lì per confronto e NON è un totale: "
                "se riporti quei numeri devi dire che riguardano solo i %d gruppi elencati. "
                "E se un contatore è 0 nell'elenco ma non nei totali, NON dire che non c'è "
                "niente in quello stato: quel dato sta tutto nei gruppi non elencati."
                % (len(out_gruppi), len(ordine), len(ordine) - len(out_gruppi), len(out_gruppi))
            )
        else:
            nota_totali = (
                "TOTALI: usa 'totali_pipeline_tutti_i_gruppi'. Qui l'elenco 'prodotti' NON è "
                "troncato (%d gruppi su %d): i totali coprono esattamente i gruppi elencati."
                % (len(out_gruppi), len(ordine))
            )

        return {
            "tipo": "produzione_pipeline",
            "query": q or None,
            "righe_totali_fonte": res.get("total"),
            "righe_scaricate": len(rows),
            "righe_corrispondenti": len(sel),
            "prodotti_trovati": len(ordine),
            "gruppi_mostrati": len(out_gruppi),
            "elenco_prodotti_troncato": troncato,
            "totali_pipeline_tutti_i_gruppi": totali_tutti,
            "somma_dei_soli_gruppi_mostrati": somma_mostrati,
            "nota_totali": nota_totali,
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
            "piattaforma": "btoweb (ordini di FABBRICA verso i produttori)",
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
            "piattaforma": "btoweb (ordini di FABBRICA verso i produttori)",
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
            "piattaforma": "btoweb (ordini di FABBRICA verso i produttori)",
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


# --- ORDINI DI FABBRICA PER NUMERO (batch) -----------------------------------
# Un ordine di fabbrica btoweb torna come PIÙ righe con lo stesso order_number:
# ogni riga è un articolo (prodotto + colore) e dentro 'products' ha una riga per
# taglia. Produttore, stato, data e conferme stanno DENTRO le righe dell'ordine:
# chi ha il numero ha già il produttore, quindi il produttore non si chiede mai
# all'utente, si legge.

# 082026-0002, 122025-0007: sei cifre (mese+anno) - quattro cifre progressive.
_BTO_NUMERO_BATCH_RE = re.compile(r"^\d{6}\s*-\s*\d{1,4}$")
# Prefisso mese/anno da solo ('082026'): ricerca parziale, tutti i batch del mese.
_BTO_NUMERO_PREFISSO_RE = re.compile(r"^\d{6}$")

# I tre valori di products_source dichiarano DA DOVE arrivano le quantità della
# riga. Le differenze qui sotto sono verificate sui dati reali (qualche migliaio
# di righe-prodotto: il numero esatto cresce a ogni batch, non va cablato qui).
# Lo SKU esiste solo su size_lines. La quantità PRODOTTA invece NON segue la
# sorgente: è valorizzata solo su una parte delle righe, e dentro la stessa
# sorgente convivono righe che ce l'hanno e righe che non ce l'hanno. Si guarda
# riga per riga. Vanno dichiarate, non date per scontate.
_BTO_PRODUCTS_SOURCE_LABELS = {
    "size_lines": (
        "quantità lette dalle righe-taglia del batch: è l'unica sorgente che porta "
        "anche lo SKU (e spesso il colore) di ogni taglia. La quantità PRODOTTA su "
        "questa sorgente c'è su alcune righe e manca su altre: leggila riga per "
        "riga, non darla per assente"
    ),
    "sizes": (
        "quantità lette dalla ripartizione taglie del prodotto: niente SKU e niente "
        "quantità prodotta, solo taglia e quantità ordinata"
    ),
    "production_quantities": (
        "quantità lette dai conteggi di produzione del fornitore: niente SKU. Su "
        "questa sorgente la quantità PRODOTTA è di norma valorizzata, ma non è "
        "l'unica sorgente che la porta: leggila riga per riga, non dedurla dalla "
        "sorgente"
    ),
}

_BTO_MAX_RIGHE_TAGLIA = 150
_BTO_MAX_ORDINI_ELENCO = 30


def _bto_num_norm(value) -> str:
    """Numero d'ordine confrontabile: minuscolo, senza spazi né accenti."""
    return re.sub(r"\s+", "", _bto_norm_producer(value))


def _bto_num_squash(value) -> str:
    """Solo cifre e lettere: '082026 0002' e '082026-0002' diventano uguali."""
    return re.sub(r"[^a-z0-9]+", "", _bto_num_norm(value))


def _bto_righe_per_numero(numero: str):
    """Righe btoweb del numero cercato. Usa il filtro order_number della edge
    function (supporta anche il parziale) e RIFILTRA lato bot come gli altri
    strumenti: il filtro server-side non è un'autorizzazione a fidarsi. Se il
    filtro non restituisce nulla si riscarica tutto e si filtra qui, così un
    filtro che smettesse di funzionare non diventa un falso 'non esiste'."""
    q = _bto_num_norm(numero)
    qs = _bto_num_squash(numero)

    rows, meta = _bto_get_paged({"resource": "orders", "order_number": numero.strip()})
    if rows is None:
        return None, meta, []

    def _filtra(righe):
        esatte = [
            r for r in righe
            if _bto_num_norm(r.get("order_number")) == q
            or _bto_num_norm(r.get("batch_name")) == q
            or (qs and (_bto_num_squash(r.get("order_number")) == qs
                        or _bto_num_squash(r.get("batch_name")) == qs))
        ]
        if esatte:
            return esatte, True
        parziali = [
            r for r in righe
            if q and (q in _bto_num_norm(r.get("order_number"))
                      or q in _bto_num_norm(r.get("batch_name")))
        ]
        return parziali, False

    sel, esatto = _filtra(rows)
    tutte = rows
    if not sel:
        tutte, meta2 = _bto_get_paged({"resource": "orders"})
        if tutte is None:
            return None, meta2, []
        sel, esatto = _filtra(tutte)
    return sel, {"esatto": esatto}, tutte


def _bto_dedup_righe(righe: list) -> list:
    """La fonte può ripetere la stessa identica riga N volte: senza dedup le
    quantità verrebbero moltiplicate (stessa cautela di ordini_per_produttore)."""
    viste, out = set(), []
    for r in righe:
        firma = json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)
        if firma in viste:
            continue
        viste.add(firma)
        out.append(r)
    return out


def _bto_valori(righe: list, campo: str) -> list:
    """Valori distinti di un campo, nell'ordine in cui compaiono. I campi
    d'ordine (produttore, stato, data) sono ripetuti su ogni riga: quasi sempre
    uno solo, ma se la fonte ne ha due non si sceglie, si dichiarano entrambi."""
    out = []
    for r in righe:
        v = r.get(campo)
        if isinstance(v, str):
            v = v.strip() or None
        if v is not None and v not in out:
            out.append(v)
    return out


def _bto_uno(valori: list):
    return valori[0] if len(valori) == 1 else (valori or None)


def _bto_articoli(righe: list) -> tuple:
    """Righe -> articoli. Ogni articolo è un prodotto+colore con le sue taglie:
    taglia, SKU, quantità ordinata e quantità prodotta. La taglia si legge da
    'size' ('category' è solo un alias dello stesso valore)."""
    gruppi, chiavi = {}, []
    righe_taglia = 0
    for r in righe:
        nota = (r.get("notes") or "").strip() or None
        for p in (r.get("products") or []):
            if not isinstance(p, dict):
                continue
            nome = (p.get("name") or "").strip() or "(senza nome)"
            colore = (p.get("colour") or "").strip() or None
            k = (nome, colore)
            if k not in gruppi:
                gruppi[k] = {
                    "prodotto": nome,
                    "colore": colore,
                    "origine_quantita": [],
                    "note_fonte": [],
                    "taglie": [],
                }
                chiavi.append(k)
            g = gruppi[k]
            src = p.get("source") or r.get("products_source")
            if src and src not in g["origine_quantita"]:
                g["origine_quantita"].append(src)
            if nota and nota not in g["note_fonte"]:
                g["note_fonte"].append(nota)
            # 'category' è l'alias di 'size': si legge size e si tiene category
            # solo come ripiego se size non fosse valorizzata.
            taglia = (p.get("size") or p.get("category") or "").strip() or None
            try:
                qty = int(p.get("quantity_ordered") if p.get("quantity_ordered") is not None
                          else (p.get("quantity") or 0))
            except (TypeError, ValueError):
                qty = 0
            prodotta = p.get("quantity_produced")
            try:
                prodotta = int(prodotta) if prodotta is not None else None
            except (TypeError, ValueError):
                prodotta = None
            riga = {
                "taglia": taglia,
                "sku": (p.get("sku") or None),
                "quantita_ordinata": qty,
                "quantita_prodotta": prodotta,
            }
            g["taglie"].append(riga)
            righe_taglia += 1

    articoli = []
    for k in chiavi:
        g = gruppi[k]
        g["pezzi_ordinati"] = sum(t["quantita_ordinata"] for t in g["taglie"])
        prodotte = [t["quantita_prodotta"] for t in g["taglie"] if t["quantita_prodotta"] is not None]
        g["pezzi_prodotti"] = sum(prodotte) if prodotte else None
        if not any(t["sku"] for t in g["taglie"]):
            g["nota_sku"] = (
                "SKU NON valorizzato alla fonte per questo articolo (origine "
                "quantità: %s): dillo, non inventarlo e non prenderlo dal catalogo."
                % ", ".join(g["origine_quantita"] or ["non dichiarata"])
            )
        if g["pezzi_prodotti"] is None:
            g["nota_quantita_prodotta"] = (
                "Quantità PRODOTTA non valorizzata alla fonte: sai solo quanto è "
                "stato ordinato, non quanto è stato prodotto."
            )
        if not g["note_fonte"]:
            g.pop("note_fonte")
        articoli.append(g)
    return articoli, righe_taglia


def _bto_scheda_ordine(numero: str, righe: list) -> dict:
    """Scheda completa di un ordine di fabbrica: produttore, stato, data attesa,
    conferme di ricezione, note e articoli con taglia/SKU/quantità."""
    righe = _bto_dedup_righe(righe)
    articoli, n_taglie = _bto_articoli(righe)

    stati = _bto_valori(righe, "status")
    produttori = _bto_valori(righe, "producer")
    date = _bto_valori(righe, "expected_arrival_date")
    sorgenti = _bto_valori(righe, "products_source")

    pezzi = sum(a["pezzi_ordinati"] for a in articoli)
    prodotti_val = [a["pezzi_prodotti"] for a in articoli if a["pezzi_prodotti"] is not None]

    # Controprova: 'totals' è dichiarato dalla fonte riga per riga. Se la somma
    # delle quantità non torna con la somma dei totals, il dato va guardato, non
    # spacciato per buono.
    tot_fonte = 0
    tot_fonte_visto = False
    tot_prodotti_fonte, tot_prodotti_visto = 0, False
    for r in righe:
        t = r.get("totals") or {}
        if t.get("qty_ordered") is not None:
            try:
                tot_fonte += int(t["qty_ordered"])
                tot_fonte_visto = True
            except (TypeError, ValueError):
                pass
        if t.get("qty_produced") is not None:
            try:
                tot_prodotti_fonte += int(t["qty_produced"])
                tot_prodotti_visto = True
            except (TypeError, ValueError):
                pass

    # Le conferme si leggono a parte e non con _bto_valori: un False è un "No"
    # da dire, non un valore da scartare.
    conf_prod = sorted({bool(r.get("producer_confirmed_receipt")) for r in righe}, reverse=True)
    conf_batch = sorted({bool(r.get("batch_receipt_confirmed")) for r in righe}, reverse=True)

    def _conferma(valori):
        if len(valori) == 1:
            return "Sì" if valori[0] else "No"
        return "parziale (righe dell'ordine discordi alla fonte)"

    # La conferma è registrata articolo per articolo, quindi i timestamp sono
    # tanti e a pochi secondi l'uno dall'altro: si dichiara il primo, e l'ultimo
    # solo se è un momento diverso. Elencarli tutti sarebbe rumore.
    momenti = sorted(_bto_valori(righe, "producer_confirmed_at"))
    conf_quando = momenti[0] if momenti else None

    stato = _bto_uno(stati)
    scheda = {
        "numero_ordine": numero,
        "piattaforma": "btoweb (ordine di FABBRICA verso il produttore, non un ordine di un cliente)",
        "produttore": _bto_uno(produttori),
        "stato": stato,
        "stato_descrizione": _BTO_ORDER_STATUS_LABELS.get(stato, stato),
        "stato_lato_produttore": _bto_uno(_bto_valori(righe, "producer_status")),
        "data_arrivo_prevista": _bto_uno(date),
        "conferme_ricezione": {
            "il_produttore_ha_confermato_di_aver_preso_in_carico_l_ordine": _conferma(conf_prod),
            "confermato_il": conf_quando,
            "ultima_conferma_il": momenti[-1] if len(momenti) > 1 else None,
            "ricezione_batch_confermata_su_btoweb": _conferma(conf_batch),
            "attenzione": (
                "Sono conferme registrate SU BTOWEB sull'ordine/batch: NON sono la "
                "prova che la merce sia arrivata fisicamente in magazzino. L'arrivo "
                "della merce si verifica solo con tracciamento_fully."
            ),
        },
        "mesi_di_copertura": _bto_uno(_bto_valori(righe, "supply_months")),
        "note_fonte": _bto_valori(righe, "notes")[:6],
        "origine_quantita_products_source": sorgenti,
        "origine_quantita_significato": {
            s: _BTO_PRODUCTS_SOURCE_LABELS.get(s, "valore non previsto: dichiaralo così com'è")
            for s in sorgenti
        },
        "articoli_totali": len(articoli),
        "righe_taglia_totali": n_taglie,
        "pezzi_ordinati_totali": pezzi,
        "pezzi_prodotti_totali": sum(prodotti_val) if prodotti_val else None,
        "articoli": articoli,
    }

    if not date:
        scheda["nota_data"] = "Data di arrivo prevista NON valorizzata alla fonte btoweb."
    if not articoli:
        scheda["nota_prodotti"] = (
            "Dettaglio prodotti NON valorizzato alla fonte per questo ordine: dillo, "
            "non dedurre cosa contiene."
        )
    # La quantità prodotta NON dipende dalla sorgente dichiarata: dentro la stessa
    # products_source convivono righe che ce l'hanno e righe che non ce l'hanno.
    # Quindi il conto si fa riga per riga, e un totale costruito solo su una parte
    # delle righe va dichiarato PARZIALE: altrimenti "418 ordinati / 95 prodotti"
    # sembra un ordine indietro con la produzione, quando invece di 323 pezzi non
    # si sa niente.
    righe_taglia = [t for a in articoli for t in a["taglie"]]
    con_prodotta = [t for t in righe_taglia if t["quantita_prodotta"] is not None]
    senza_prodotta = [t for t in righe_taglia if t["quantita_prodotta"] is None]
    pezzi_senza_dato = sum(t["quantita_ordinata"] for t in senza_prodotta)

    if scheda["pezzi_prodotti_totali"] is None and righe_taglia:
        scheda["nota_pezzi_prodotti"] = (
            "La fonte non registra la quantità prodotta per NESSUNA delle %d righe di "
            "questo ordine: parla solo di pezzi ORDINATI. La quantità prodotta è "
            "valorizzata solo su una parte delle righe e non si deduce dalla sorgente "
            "('origine_quantita_products_source'): non dire che manca PERCHÉ la "
            "sorgente è quella, di' che alla fonte non c'è." % len(righe_taglia)
        )
    elif senza_prodotta:
        scheda["quantita_prodotta_parziale"] = True
        scheda["nota_pezzi_prodotti"] = (
            "TOTALE PARZIALE, dillo sempre: 'pezzi_prodotti_totali' (%d) è la somma di "
            "sole %d righe su %d. Sulle altre %d righe, che valgono %d pezzi ORDINATI, "
            "la fonte NON registra nessuna quantità prodotta: di quei %d pezzi non si "
            "sa quanti ne siano stati prodotti. È VIETATO dire '%d prodotti su %d "
            "ordinati' o presentarlo come un ordine indietro con la produzione: di' "
            "che il dato di produzione copre solo una parte dell'ordine e dichiara "
            "quale parte resta senza dato."
            % (
                scheda["pezzi_prodotti_totali"],
                len(con_prodotta),
                len(righe_taglia),
                len(senza_prodotta),
                pezzi_senza_dato,
                pezzi_senza_dato,
                scheda["pezzi_prodotti_totali"],
                pezzi,
            )
        )
    if tot_fonte_visto:
        scheda["totale_dichiarato_dalla_fonte"] = tot_fonte
        scheda["controprova_totali"] = (
            "coerente: la somma delle quantità per taglia coincide con il totale "
            "dichiarato dalla fonte (%d)" % tot_fonte
            if tot_fonte == pezzi
            else "DISCREPANZA: somma delle taglie %d, totale dichiarato dalla fonte %d. "
            "Segnalala, non scegliere tu quale sia buono." % (pezzi, tot_fonte)
        )
    if tot_prodotti_visto:
        scheda["totale_prodotto_dichiarato_dalla_fonte"] = tot_prodotti_fonte
    if _UUID_RE.match(str(numero)):
        scheda["nota_numero"] = (
            "Numero d'ordine non valorizzato alla fonte: questo è l'id tecnico interno."
        )
    if n_taglie > _BTO_MAX_RIGHE_TAGLIA:
        tenute = 0
        for a in scheda["articoli"]:
            resta = max(0, _BTO_MAX_RIGHE_TAGLIA - tenute)
            if len(a["taglie"]) > resta:
                a["taglie_non_mostrate"] = len(a["taglie"]) - resta
                a["taglie"] = a["taglie"][:resta]
            tenute += len(a["taglie"])
        scheda["nota_troncamento"] = (
            "Ordine molto grande: mostrate le prime %d righe-taglia su %d. I totali "
            "sopra sono calcolati su TUTTE le righe." % (_BTO_MAX_RIGHE_TAGLIA, n_taglie)
        )
    return scheda


def tool_ordine_fabbrica_per_numero(numero: str = None) -> dict:
    """Ordine di FABBRICA btoweb cercato per numero di batch/ordine (solo staff).
    Accetta anche un numero parziale ('082026' = tutti i batch di agosto 2026).
    Il produttore è un RISULTATO, non un ingresso: non va chiesto all'utente."""
    q = (numero or "").strip()
    if not q:
        return {"error": "Serve il numero dell'ordine di fabbrica (batch) da cercare."}

    righe, meta, tutte = _bto_righe_per_numero(q)
    if righe is None:
        return meta

    # Se il modello passa il numero dentro una frase ('batch 082026-0002'), il
    # numero si estrae: è dato dall'utente, non c'è niente da chiedergli.
    estratto = None
    if not righe:
        m = re.search(r"\d{6}\s*-\s*\d{1,4}", q) or re.search(r"(?<!\d)\d{6}(?!\d)", q)
        if m and m.group(0) != q:
            estratto = m.group(0)
            righe, meta, tutte = _bto_righe_per_numero(estratto)
            if righe is None:
                return meta

    if not righe:
        # Numeri realmente presenti con lo stesso prefisso mese/anno: servono a
        # dire "esistono questi", MAI a spacciarne uno per quello cercato.
        prefisso = _bto_num_squash(estratto or q)[:6]
        simili = []
        for r in (tutte or []):
            n = str(r.get("order_number") or "").strip()
            if n and _bto_num_squash(n).startswith(prefisso) and n not in simili:
                simili.append(n)
        out = {
            "tipo": "ordine_fabbrica_btoweb",
            "piattaforma": "btoweb (ordini di FABBRICA verso i produttori)",
            "numero_cercato": q,
            "trovato": False,
            "ordini": [],
            "nota": (
                f"Nessun ordine di FABBRICA btoweb con il numero '{q}'. Questo numero NON "
                "esiste su btoweb: non inventarne il contenuto, non attribuirlo a un "
                "produttore e non spacciarne un altro per lui. Se il numero può invece "
                "essere un ordine CUSTOM di kanokimonos.app (formato NNNN-MM-YY, es. "
                "0495-05-26-A) riprova con cerca_ordine_per_numero prima di rispondere."
            ),
        }
        if estratto:
            out["numero_estratto_dal_testo"] = estratto
        if simili:
            out["numeri_btoweb_esistenti_con_lo_stesso_prefisso"] = simili[:_BTO_MAX_ORDINI_ELENCO]
            out["nota_simili"] = (
                "Questi numeri esistono su btoweb e iniziano come quello cercato, ma NON "
                "sono l'ordine chiesto: citali solo per chiedere se intendeva uno di loro."
            )
        return out

    gruppi, chiavi = {}, []
    for r in righe:
        num = str(r.get("order_number") or "").strip() or "(senza numero)"
        if num not in gruppi:
            gruppi[num] = []
            chiavi.append(num)
        gruppi[num].append(r)

    if len(chiavi) == 1:
        scheda = _bto_scheda_ordine(chiavi[0], gruppi[chiavi[0]])
        if estratto:
            scheda["numero_estratto_dal_testo"] = estratto
        scheda.update({
            "tipo": "ordine_fabbrica_btoweb",
            "numero_cercato": q,
            "trovato": True,
            "ricerca_parziale": not meta.get("esatto"),
            "nota": (
                "Ordine di FABBRICA su btoweb: merce ordinata a un produttore, NON un "
                "ordine di un cliente. Dichiaralo fin dalla prima riga. Il produttore è "
                "nel campo 'produttore' qui sopra: non chiederlo all'utente. Riporta "
                "taglie, quantità, SKU, stato, data prevista, conferme e note esattamente "
                "come stanno qui; dove il dato manca dichiaralo, non stimarlo."
            ),
            "nota_prezzi": _BTO_NOTA_PREZZI,
        })
        return scheda

    ordini = []
    for num in chiavi[:_BTO_MAX_ORDINI_ELENCO]:
        s = _bto_scheda_ordine(num, gruppi[num])
        ordini.append({
            "numero_ordine": num,
            "produttore": s["produttore"],
            "stato": s["stato"],
            "stato_descrizione": s["stato_descrizione"],
            "data_arrivo_prevista": s["data_arrivo_prevista"],
            "articoli_totali": s["articoli_totali"],
            "pezzi_ordinati_totali": s["pezzi_ordinati_totali"],
            "prodotti": [a["prodotto"] for a in s["articoli"]][:8],
        })

    return {
        "tipo": "ordini_fabbrica_btoweb",
        "piattaforma": "btoweb (ordini di FABBRICA verso i produttori)",
        "numero_cercato": q,
        "trovato": True,
        "ricerca_parziale": True,
        "ordini_totali": len(chiavi),
        "ordini_mostrati": len(ordini),
        "ordini": ordini,
        "nota": (
            f"'{q}' è un numero PARZIALE: corrispondono {len(chiavi)} ordini di FABBRICA "
            "btoweb. Elencali con numero, produttore, stato e pezzi. Il produttore è qui "
            "accanto a ogni ordine: non chiederlo. Per il dettaglio di taglie, SKU e "
            "quantità richiama questo strumento con il numero completo di un ordine."
        ),
        "nota_prezzi": _BTO_NOTA_PREZZI,
    }


def format_bto_order_card(res: dict) -> str:
    """Scheda testuale dell'ordine di fabbrica per il percorso
    cerca_ordine_per_numero, che restituisce testo e non JSON."""
    if not res or res.get("error") or not res.get("trovato"):
        return ""

    if res.get("tipo") == "ordini_fabbrica_btoweb":
        lines = [
            f"Ordini di FABBRICA su btoweb che iniziano con «{res.get('numero_cercato')}» "
            f"({res.get('ordini_totali')} trovati) — piattaforma btoweb, merce ordinata ai "
            "produttori, non ordini di clienti:"
        ]
        for o in res.get("ordini", []):
            lines.append(
                "• %s | %s | %s | arrivo previsto: %s | %s articoli, %s pz"
                % (
                    o.get("numero_ordine"),
                    o.get("produttore") or "produttore N/A",
                    o.get("stato_descrizione") or o.get("stato") or "stato N/A",
                    o.get("data_arrivo_prevista") or "non valorizzato",
                    o.get("articoli_totali"),
                    o.get("pezzi_ordinati_totali"),
                )
            )
        return "\n".join(lines)

    c = res.get("conferme_ricezione", {})
    lines = [
        f"Ordine di FABBRICA {res.get('numero_ordine')} — Piattaforma: btoweb "
        "(merce ordinata al produttore, NON un ordine di un cliente)",
        f"Produttore: {res.get('produttore') or 'N/A'}",
        f"Stato: {res.get('stato') or 'N/A'}"
        + (f" ({res['stato_descrizione']})" if res.get("stato_descrizione") else ""),
        f"Arrivo previsto: {res.get('data_arrivo_prevista') or 'non valorizzato alla fonte'}",
        "Presa in carico confermata dal produttore: %s%s"
        % (
            c.get("il_produttore_ha_confermato_di_aver_preso_in_carico_l_ordine", "N/A"),
            f" (dal {c['confermato_il']})" if c.get("confermato_il") else "",
        ),
        f"Ricezione batch confermata su btoweb: {c.get('ricezione_batch_confermata_su_btoweb', 'N/A')}",
        "Queste conferme riguardano l'ordine su btoweb e NON provano l'arrivo fisico "
        "della merce.",
        "Contenuto: %s articoli, %s pezzi ordinati%s"
        % (
            res.get("articoli_totali"),
            res.get("pezzi_ordinati_totali"),
            " (totale dichiarato dalla fonte: %s)" % res["totale_dichiarato_dalla_fonte"]
            if res.get("totale_dichiarato_dalla_fonte") is not None
            else "",
        ),
        "Pezzi prodotti: %s"
        % (
            res["pezzi_prodotti_totali"]
            if res.get("pezzi_prodotti_totali") is not None
            else "non valorizzato alla fonte (si sa solo quanto è stato ordinato)"
        ),
        "Origine delle quantità (products_source): %s"
        % (", ".join(res.get("origine_quantita_products_source") or []) or "non dichiarata"),
    ]
    for a in res.get("articoli", []):
        taglie = ", ".join(
            "%s ×%s%s"
            % (
                t.get("taglia") or "taglia N/A",
                t.get("quantita_ordinata"),
                f" [SKU {t['sku']}]" if t.get("sku") else "",
            )
            for t in a.get("taglie", [])
        )
        lines.append(
            "• %s%s — %s pz ordinati%s: %s"
            % (
                a.get("prodotto"),
                f" (colore: {a['colore']})" if a.get("colore") else "",
                a.get("pezzi_ordinati"),
                f", {a['pezzi_prodotti']} prodotti" if a.get("pezzi_prodotti") is not None else "",
                taglie or "dettaglio taglie non valorizzato",
            )
        )
    if res.get("note_fonte"):
        lines.append("Note dalla fonte: " + " | ".join(res["note_fonte"]))
    if res.get("nota_troncamento"):
        lines.append(res["nota_troncamento"])
    lines.append(_BTO_NOTA_PREZZI)
    return "\n".join(lines)


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


# --- RIPARTENZA VERSO IL CLIENTE: la fonte primaria è fully_outbound ----------
# Registro degli invii da Fully verso il cliente (riga con status='sent' e
# last_error NULL = l'ordine È partito). Copre solo dal 23/06/2026 in poi: per
# gli ordini più vecchi la sola fonte restano i campi logistics_* dell'ordine,
# e va SEMPRE dichiarato quale delle due fonti ha risposto.
# fully_order_id NON è un tracking: è l'identificativo dell'invio su Fully.
# last_error valorizzato NON è una spedizione fallita: i casi reali sono
# MARKER_EXCLUSION (merce già consegnata per altra via, tracking nei campi
# logistics_*).

_FULLY_OUTBOUND_NOTA_NUMERO = (
    "identificativo dell'ordine di uscita su Fully, NON un numero di tracking "
    "corriere: NON darlo al cliente per tracciare il pacco"
)


def _fully_blocco_ripartenza(o: dict, riga_out: dict, compagni: list,
                             copertura_dal: str) -> tuple:
    """(blocco ripartenza_verso_cliente | None, data_partenza | None, note extra).

    Ordine di lettura: registro invii Fully (fully_outbound) -> campi
    logistics_* dell'ordine -> nessuna fonte. Il blocco dichiara sempre da
    quale fonte arriva la risposta; le due fonti non si fondono in silenzio."""
    os_code = o.get("order_status")
    log_at = o.get("logistics_shipped_at")

    def _avviso_cliente(r):
        if r.get("customer_shipping_email_sent_at"):
            return {
                "mail_di_spedizione_inviata_il": r.get("customer_shipping_email_sent_at"),
                "a": r.get("customer_shipping_email_to"),
            }
        return {
            "mail_di_spedizione": (
                "NON risulta registrato nessun avviso via mail al cliente. Non "
                "significa che il cliente non sia stato avvisato per altra via "
                "(WhatsApp, telefono): di' che l'avviso non risulta a sistema, "
                "non che non è stato fatto."
            )
        }

    if isinstance(riga_out, dict) and riga_out.get("last_error"):
        # Riga presente ma con last_error: NON è una spedizione fallita. I casi
        # reali sono esclusioni per merce già consegnata per altra via: il dato
        # di spedizione vero sta nei campi logistics_* dell'ordine.
        blocco = {
            "fonte": (
                "campi del vecchio modulo logistico dell'ordine (l'invio via "
                "registro Fully risulta ESCLUSO, vedi 'invio_fully_escluso')"
            ),
            "invio_fully_escluso": {
                "testo_alla_fonte": riga_out.get("last_error"),
                "come_leggerlo": (
                    "NON è una spedizione fallita: l'invio tramite Fully è stato "
                    "ESCLUSO perché la merce risulta già consegnata/spedita per "
                    "altra via. Racconta l'esclusione con il testo qui sopra, "
                    "senza parlare di errori o fallimenti."
                ),
            },
        }
        if log_at:
            blocco.update({
                "spedito_da_fully_il": log_at,
                "corriere": o.get("logistics_courier"),
                "tracking": o.get("logistics_tracking"),
            })
        else:
            blocco["nota"] = (
                "Nemmeno i campi logistici dell'ordine hanno una data di "
                "partenza: la data non risulta da nessuna delle due fonti."
            )
        return blocco, log_at, []

    if isinstance(riga_out, dict) and riga_out.get("status") == "sent":
        blocco = {
            "fonte": (
                "registro invii Fully (fully_outbound). CITA QUESTA FONTE nella "
                "risposta insieme alla data, es. \"partito l'8 luglio (dal "
                "registro invii Fully)\": e' una registrazione diversa dai campi "
                "del modulo logistico e il lettore deve sapere quale delle due "
                "ha risposto."
            ),
            "partito_il": riga_out.get("sent_at"),
            "numero_invio_fully": riga_out.get("fully_order_id"),
            "nota_numero_invio": _FULLY_OUTBOUND_NOTA_NUMERO,
            "asn_di_origine": riga_out.get("shipment_number"),
            "invio_registrato_da": riga_out.get("trigger_type"),
            "avviso_al_cliente": _avviso_cliente(riga_out),
        }
        if compagni:
            blocco["spedizione_raggruppata_con"] = compagni
            blocco["nota_raggruppata"] = (
                "Questi ordini condividono lo stesso invio Fully: sono partiti "
                "nella STESSA spedizione raggruppata (il cliente riceve un "
                "collo unico)."
            )
        # Tracking corriere: il registro NON lo contiene. Se c'è, sta nei campi
        # logistici dell'ordine e va dichiarato come tale; se non c'è, dirlo.
        if o.get("logistics_tracking"):
            blocco["tracking_corriere"] = {
                "fonte": "campi logistici dell'ordine (non dal registro Fully)",
                "tracking": o.get("logistics_tracking"),
                "corriere": o.get("logistics_courier"),
            }
        else:
            blocco["tracking_corriere"] = (
                "NON valorizzato in nessuna fonte: senza tracking non sai dove "
                "sia il pacco, dillo apertamente."
            )
        return blocco, riga_out.get("sent_at"), []

    # Nessuna riga nel registro: fallback sui campi logistici dell'ordine.
    if log_at:
        blocco = {
            "fonte": "campi del vecchio modulo logistico dell'ordine",
            "spedito_da_fully_il": log_at,
            "corriere": o.get("logistics_courier"),
            "tracking": o.get("logistics_tracking"),
            "nota_registro": (
                "Per questo ordine non esiste una riga nel registro invii Fully"
                + (f" (il registro copre solo dal {copertura_dal})" if copertura_dal else "")
                + ": il dato viene dai campi logistici dell'ordine."
            ),
        }
        return blocco, log_at, []

    # Nessuna fonte. Se lo stato dice spedito, la mancanza va dichiarata.
    if os_code in ("shipped_to_customer", "shipped"):
        note = [(
            "ATTENZIONE: lo stato registrato dice spedito al cliente, ma la "
            "DATA DI PARTENZA NON RISULTA DA NESSUNA DELLE DUE FONTI (né nel "
            "registro invii Fully"
            + (f", che copre solo dal {copertura_dal}," if copertura_dal else ",")
            + " né nei campi logistici dell'ordine). Niente data, niente "
            "tracking: dillo apertamente, senza dedurre quando sia partito."
        )]
        return None, None, note
    # Ordine non spedito e nessuna riga: nessun blocco, e l'assenza di riga NON
    # è una prova in nessuna direzione.
    return None, None, []


def _fully_traccia_ordine(o: dict, spedizioni: list, righe_recon: list,
                          righe_lri: list, rep_per_asn: dict = None,
                          outb_per_num: dict = None,
                          outb_per_foid: dict = None,
                          outb_copertura_dal: str = None) -> dict:
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

    # Ripartenza verso il cliente: fonte primaria il registro invii Fully,
    # fallback i campi logistics_* dell'ordine, sempre con la fonte dichiarata.
    riga_out = (outb_per_num or {}).get(_fully_norm_num(num))
    compagni = []
    if isinstance(riga_out, dict) and riga_out.get("fully_order_id"):
        compagni = [
            n for n in (outb_per_foid or {}).get(str(riga_out["fully_order_id"]), [])
            if _fully_norm_num(n) != _fully_norm_num(num)
        ]
    blocco_rip, data_partenza, note_rip = _fully_blocco_ripartenza(
        o, riga_out, compagni, outb_copertura_dal
    )
    if blocco_rip:
        tr["ripartenza_verso_cliente"] = blocco_rip
    for n_extra in note_rip:
        tr["nota_partenza_mancante"] = n_extra

    # Valutazione "pronto o no per il cliente": SOLO composizione di fatti presenti,
    # niente deduzioni dove il dato non c'è.
    if os_code in ("shipped_to_customer", "shipped"):
        if data_partenza:
            fonte_breve = (
                "registro invii Fully"
                if (blocco_rip or {}).get("fonte", "").startswith("registro")
                else "campi logistici dell'ordine"
            )
            val = f"Già spedito al cliente il {data_partenza} ({fonte_breve})."
        else:
            val = (
                "Risulta spedito al cliente (stato registrato sulla piattaforma), "
                "ma la data di partenza non risulta da nessuna fonte."
            )
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

    # Registro invii da Fully verso il cliente: fonte PRIMARIA della ripartenza.
    # Un solo scarico; il filtro order_number della risorsa esiste ed è esatto,
    # ma qui servono anche i compagni di spedizione raggruppata, quindi si
    # scarica tutto e si filtra lato bot come per le altre risorse.
    outbound, err = _fully_rows("fully_outbound")
    if err:
        return err
    outb_per_num = {}
    outb_per_foid = {}
    for r in outbound:
        k = _fully_norm_num(r.get("order_number"))
        if k:
            outb_per_num[k] = r
        foid = r.get("fully_order_id")
        if foid:
            outb_per_foid.setdefault(str(foid), []).append(r.get("order_number"))
    # Copertura del registro: calcolata dai dati, non cablata (oggi 23/06/2026).
    date_reg = [str(r.get("created_at") or "")[:10] for r in outbound if r.get("created_at")]
    outb_copertura_dal = min(date_reg) if date_reg else None

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

    base = {
        "tipo": "tracciamento_fully",
        "piattaforma": "kanokimonos.app (ordini custom). La piattaforma REGISTRA l'ordine: non spedisce e non riceve merce",
        "nota_verifica": _FULLY_NOTA_VERIFICA,
    }

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
                o, _spedizioni_di(o, recon_o), recon_o, lri_o, rep_per_asn,
                outb_per_num, outb_per_foid, outb_copertura_dal
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
                o, _spedizioni_di(o, recon_o), recon_o, lri_o, rep_per_asn,
                outb_per_num, outb_per_foid, outb_copertura_dal
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
        if name == "ordine_fabbrica_per_numero":
            return tool_ordine_fabbrica_per_numero(tool_input.get("numero"))
        if name == "tracciamento_fully":
            return tool_tracciamento_fully(
                tool_input.get("numero"), tool_input.get("cliente")
            )
        if name == "rispondi_dal_manuale":
            return tool_rispondi_dal_manuale(
                tool_input.get("argomento"), user_message, role
            )
        return {"error": f"Strumento sconosciuto: {name}"}
    except Exception as e:
        # Il testo dell'eccezione resta nel log: al modello va una frase che
        # dice "non ho potuto guardare", non un messaggio tecnico da ripetere.
        print(f"[STRUMENTO {name}] eccezione: {e}")
        return {
            "error": (
                "Questo strumento non è riuscito a completare la ricerca per un "
                "problema tecnico. NON è una risposta sul merito: non dire che il "
                "dato non esiste, di' che questa fonte non è consultabile ora."
            ),
            "fonte": name,
        }


def chat_with_tools(chat_id: str, user_message: str, role: str = DEFAULT_ROLE) -> str:
    """Loop tool use: Haiku decide, eseguiamo le funzioni esistenti, Haiku compone."""
    if not ANTHROPIC_API_KEY:
        return "Errore: ANTHROPIC_API_KEY non configurata."

    role = _normalize_role(role)
    active_tools = [t for t in CHAT_TOOLS if t["name"] in ROLE_TOOLS[role]]

    history = get_recent_messages(chat_id)
    system = _compose_system(role)

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
        # Unico punto che parla all'utente SENZA passare dal modello: qui usciva
        # str(e) grezzo (chiavi, quote, stack del client). Dettaglio nel log.
        print(f"[AI] eccezione: {e}")
        return (
            "Non riesco a rispondere in questo momento per un problema tecnico del "
            "servizio. Non è una risposta sulla tua domanda: riprova fra poco."
        )


# --- CHIAVE AMMINISTRATIVA SUGLI ENDPOINT DI SERVIZIO ------------------------
# Gli endpoint che espongono dati (ordini, clienti, listini, manuale) o che
# scrivono sul DB non devono rispondere a chiunque conosca l'URL. Si presenta
# l'header 'x-bot-admin-key' e deve combaciare con BOT_ADMIN_KEY dell'ambiente.
#
# Due scelte deliberate:
# - FAIL CLOSED: se BOT_ADMIN_KEY non è configurata l'endpoint risponde 503 e
#   NON si apre. Una variabile dimenticata deve rendere il servizio muto, mai
#   pubblico.
# - confronto a tempo costante (hmac.compare_digest su byte) così la chiave non
#   si ricostruisce misurando quanto ci mette a rispondere; il confronto su
#   byte evita anche che un header con caratteri non ASCII faccia esplodere il
#   paragone e trasformi un 401 in un 500.
def richiedi_chiave_admin(x_bot_admin_key: str = Header(default=None)):
    if not BOT_ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail="Endpoint amministrativo non configurato (BOT_ADMIN_KEY assente).",
        )
    fornita = (x_bot_admin_key or "").encode("utf-8")
    attesa = BOT_ADMIN_KEY.encode("utf-8")
    if not fornita or not hmac.compare_digest(fornita, attesa):
        raise HTTPException(
            status_code=401,
            detail="Chiave amministrativa mancante o errata (header x-bot-admin-key).",
        )


SOLO_ADMIN = [Depends(richiedi_chiave_admin)]


# Riconoscimento della chiave client (vedi BOT_CLIENT_KEYS in testa). Stesso
# confronto a tempo costante e su byte della chiave amministrativa, e per gli
# stessi motivi. Ritorna il ruolo associato alla chiave, None se la chiave
# manca o non corrisponde a nessuna configurata: dalla fase 3 il None è un
# rifiuto (vedi rifiuta_chiave_client).
def ruolo_da_chiave_client(chiave_fornita):
    if not chiave_fornita:
        return None
    fornita = chiave_fornita.encode("utf-8")
    for ruolo, attesa in BOT_CLIENT_KEYS:
        if attesa and hmac.compare_digest(fornita, attesa.encode("utf-8")):
            return ruolo
    return None


# Esito leggibile per i log [CHAT-KEY]/[FEEDBACK-KEY]: mai la chiave, solo
# presenza e risultato. Formato invariato dalla fase 1, così le righe restano
# confrontabili nel tempo.
def esito_chiave_client(chiave_fornita, ruolo):
    if chiave_fornita is None:
        return "assente"
    if ruolo:
        return f"valida->{ruolo}"
    return "sconosciuta"


# FASE 3: il rifiuto. Il 401 dice solo che l'accesso non è autorizzato — il
# nome dell'header, l'esito e ogni altro dettaglio restano nei log del server,
# mai nella risposta. Caso a parte: se sul server non è configurata NESSUNA
# chiave client, rifiutare con 401 direbbe il falso ("chiave sbagliata" quando
# il problema è nostro); si risponde 503, fail closed come richiedi_chiave_admin
# ma distinguibile nei fatti da una chiave errata.
def rifiuta_chiave_client():
    if not any(attesa for _ruolo, attesa in BOT_CLIENT_KEYS):
        raise HTTPException(
            status_code=503,
            detail="Servizio momentaneamente non disponibile.",
        )
    raise HTTPException(status_code=401, detail="Accesso non autorizzato.")


@app.get("/")
def home():
    return {"status": "BambuUp Bot running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/feedback")
def submit_feedback(request: FeedbackRequest, x_bot_client_key: str = Header(default=None)):
    # Stessa chiave client di /chat (fase 3): senza chiave riconosciuta il
    # feedback non entra nel DB. Qui il ruolo non serve, conta solo che la
    # chiave sia una delle nostre.
    ruolo_da_chiave = ruolo_da_chiave_client(x_bot_client_key)
    esito_chiave = esito_chiave_client(x_bot_client_key, ruolo_da_chiave)
    print(f"[FEEDBACK-KEY] chiave={esito_chiave}")
    if ruolo_da_chiave is None:
        rifiuta_chiave_client()
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


@app.post("/chat")
def chat(request: ChatRequest, x_bot_client_key: str = Header(default=None)):
    # FASE 3 delle chiavi client: il ruolo lo decide SOLO il server dalla
    # chiave; il 'role' del body non fa più fede in nessun caso. Chi non manda
    # la chiave, o ne manda una sconosciuta, viene rifiutato — ma prima si
    # logga, così anche i rifiuti restano visibili nell'audit.
    ruolo_da_chiave = ruolo_da_chiave_client(x_bot_client_key)
    esito_chiave = esito_chiave_client(x_bot_client_key, ruolo_da_chiave)
    print(f"[CHAT-KEY] chiave={esito_chiave} source={request.source} role_body={request.role}")
    if ruolo_da_chiave is None:
        rifiuta_chiave_client()
    role = ruolo_da_chiave
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
        # role seleziona modalità utente: deciso sopra, solo dalla chiave.
        bot_reply = chat_with_tools(request.chat_id, request.message, role)

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


@app.get("/custom-orders", dependencies=SOLO_ADMIN)
def custom_orders(limit: int = 20):
    try:
        return get_custom_resource("orders", limit)
    except Exception as e:
        return {"error": str(e)}

@app.get("/custom-debug", dependencies=SOLO_ADMIN)
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

@app.get("/custom-order-view", dependencies=SOLO_ADMIN)
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
        
# --- REIMPORT DEL MANUALE: non è più una rotta HTTP ---------------------------
# Prima era GET /import-knowledge, raggiungibile da chiunque senza credenziali,
# e faceva DELETE + INSERT sui chunk del manuale nel DB di produzione: una
# cancella-e-ricarica innescabile da un estraneo. Ora è una funzione: la può
# eseguire solo chi ha già accesso all'ambiente (DATABASE_URL + il docx).
# Il chunking DEVE restare allineato a KNOWLEDGE_CHUNK_OVERLAP, che
# _reconstruct_manuale_text usa per ricucire il testo contiguo.
def reimport_knowledge_from_docx(file_path: str = "manuale_operativo.docx") -> dict:
    """Ricarica il manuale nel DB: legge il docx, lo divide in chunk e sostituisce
    i documenti category='manuale'. Richiede DATABASE_URL e il file docx nella
    stessa macchina. Da riga di comando:
        python -c "import main; print(main.reimport_knowledge_from_docx())"
    """
    full_text = extract_text_from_docx(file_path)

    if not full_text:
        return {"error": "Nessun testo estratto dal documento"}

    chunk_size = 4000
    overlap = KNOWLEDGE_CHUNK_OVERLAP
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


# Riesposta come rotta PROTETTA: Bambu deve poter ricaricare il manuale dopo
# ogni revisione del docx senza dipendere da una shell su Render. Resta una
# operazione distruttiva (cancella e riscrive i chunk del manuale), quindi vive
# solo dietro la chiave amministrativa.
@app.get("/import-knowledge", dependencies=SOLO_ADMIN)
def import_knowledge():
    try:
        return reimport_knowledge_from_docx()
    except Exception as e:
        return {"error": str(e)}


@app.get("/search-knowledge", dependencies=SOLO_ADMIN)
def search_knowledge(q: str, limit: int = 10, consegna: int = 0):
    """Sonda di verifica del retrieval: CHIAMA la stessa cerca_righe_manuale
    del bot (prima aveva l'algoritmo ricopiato dentro, e le due copie erano
    gia' divergenti: taglio a 10 contro 20). Il taglio e' un parametro.
    Con consegna=1 restituisce la CONSEGNA COMPLETA di get_knowledge_context
    (sezioni + appendice), cioe' esattamente cio' che il modello riceve."""
    try:
        if consegna:
            return {"query": q, "consegna": get_knowledge_context(q)}
        righe = cerca_righe_manuale(q, max_matches=max(1, min(limit, 50)))
        if not righe and _INDICE_MANUALE["firma"] is None:
            return {"result": "no knowledge"}
        return {"query": q, "matches": righe}
    except Exception as e:
        return {"error": str(e)}
