# -*- coding: utf-8 -*-
"""Connettore Shopify -> Fully: costruisce e controlla il payload di un ordine.

SOLA LETTURA su Shopify. Nessun invio: il payload lo spedisce chi chiama (vedi send_staging nel cmd 97).
Formato: OpenAPI Fully 2.0.0-beta, schema OrderCreateRequest (backup/fully_api_spec_24_09_26.txt).
"""
import json, os, re, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SHOP_ID = 721                 # shop Fully "Kano Kimonos API" (727 = ordini importati da WooCommerce)
CURRENCY = "EUR"
# paesi dove Shopify chiede la provincia: qui delivery_state deve essere pieno.
# Negli altri (es. DE) Fully accetta "" (spec: "send \"\" for countries without a state part").
STATE_REQUIRED = {"IT", "ES", "US", "CA", "IE"}

# corriere Fully dal SOLO paese di consegna (cmd 131). carrier_map.json non si usa piu' (copia in backup/).
CARRIER_RULE = json.load(open(os.path.join(HERE, "carrier_rule.json"), encoding="utf-8"))
CARRIERS_AMMESSI = {CARRIER_RULE["IT"], CARRIER_RULE["UE"]["carrier"], CARRIER_RULE["altri"]}

ORDER_FIELDS = """id name createdAt currencyCode email phone taxesIncluded cancelledAt tags note
  displayFinancialStatus displayFulfillmentStatus
  billingAddress{ phone }
  customer{ defaultPhoneNumber{ phoneNumber } defaultAddress{ phone } }
  totalPriceSet{ shopMoney{ amount currencyCode } }
  currentTotalPriceSet{ shopMoney{ amount } }
  shippingLine{ title originalPriceSet{ shopMoney{ amount } } }
  shippingAddress{ name firstName lastName company address1 address2 city zip provinceCode countryCodeV2 phone }
  lineItems(first:100){ nodes{ sku name quantity currentQuantity
    discountedUnitPriceAfterAllDiscountsSet{ shopMoney{ amount } }
    taxLines{ rate priceSet{ shopMoney{ amount } } }
    customAttributes{ key value }
    variant{ id sku price compareAtPrice bundle: metafield(namespace:"simple_bundles", key:"bundled_variants"){ value } } } }"""
ORDER_QUERY = "query($id:ID!){ order(id:$id){ %s } }" % ORDER_FIELDS

LAST_ORDERS_QUERY = """query($n:Int!){ orders(first:$n, reverse:true, sortKey:CREATED_AT){ nodes{ id } } }"""


def origin_for(order_name):
    """Origin dell'ordine in Fully. UNICA fonte: la usano connettore_fully, ritorno_fully e sync_giacenze."""
    return "SHOP-" + order_name.lstrip("#")


def phone_for(order):
    """Telefono del destinatario: spedizione -> ordine -> fatturazione -> cliente. "" se manca ovunque."""
    c = order.get("customer") or {}
    for v in ((order.get("shippingAddress") or {}).get("phone"), order.get("phone"),
              (order.get("billingAddress") or {}).get("phone"),
              (c.get("defaultPhoneNumber") or {}).get("phoneNumber"), (c.get("defaultAddress") or {}).get("phone")):
        if v and str(v).strip():
            return str(v).strip()
    return ""


# cmd 240: prefisso internazionale sul telefono per Fully. Con phonenumbers se c'e', altrimenti questa tabella.
try:
    import phonenumbers as PN
except ImportError:
    PN = None
PREFISSI = {"IT": "39", "SM": "378", "VA": "39", "ES": "34", "PT": "351", "DE": "49", "FR": "33", "AT": "43",
            "BE": "32", "NL": "31", "LU": "352", "IE": "353", "GB": "44", "CH": "41", "LI": "423", "MC": "377",
            "SI": "386", "HR": "385", "HU": "36", "SK": "421", "CZ": "420", "PL": "48", "RO": "40", "BG": "359",
            "GR": "30", "CY": "357", "MT": "356", "DK": "45", "SE": "46", "FI": "358", "NO": "47", "EE": "372",
            "LV": "371", "LT": "370", "US": "1", "CA": "1", "AU": "61", "PK": "92"}
SENZA_TRONCO = {"IT", "SM", "VA", "GR", "ES", "PT", "DK", "NO", "MT", "CY", "LU", "MC", "US", "CA", "PL", "CZ", "SK", "EE", "LV"}  # lo 0 iniziale resta (o non esiste)


def phone_intl(raw, country):
    """Telefono con prefisso internazionale. Toglie spazi . - ( ) /; 00 -> +; + resta; altrimenti
    prefisso del paese di SPEDIZIONE (senza lo 0 nazionale dove va tolto, mai per l'Italia);
    se inizia gia' col prefisso del paese senza +, aggiunge solo il +. Paese sconosciuto -> numero solo ripulito."""
    s = re.sub(r"[\s.\-()/]", "", str(raw or ""))
    if not s:
        return ""
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        return s
    country = (country or "").upper()
    if PN:
        cands = []
        try:
            cc = str(PN.country_code_for_region(country)) if country else ""
        except Exception:
            cc = ""
        if cc and s.startswith(cc):
            cands.append("+" + s)                       # gia' col prefisso, manca solo il +
        if country:
            cands.append((s, country))                  # numero nazionale
        for c in cands:
            try:
                n = PN.parse(*c) if isinstance(c, tuple) else PN.parse(c, None)
            except Exception:
                continue
            if PN.is_valid_number(n):
                return PN.format_number(n, PN.PhoneNumberFormat.E164)
    cc = PREFISSI.get(country)
    if not cc:
        return s
    if s.startswith(cc) and len(s) > len(cc) + 8:
        return "+" + s
    if s.startswith("0") and country not in SENZA_TRONCO:
        s = s[1:]
    return "+" + cc + s


def zone_for(country):
    """Gruppo della regola corrieri: IT / UE / altri."""
    if not country:
        return None
    if country == "IT":
        return "IT"
    return "UE" if country in CARRIER_RULE["UE"]["paesi"] else "altri"


def carrier_for(country, rate_title=None):
    """Ritorna (corriere_fully, gruppo, nota). rate_title e' ignorato: conta solo il paese."""
    g = zone_for(country)
    if not g:
        return None, None, "paese di consegna mancante"
    return (CARRIER_RULE["IT"] if g == "IT" else CARRIER_RULE["UE"]["carrier"] if g == "UE"
            else CARRIER_RULE["altri"]), g, None


def _money(x):
    return round(float(x), 2)


def net_unit(li, taxes_included=True):
    """Prezzo per pezzo SENZA IVA (dopo gli sconti). Se i prezzi Shopify includono l'IVA la toglie
    con le taxLines della riga (IVA della riga / quantita); se non la includono il prezzo e' gia' netto."""
    qty = int(li["quantity"]) or 1
    unit = float(li["discountedUnitPriceAfterAllDiscountsSet"]["shopMoney"]["amount"])
    if not taxes_included:
        return _money(unit)
    tax = sum(float(t["priceSet"]["shopMoney"]["amount"]) for t in (li.get("taxLines") or []))
    return _money(unit - tax / qty)


def expand_line(li, unit=None):
    """Ritorna [(sku, nome, quantita, prezzo_per_pezzo)] e note. unit: prezzo per pezzo da usare
    (default: quello scontato IVA inclusa).
    Bundle Simple Bundles: variante senza SKU con metafield simple_bundles.bundled_variants ->
    si mandano i COMPONENTI; il prezzo pagato si divide in proporzione al prezzo dei componenti.
    cmd 296: quantita' = currentQuantity (ordini modificati: quantity resta quella originale)."""
    qty = int(li["currentQuantity"])
    if unit is None:
        unit = _money(li["discountedUnitPriceAfterAllDiscountsSet"]["shopMoney"]["amount"])
    var = li.get("variant") or {}
    raw = (var.get("bundle") or {}).get("value")
    if raw:
        comps = json.loads(raw)
        base = sum(float(c["price"]) * int(c.get("quantity_in_bundle") or 1) for c in comps) or 1.0
        out, assigned = [], 0.0
        for i, c in enumerate(comps):
            q = int(c.get("quantity_in_bundle") or 1)
            if i < len(comps) - 1:
                p = round(unit * float(c["price"]) / base, 2)
                assigned += p * q
            else:  # l'ultimo componente prende il resto: la somma resta uguale al prezzo pagato
                p = round((unit - assigned) / q, 2)
            out.append(((c.get("sku") or "").strip(), "%s - %s" % (c.get("product_title"), c.get("variant_title")), qty * q, p))
        return out, ["bundle %r espanso in %d componenti" % (li["name"], len(comps))]
    return [((li.get("sku") or "").strip(), li["name"], qty, unit)], []


def list_unit(li):
    """cmd 230: prezzo di listino della variante su Shopify (se manca, compareAtPrice). None se mancano entrambi."""
    var = li.get("variant") or {}
    for v in (var.get("price"), var.get("compareAtPrice")):
        if v not in (None, "") and float(v) > 0:
            return _money(v)
    return None


def build(order, origin=None):
    """order: dict con la forma di ORDER_QUERY. Ritorna {"payload", "errori", "note"}."""
    err, note = [], []
    a = order.get("shippingAddress") or {}
    country = (a.get("countryCodeV2") or "").upper()
    rate = (order.get("shippingLine") or {}).get("title")
    carrier, zone, cnote = carrier_for(country, rate)
    if not carrier:
        err.append("corriere: " + cnote)
    elif cnote:
        note.append("corriere %r per %s: %s" % (carrier, country, cnote))
    products = []
    total = _money(order["currentTotalPriceSet"]["shopMoney"]["amount"])   # cmd 296: totale dopo le modifiche dell'ordine
    # cmd 230: ordine a totale 0 (cambi) -> ogni riga col prezzo di listino della variante; gli altri ordini invariati
    listino = total == 0
    if listino:
        note.append("ordine a totale 0: prezzi di listino delle varianti (cmd 230)")
    for li in order["lineItems"]["nodes"]:
        if int(li["currentQuantity"]) == 0:   # cmd 296: riga tolta con una modifica dell'ordine
            continue
        if listino:
            unit = list_unit(li)
            if unit is None:
                err.append("prezzo di listino mancante su %r" % li["name"])
                unit = 0.0
        else:
            unit = net_unit(li, order.get("taxesIncluded", True))
        rows, n = expand_line(li, unit)
        note += n
        for sku, name, q, p in rows:
            if not sku:
                err.append("riga senza SKU: %r" % name)
            if q <= 0:
                err.append("quantita non valida su %r" % name)
            products.append({"sku": sku, "quantity": q, "price_per_piece": p, "_name": name})
    name = a.get("name") or " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x)
    company = (a.get("company") or "").strip()
    delivery_name = ("%s - %s" % (company, name) if name else company) if company else name
    addr1 = (a.get("address1") or "").strip()
    state = (a.get("provinceCode") or "").strip()
    if country in STATE_REQUIRED and not state:
        err.append("provincia mancante (obbligatoria per %s)" % country)
    if not re.search(r"\d", addr1):
        err.append("numero civico mancante in %r" % addr1)
    if not delivery_name:
        err.append("nome destinatario mancante")
    if not phone_for(order):
        err.append("telefono mancante")
    if listino:   # cmd 230: il totale per Fully e' la somma delle righe a listino
        total = _money(sum(x["quantity"] * x["price_per_piece"] for x in products))
    if total <= 0:
        err.append("totale non positivo")
    cur = order["totalPriceSet"]["shopMoney"].get("currencyCode") or order.get("currencyCode")
    if cur != CURRENCY:
        err.append("valuta %s diversa da EUR" % cur)
    created = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "origin": origin or origin_for(order["name"]),
        "shop_id": SHOP_ID,
        "source_ref": order["name"],
        "requested_carrier": carrier or "",
        "cash_on_delivery": 0,
        "currency": CURRENCY,
        "order_total": total,
        "created_date": created,
        "delivery_name": delivery_name,
        "email": order.get("email") or "",
        "phone": phone_intl(phone_for(order), country),   # cmd 240
        "delivery_address": addr1,
        "delivery_address_2": (a.get("address2") or "").strip(),
        "delivery_city": (a.get("city") or "").strip(),
        "delivery_state": state,
        "delivery_zip": (a.get("zip") or "").strip(),
        "delivery_country": country,
        "products": products,
    }
    return {"payload": payload, "errori": err, "note": note}


def for_fully(payload):
    """Solo i campi dello schema OrderCreateRequest (toglie i campi interni _name)."""
    p = dict(payload)
    p["products"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in payload["products"]]
    return p


def masked(payload):
    p = json.loads(json.dumps(payload))
    for k in ("delivery_name", "email", "phone"):
        if p.get(k):
            p[k] = "<nascosto>"
    return p


def fetch_order(gid):
    from _seo_lib import gql
    return gql(ORDER_QUERY, {"id": gid})["order"]


def last_orders(n=3):
    from _seo_lib import gql
    return [fetch_order(o["id"]) for o in gql(LAST_ORDERS_QUERY, {"n": n})["orders"]["nodes"]]
