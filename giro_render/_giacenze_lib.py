# -*- coding: utf-8 -*-
"""Funzioni comuni del giro Fully (copia per Render, cmd 289, da ShopifyKano/_giacenze_lib.py).
Shopify: token app Fully Warehouse (write_inventory). Fully: SOLO GET. Nessun token stampato.
cmd 289: niente cartella backup/; le mutation passano da _mut che fuori da GIRO_MODO=acceso scrive solo "AVREI FATTO";
ora delle note e dei log = ora di Roma."""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, ROOT)
from _seo_lib import gql, load_env, giro_modo, scrive_davvero, avrei_fatto, roma_str

ENV = load_env()
SHOP = "tipjd7-r0.myshopify.com"
LOC = "gid://shopify/Location/117507064190"          # sede Fully
_TOK = None

def fw_token():
    """Token client_credentials dell'app Fully Warehouse."""
    global _TOK
    if _TOK: return _TOK
    body = json.dumps({"client_id": ENV["FULLY_SHOPIFY_CLIENT_ID"], "client_secret": ENV["FULLY_SHOPIFY_CLIENT_SECRET"],
                       "grant_type": "client_credentials"}).encode()
    r = urllib.request.Request("https://%s/admin/oauth/access_token" % SHOP, data=body,
                               headers={"Content-Type": "application/json"}, method="POST")
    _TOK = json.loads(urllib.request.urlopen(r, timeout=60).read().decode())["access_token"]
    return _TOK

def q(query, variables=None):
    return gql(query, variables, token=fw_token())

def stamp():
    return roma_str("%Y%m%d_%H%M%S")

# ---------- Shopify: varianti ACTIVE con giacenza sulla sede Fully ----------
_QV = """query($c:String){ productVariants(first:200, after:$c, query:"product_status:active"){
  pageInfo{ hasNextPage endCursor }
  nodes{ id sku title requiresComponents productVariantComponents(first:1){ nodes{ id } }
    sb: metafield(namespace:"simple_bundles", key:"bundled_variants"){ value }
    product{ title handle hasVariantsThatRequiresComponents }
    inventoryItem{ id tracked inventoryLevel(locationId:"%s"){ quantities(names:["available"]){ name quantity } } } } } }""" % LOC

_QN = """query($ids:[ID!]!){ nodes(ids:$ids){ ... on ProductVariant{ id sku title requiresComponents productVariantComponents(first:1){ nodes{ id } }
    sb: metafield(namespace:"simple_bundles", key:"bundled_variants"){ value }
    product{ title handle status hasVariantsThatRequiresComponents }
    inventoryItem{ id tracked inventoryLevel(locationId:"%s"){ quantities(names:["available"]){ name quantity } } } } } }""" % LOC


def _row(n, componente=False):
    ii = n["inventoryItem"] or {}
    lv = ii.get("inventoryLevel")
    av = next((x["quantity"] for x in (lv or {}).get("quantities", []) if x["name"] == "available"), None)
    sbv = (n.get("sb") or {}).get("value")
    bundle = bool(n.get("requiresComponents") or n["productVariantComponents"]["nodes"] or
                  (sbv and sbv.strip() not in ("", "[]")) or n["product"].get("hasVariantsThatRequiresComponents"))
    return dict(variant_id=n["id"], sku=(n["sku"] or "").strip(), prodotto=n["product"]["title"],
                variante=n["title"], inventory_item=ii.get("id"), tracked=ii.get("tracked"),
                su_sede=lv is not None, available=av, bundle=bundle, componente=componente,
                stato=n["product"].get("status", "ACTIVE"), _sb=sbv)


def read_variants(con_componenti=False):
    """Varianti dei prodotti ACTIVE. Con con_componenti=True (cmd 170) aggiunge le varianti componenti dei bundle
    citate nel metafield simple_bundles.bundled_variants delle varianti bundle attive, anche se il loro prodotto e'
    in bozza (es. ARASHI 2.0 BJJ Gi Jacket White / ARASHI 2.0 Pants White BJJ). Solo quelle citate, nessun'altra."""
    rows, c = [], None
    while True:
        d = q(_QV, {"c": c})["productVariants"]
        rows += [_row(n) for n in d["nodes"]]
        if not d["pageInfo"]["hasNextPage"]: break
        c = d["pageInfo"]["endCursor"]
    if con_componenti:
        have = {r["variant_id"] for r in rows}
        want = []
        for r in rows:
            if r["bundle"] and r["_sb"]:
                for comp in json.loads(r["_sb"]):
                    gid = "gid://shopify/ProductVariant/%s" % comp["variant_id"]
                    if gid not in have and gid not in want: want.append(gid)
        for i in range(0, len(want), 100):
            for n in q(_QN, {"ids": want[i:i + 100]})["nodes"]:
                if n: rows.append(_row(n, componente=True))
    for r in rows: r.pop("_sb", None)
    return rows

# ---------- Shopify: ordini aperti (non annullati, non evasi) ----------
# cmd 222: 250 righe per ordine (prima 100) + paginazione delle righe: nessun ordine letto a meta'
_LI = """nodes{ sku name unfulfilledQuantity
      variant{ sku bundle: metafield(namespace:"simple_bundles", key:"bundled_variants"){ value } } }"""
_QO = """query($c:String){ orders(first:10, after:$c, query:"status:open"){
  pageInfo{ hasNextPage endCursor }
  nodes{ id name cancelledAt displayFulfillmentStatus
    lineItems(first:250){ pageInfo{ hasNextPage endCursor } %s } } } }""" % _LI
_QLI = """query($id:ID!, $c:String){ order(id:$id){ lineItems(first:250, after:$c){ pageInfo{ hasNextPage endCursor } %s } } }""" % _LI


def _tutte_le_righe(o):
    """Righe dell'ordine, tutte: se la prima pagina non basta legge le successive."""
    righe, pg = list(o["lineItems"]["nodes"]), o["lineItems"]["pageInfo"]
    while pg["hasNextPage"]:
        li = q(_QLI, {"id": o["id"], "c": pg["endCursor"]})["order"]["lineItems"]
        righe += li["nodes"]; pg = li["pageInfo"]
    return righe

def read_open_orders():
    """Ritorna [(name, {sku: pezzi_non_evasi})]; i bundle Simple Bundles sono espansi nei componenti."""
    out, c = [], None
    while True:
        d = q(_QO, {"c": c})["orders"]
        for o in d["nodes"]:
            if o["cancelledAt"] or o["displayFulfillmentStatus"] == "FULFILLED":
                continue
            pezzi = {}
            for li in _tutte_le_righe(o):
                n = int(li["unfulfilledQuantity"] or 0)
                if n <= 0: continue
                raw = ((li.get("variant") or {}).get("bundle") or {}).get("value")
                if raw:
                    for comp in json.loads(raw):
                        s = (comp.get("sku") or "").strip()
                        k = n * int(comp.get("quantity_in_bundle") or 1)
                        pezzi[s] = pezzi.get(s, 0) + k
                else:
                    s = (li.get("sku") or "").strip()
                    pezzi[s] = pezzi.get(s, 0) + n
            if pezzi:
                out.append((o["name"], pezzi))
        if not d["pageInfo"]["hasNextPage"]: return out
        c = d["pageInfo"]["endCursor"]

# ---------- Fully: SOLO GET ----------
def fully_get(path, params=None):
    url = "https://api.fully.si/api/v2-jwt/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    r = urllib.request.Request(url, method="GET")  # SOLO GET
    r.add_header("Authorization", "Bearer " + ENV["FULLY_PRODUCTION_TOKEN"])
    r.add_header("Accept", "application/json"); r.add_header("User-Agent", "kano-readonly-audit/1.0")
    for a in range(3):
        try:
            return json.loads(urllib.request.urlopen(r, timeout=120).read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and a < 2: time.sleep(4); continue
            raise RuntimeError("Fully HTTP %s su %s" % (e.code, path))

def fully_products():
    """{default_code: [prodotti]} di tutti i prodotti Fully."""
    allp, off = [], 0
    while True:
        d = fully_get("product.product", {"limit": 250, "offset": off})
        b = d.get("products") or []
        allp.extend(b); off += 250
        tot = d.get("filtered_count", d.get("total_count"))
        if not b or (tot is not None and len(allp) >= tot): break
    by = {}
    for p in allp:
        k = (p.get("default_code") or "").strip()
        if k: by.setdefault(k, []).append(p)
    return by

# stati Fully in cui l'ordine NON impegna merce. api_error NON c'e': mette merce in outgoing_qty (verificato cmd 136)
NON_IMPEGNA = {"cancelled"}


def fully_order_status(origin):
    """Stato dell'ordine Fully con quell'origin, o None se non esiste."""
    d = fully_get("stock.picking/orders", {"origin": origin, "limit": 5})
    o = next((x for x in (d.get("orders") or []) if (x.get("origin") or "") == origin), None)
    return (o.get("status") or "?") if o else None


def fully_has_origin(origin):
    return fully_order_status(origin) is not None


def fully_impegna(origin):
    """True se l'ordine e' in Fully in uno stato che impegna la merce."""
    st = fully_order_status(origin)
    return st is not None and st not in NON_IMPEGNA

def connector_origin(order_name):
    """Origin preso direttamente dal connettore: fully_connector/build_order.origin_for."""
    sys.path.insert(0, os.path.join(ROOT, "fully_connector"))
    import build_order
    return build_order.origin_for(order_name)


# ---------- etichette sugli ordini Shopify (tag + riga di nota) ----------
TAG_OK, TAG_ERR = "FULLY-INVIATO", "FULLY-ERRORE"
_M_TAGS = """mutation($id:ID!, $tags:[String!]!%s){ tagsAdd(id:$id, tags:$tags)%s{ userErrors{ field message } } }"""
_M_NOTE = """mutation($input:OrderInput!%s){ orderUpdate(input:$input)%s{ order{ id } userErrors{ field message } } }"""


def _mut(m, v, cosa=None):
    """Esegue la mutation; se Shopify chiede @idempotent la ripete con una chiave nuova.
    cmd 289: fuori da GIRO_MODO=acceso non scrive: riga "AVREI FATTO: <cosa>" e risposta finta senza userErrors."""
    import re, uuid
    if not scrive_davvero():
        campo = re.search(r"\{\s*(\w+)\(", m).group(1)          # tagsAdd / tagsRemove / orderUpdate
        avrei_fatto(cosa or "%s %s" % (campo, json.dumps(v, ensure_ascii=False)[:200]))
        return {campo: {"userErrors": []}}
    try:
        return q(m % ("", ""), v)
    except RuntimeError as e:
        if "@idempotent" not in str(e):
            raise
        return q(m % (", $key:String!", " @idempotent(key:$key)"), dict(v, key=str(uuid.uuid4())))


def etichetta(order, tag, riga_nota):
    """Aggiunge il tag e accoda una riga alla nota dell'ordine (la nota esistente resta).
    order: dict con id e note. Ritorna lista di errori (vuota se ok)."""
    err = []
    chi = order.get("name") or order["id"]
    d = _mut(_M_TAGS, {"id": order["id"], "tags": [tag]}, "tagsAdd %s su %s" % (tag, chi))
    err += [e["message"] for e in d["tagsAdd"]["userErrors"]]
    nota = ((order.get("note") or "").rstrip() + "\n" if order.get("note") else "") + "[%s] %s %s" % (
        roma_str("%d/%m/%Y %H:%M"), tag, riga_nota)
    d = _mut(_M_NOTE, {"input": {"id": order["id"], "note": nota}}, "orderUpdate nota di %s: riga [%s] %s %s" % (
        chi, roma_str("%d/%m/%Y %H:%M"), tag, riga_nota[:200]))
    err += [e["message"] for e in d["orderUpdate"]["userErrors"]]
    return err


def log(msg):
    """Riga per il log del giro automatico (giro_fully.py raccoglie le righe che iniziano con LOG:).
    cmd 289: fuori da GIRO_MODO=acceso la riga inizia con [prova] (nessuna scrittura e' avvenuta)."""
    print("LOG: " + ("" if scrive_davvero() else "[%s] " % giro_modo()) + msg, flush=True)
