# -*- coding: utf-8 -*-
"""Connettore ordini Shopify -> Fully (shop 721 "Kano Kimonos API").

Seleziona ordini Shopify PAGATI, non annullati, non evasi (UNFULFILLED o IN_PROGRESS "in preparazione"),
non ancora in Fully (origin SHOP-<numero>).
Anche NON pagati (PENDING o PARTIALLY_PAID) se hanno il tag manuale esatto SPEDITO-NON-PAGATO (cmd 210):
stessi controlli dei pagati; il tag lo toglie tag_spedito_non_pagato.py quando l'ordine diventa PAID.
Payload da fully_connector/build_order.py (corriere da carrier_rule.json in base al paese, prezzi SENZA IVA).
Validazione completa prima di qualsiasi invio: un errore -> l'ordine non parte e viene elencato.
Corriere dal paese di consegna (fully_connector/carrier_rule.json): IT -> GLS Italia, UE -> GLS Slovenia,
altri -> Fedex Connect Plus. Ammessi SOLO questi tre nomi (build_order.CARRIERS_AMMESSI).
Origin gia' presente in Fully, in QUALSIASI stato -> mai reinviato (cancelled = "da gestire a mano").
cmd 231: gia' presente ma api_error o hold -> FULLY-ERRORE "gia' in Fully (shop X, id Y) ma in errore: ..." (non FULLY-INVIATO).
cmd 256 (06/10/26, ritorno al nostro invio dopo l'app "Fully - KanoKimonos" shop 883): anti-doppio con copie_fully(),
  cioe' TUTTE le copie dell'ordine su TUTTI gli shop (origin SHOP-<n>, origin/source_ref/source_id/nome = nome ordine).
  1 copia attiva (non cancelled) -> mai inviare: in errore/hold -> FULLY-ERRORE, altrimenti FULLY-INVIATO (come il 231);
  2+ copie attive -> FULLY-ERRORE "DOPPIONE in Fully: id ..."; solo copie annullate -> FULLY-ERRORE "unica copia annullata",
  non inviato (da gestire a mano). Prima (fully_gia_presente) si guardava solo la PRIMA copia trovata.
  Versione precedente: backup/giro_pre_243_06_10_26/connettore_fully.py.

Etichette sull'ordine Shopify (solo in "auto" e "invia"):
  inviato -> tag FULLY-INVIATO + nota "Fully id <id>"
  bloccato (validazione, invio fallito, api_error in risposta) -> tag FULLY-ERRORE + nota col motivo
  ordini con FULLY-ERRORE non vengono ritentati finche' il tag non viene tolto a mano.

Uso:  python connettore_fully.py              -> PROVA A SECCO su tutti gli ordini selezionati
      python connettore_fully.py prova <N>    -> PROVA A SECCO forzata sull'ordine N (anche se annullato/non pagato)
      python connettore_fully.py invia <N>    -> invia SOLO l'ordine N (max 2 tentativi)
      python connettore_fully.py auto         -> invia TUTTI i selezionati, max 10 per giro (usato da giro_fully.py)
cmd 289 (copia per Render): nessun backup su file; fuori da GIRO_MODO=acceso il POST a Fully e le etichette
diventano righe "AVREI FATTO" nel log (esito "prova: sarebbe stato inviato").
"""
import json, os, re, sys, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "fully_connector"))
import build_order as B
from _giacenze_lib import q, fully_products, fully_get, stamp, ENV, etichetta, log, TAG_OK, TAG_ERR
from _seo_lib import avrei_fatto

FULLY_POST = "https://api.fully.si/api/v2-jwt/stock.picking/orders"
TENTATIVI = 2
MAX_AUTO = 10
Q_SEL = """query($c:String){ orders(first:25, after:$c,
  query:"status:open financial_status:paid -fulfillment_status:shipped -tag:FULLY-ERRORE -tag:FULLY-INVIATO"){
  pageInfo{ hasNextPage endCursor } nodes{ %s } } }""" % B.ORDER_FIELDS
# cmd 210: non pagati col tag manuale SPEDITO-NON-PAGATO partono come i pagati (tag esatto controllato in selezionabile)
TAG_SNP = "SPEDITO-NON-PAGATO"
NON_PAGATI_OK = ("PENDING", "PARTIALLY_PAID")
Q_SNP = """query($c:String){ orders(first:25, after:$c,
  query:"status:open tag:'%s' -financial_status:paid -fulfillment_status:shipped -tag:FULLY-ERRORE -tag:FULLY-INVIATO"){
  pageInfo{ hasNextPage endCursor } nodes{ %s } } }""" % (TAG_SNP, B.ORDER_FIELDS)
Q_NAME = """query($q:String!){ orders(first:5, query:$q){ nodes{ %s } } }""" % B.ORDER_FIELDS


# cmd 218 f2: anti-doppione su TUTTI gli shop Fully (anche ordini creati dall'app "Fully - KanoKimonos",
# che puo' usare un origin diverso dal nostro SHOP-<numero>)
_TUTTI_FULLY = None


def tutti_fully():
    """Tutti gli ordini Fully di tutti gli shop (letti una volta per esecuzione)."""
    global _TUTTI_FULLY
    if _TUTTI_FULLY is None:
        _TUTTI_FULLY, off = [], 0
        while True:
            d = fully_get("stock.picking/orders", {"limit": 250, "offset": off, "sort_by": "id desc"})
            blocco = d.get("orders") or []
            _TUTTI_FULLY += blocco
            off += 250
            if len(blocco) < 250 or off >= (d.get("total_count") or 0): break
    return _TUTTI_FULLY


def copie_fully(nome):
    """cmd 256: TUTTE le copie Fully (qualsiasi shop e stato) dell'ordine Shopify `nome` (es. 1044-W26).
    Cerca il nome dentro origin/source_ref/source_id/name: trova SHOP-1044-W26 (nostro, shop 721) e 1044-W26 (app, shop 883)."""
    nome = nome.lstrip("#")
    pat = re.compile(r"(^|[^0-9A-Za-z])#?%s($|[^0-9A-Za-z])" % re.escape(nome), re.I)
    return [x for x in tutti_fully() if any(pat.search(str(x.get(k) or "")) for k in ("origin", "source_ref", "source_id", "name"))]


def _desc(x):
    return "%s (shop %s, %s%s)" % (x.get("id"), x.get("shop_id"), x.get("status"), ", hold" if x.get("hold") else "")


def corrieri_721():
    """requested_carrier degli ordini SPEDITI dello shop 721 (nomi che Fully accetta davvero)."""
    nomi, off = {}, 0
    while True:
        d = fully_get("stock.picking/orders", {"limit": 250, "offset": off, "sort_by": "id desc"})
        b = d.get("orders") or []
        for o in b:
            rc = o.get("requested_carrier")
            if o.get("shop_id") == 721 and o.get("status") == "shipped" and rc:
                nomi[rc] = nomi.get(rc, 0) + 1
        off += 250
        if not b or off >= (d.get("total_count") or 0): return nomi


def selezionabile(o):
    """Motivo per cui l'ordine NON e' selezionabile, o None."""
    if o.get("cancelledAt"): return "annullato"
    if TAG_ERR in (o.get("tags") or []): return "tag %s (da sbloccare a mano)" % TAG_ERR
    fin = o.get("displayFinancialStatus")
    if fin != "PAID" and not (fin in NON_PAGATI_OK and TAG_SNP in (o.get("tags") or [])):
        return "non pagato (%s)" % fin
    if o.get("displayFulfillmentStatus") not in ("UNFULFILLED", "IN_PROGRESS"):   # IN_PROGRESS = "in preparazione"
        return "gia' evaso o in parte (%s)" % o.get("displayFulfillmentStatus")
    return None


def valida(o, fby, corrieri):
    """Costruisce il payload e fa TUTTI i controlli. Ritorna (payload_per_fully, errori, note)."""
    r = B.build(o)
    err, note = list(r["errori"]), list(r["note"])
    p = B.for_fully(r["payload"])
    if p["requested_carrier"] and p["requested_carrier"] not in corrieri:
        err.append("corriere %r non ammesso (ammessi: %s)" % (
            p["requested_carrier"], ", ".join(sorted(corrieri)) or "nessuno"))
    if not p["delivery_country"]: err.append("paese mancante")
    if not p["delivery_city"]: err.append("citta' mancante")
    if not p["delivery_zip"]: err.append("CAP mancante")
    if not p["products"]: err.append("nessuna riga prodotto")
    for x in p["products"]:
        if x["sku"] and x["sku"] not in fby: err.append("SKU %s non esiste in Fully" % x["sku"])
        elif x["sku"] and len(fby[x["sku"]]) > 1: err.append("SKU %s doppio in Fully" % x["sku"])
        elif x["sku"]:
            free = float(fby[x["sku"]][0].get("free_qty") or 0)
            if free < x["quantity"]:
                note.append("SKU %s: free_qty Fully %s < %s richiesti (Fully lo mettera' in backorder)" % (x["sku"], free, x["quantity"]))
        if not isinstance(x["quantity"], int) or x["quantity"] <= 0: err.append("quantita' non valida su %s" % x["sku"])
        if x["price_per_piece"] < 0: err.append("prezzo negativo su %s" % x["sku"])
    return p, err, note


def invia(p):
    if not avrei_fatto("POST Fully ordine %s (origin %s, %d righe, totale %s, corriere %s)" % (
            p.get("source_ref"), p.get("origin"), len(p.get("products") or []), p.get("order_total"), p.get("requested_carrier"))):
        return {"prova": True}
    body = json.dumps(p).encode("utf-8")
    last = None
    for a in range(1, TENTATIVI + 1):
        req = urllib.request.Request(FULLY_POST, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + ENV["FULLY_PRODUCTION_TOKEN"])
        req.add_header("Content-Type", "application/json"); req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as x:
                resp = json.loads(x.read().decode("utf-8"))
                o = resp.get("order") or {}
                return {"tentativo": a, "http": x.status, "success": resp.get("success"), "fully_id": o.get("id"),
                        "status": o.get("status"), "errors": o.get("errors")}
        except urllib.error.HTTPError as e:
            last = {"tentativo": a, "http": e.code, "body": e.read().decode("utf-8", "replace")[:400]}
            if e.code == 400:   # validazione o origin gia' presente: ripetere non serve
                return last
        except Exception as e:
            last = {"tentativo": a, "errore": "%s: %s" % (type(e).__name__, str(e)[:200])}
        print("  tentativo %d fallito: %s" % (a, last))
        time.sleep(5)
    return last


def main():
    args = sys.argv[1:]
    modo = args[0] if args else "secco"
    if modo in ("prova", "invia") and len(args) < 2:
        raise SystemExit("uso: python connettore_fully.py [prova|invia] <numero ordine>")
    scrive = modo in ("invia", "auto")
    t0 = stamp()
    fby = fully_products()
    corrieri = set(B.CARRIERS_AMMESSI)
    print("corrieri ammessi:", ", ".join(sorted(corrieri)))
    if modo in ("secco", "auto"):
        ordini = []
        for qs in (Q_SEL, Q_SNP):
            c = None
            while True:
                d = q(qs, {"c": c})["orders"]
                ordini += [o for o in d["nodes"] if o["id"] not in {x["id"] for x in ordini}]
                if not d["pageInfo"]["hasNextPage"]: break
                c = d["pageInfo"]["endCursor"]
    else:
        nome = args[1].lstrip("#")
        ordini = [o for o in q(Q_NAME, {"q": "name:%s" % nome})["orders"]["nodes"] if o["name"].lstrip("#") == nome]
        if not ordini: raise SystemExit("ordine %s non trovato su Shopify" % nome)
    print("MODO: %s | ordini da esaminare: %d" % (modo, len(ordini)))
    if modo == "auto":
        log("connettore: %d ordini selezionati" % len(ordini))
    rep = {"data": t0, "modo": modo, "ordini": []}
    lavorati = 0
    for o in ordini:
        origin = B.origin_for(o["name"])
        riga = {"ordine": o["name"], "origin": origin}
        motivo = selezionabile(o)
        copie = copie_fully(o["name"])          # cmd 256: tutte le copie, tutti gli shop
        attive = [x for x in copie if x.get("status") != "cancelled"]
        esiste = attive[0] if len(attive) == 1 else None
        if copie:
            riga["fully"] = [{"id": x.get("id"), "status": x.get("status"), "shop": x.get("shop_id"), "origin": x.get("origin"),
                              "hold": x.get("hold")} for x in copie]
        if len(attive) >= 2:   # cmd 256: doppione gia' in Fully -> mai inviare, segnalare
            msg = "DOPPIONE in Fully: id " + ", ".join(_desc(x) for x in attive)
            riga["esito"] = msg + ", non reinviato"
            if scrive and not motivo:
                riga["etichetta"] = etichetta(o, TAG_ERR, msg)
                log("%s %s -> %s" % (o["name"], msg, TAG_ERR))
        elif copie and not attive:   # cmd 256: solo copie annullate -> non inviare, segnalare
            msg = "unica copia in Fully annullata: %s; da gestire a mano, non reinviato" % ", ".join(_desc(x) for x in copie)
            riga["esito"] = "DA GESTIRE A MANO: " + msg
            if scrive and not motivo:
                riga["etichetta"] = etichetta(o, TAG_ERR, msg)
                log("%s %s -> %s" % (o["name"], msg, TAG_ERR))
        elif esiste:   # mai reinviare, qualunque sia lo shop
            st = esiste.get("status")
            if "error" in str(st or "") or esiste.get("hold"):
                # cmd 231: gia' in Fully ma in errore o in hold -> FULLY-ERRORE (parte l'avviso email), mai FULLY-INVIATO
                errore = esiste.get("errors") or ("stato %s" % st)
                if esiste.get("hold"): errore = "%s (hold)" % errore
                msg = "gia' in Fully (shop %s, id %s) ma in errore: %s" % (esiste.get("shop_id"), esiste.get("id"), errore)
                riga["esito"] = msg + ", non reinviato"
                if scrive and not motivo:    # solo ordini che sarebbero partiti: gli altri non si toccano
                    riga["etichetta"] = etichetta(o, TAG_ERR, msg)
                    log("%s %s -> %s" % (o["name"], msg, TAG_ERR))
            else:
                riga["esito"] = "gia' in Fully (shop %s, id %s, %s), non reinviato" % (esiste.get("shop_id"), esiste.get("id"), st)
                if scrive and not motivo:    # solo ordini che sarebbero partiti: gli altri non si toccano
                    riga["etichetta"] = etichetta(o, TAG_OK, "Fully id %s (gia' presente, shop %s, origin %s)" % (
                        esiste.get("id"), esiste.get("shop_id"), esiste.get("origin")))
                    log("%s gia' in Fully (shop %s, id %s), non reinviato -> %s" % (o["name"], esiste.get("shop_id"), esiste.get("id"), TAG_OK))
        elif motivo and modo != "prova":
            riga["esito"] = "saltato: " + motivo
        elif modo == "auto" and lavorati >= MAX_AUTO:
            riga["esito"] = "rimandato al prossimo giro (max %d)" % MAX_AUTO
        else:
            lavorati += 1
            p, err, note = valida(o, fby, corrieri)
            riga.update({"errori": err, "note": note, "selezione": motivo or "ok",
                         "payload": p if scrive else B.masked(p)})
            if err:
                riga["esito"] = "NON parte: validazione"
                if scrive:
                    riga["etichetta"] = etichetta(o, TAG_ERR, "validazione: " + "; ".join(err))
                    log("%s bloccato (validazione: %s) -> %s" % (o["name"], "; ".join(err), TAG_ERR))
            elif scrive:
                inv = invia(p)
                riga["invio"] = inv or {}
                if (inv or {}).get("prova"):     # cmd 289: GIRO_MODO diverso da acceso, niente inviato, niente etichetta
                    riga["esito"] = "prova: sarebbe stato inviato"
                    log("%s pronto per Fully (prova, non inviato)" % o["name"])
                elif (inv or {}).get("success") and inv.get("status") != "api_error":
                    riga["esito"] = "inviato"
                    riga["etichetta"] = etichetta(o, TAG_OK, "Fully id %s" % inv.get("fully_id"))
                    log("%s inviato -> Fully id %s (%s) -> %s" % (o["name"], inv.get("fully_id"), inv.get("status"), TAG_OK))
                else:
                    msg = (("Fully id %s api_error: %s" % (inv.get("fully_id"), inv.get("errors"))) if (inv or {}).get("success")
                           else "invio fallito: %s" % json.dumps(inv, ensure_ascii=False)[:300])
                    riga["esito"] = "invio FALLITO"
                    riga["etichetta"] = etichetta(o, TAG_ERR, msg)
                    log("%s %s -> %s" % (o["name"], msg, TAG_ERR))
            else:
                riga["esito"] = "pronto (a secco)"
        rep["ordini"].append(riga)
        print("\n%s  origin %s -> %s" % (o["name"], origin, riga["esito"]))
        if riga.get("selezione") and riga["selezione"] != "ok": print("  (forzato: %s)" % riga["selezione"])
        for e in riga.get("errori", []): print("  ERRORE:", e)
        for n in riga.get("note", []): print("  nota:", n)
        if "payload" in riga and not scrive:
            print(json.dumps(riga["payload"], ensure_ascii=False, indent=1))
        if "invio" in riga: print("  invio:", riga["invio"])
        if riga.get("etichetta"): print("  errori etichetta:", riga["etichetta"])
    if modo == "auto" and not lavorati:
        print("nessun ordine lavorato"); return
    # cmd 289: nessun file di backup (disco di Render effimero)


if __name__ == "__main__":
    main()
