# -*- coding: utf-8 -*-
"""Ritorno Fully -> Shopify: segna evasi gli ordini spediti, con tracking.

Per ogni ordine Shopify aperto e non evaso, cerca in Fully la copia dell'ordine.
cmd 256: la cerca per NOME su tutti gli shop con connettore_fully.copie_fully() (SHOP-<numero> del nostro connettore,
shop 721, e "<numero>" dell'app "Fully - KanoKimonos", shop 883). Usa l'unica copia attiva (non cancelled);
2+ copie attive -> NON evade e lo segnala nel log; solo copie annullate -> solo elenco.
  shipped + carrier_tracking_ref -> fulfillment su Shopify (numero, link, corriere, notifica al cliente)
  api_error / cancelled -> solo elenco.
  cmd 257 (allarme): ordine Shopify con FULLY-INVIATO la cui copia Fully e' in errore o in hold -> tagsAdd FULLY-ERRORE con il
  motivo in nota (cosi' entra nel riepilogo delle 09:00); quando la copia torna processing/shipped senza hold -> tagsRemove
  FULLY-ERRORE. Nessun altro tag toccato.
  cancelled / undelivered -> solo elenco
Fully: SOLO GET. Shopify: token app Fully Warehouse.

Uso:  python ritorno_fully.py               -> PROVA A SECCO
      python ritorno_fully.py go            -> crea le evasioni
      python ritorno_fully.py prova721 <N>  -> legge gli ultimi N ordini SPEDITI dello shop Fully 721 e mostra
                                               cosa verrebbe scritto (nessuna scrittura, nessun ordine Shopify)
cmd 289 (copia per Render): nessun backup su file; fuori da GIRO_MODO=acceso fulfillmentCreate e tag diventano
righe "AVREI FATTO" nel log.
"""
import json, os, sys, time, uuid
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "fully_connector"))
import build_order as B
from _giacenze_lib import q, _mut, fully_get, stamp, LOC, etichetta, log, TAG_OK, TAG_ERR
from _seo_lib import avrei_fatto
M_RM = """mutation($id:ID!, $tags:[String!]!%s){ tagsRemove(id:$id, tags:$tags)%s{ userErrors{ field message } } }"""

# nome corriere Fully -> nome riconosciuto da Shopify (il link di tracking viene comunque passato)
COMPANY = {"GLS Italia": "GLS Italy", "GLS Slovenia": "GLS", "DHL Express": "DHL Express",
           "Fedex Connect Plus": "FedEx", "Fedex Priority": "FedEx", "Fedex Connect Plus DDP": "FedEx",
           "IT - Bartolini BRT - Packeta": "BRT"}   # cmd 256: corriere usato sugli ordini dello shop 883
SENZA_TRACKING = {"Pickup", "B2B Pickup"}

Q_OPEN = """query($c:String){ orders(first:50, after:$c, query:"status:open"){
  pageInfo{ hasNextPage endCursor }
  nodes{ id name cancelledAt displayFulfillmentStatus tags note
    fulfillmentOrders(first:10){ nodes{ id status assignedLocation{ location{ id } } } } } } }"""
M = """mutation($f:FulfillmentInput!%s){ fulfillmentCreate(fulfillment:$f)%s{
  fulfillment{ id status trackingInfo{ number url company } } userErrors{ field message } } }"""


def fully_order(nome):
    """cmd 256: copia Fully dell'ordine Shopify `nome`, cercata per nome su tutti gli shop.
    Ritorna (ordine_fully_dettaglio | None, copie_attive). Con 2+ copie attive ritorna (None, attive): il chiamante non evade."""
    from connettore_fully import copie_fully
    copie = copie_fully(nome)
    attive = [x for x in copie if x.get("status") != "cancelled"]
    if len(attive) >= 2:
        return None, attive
    scelta = attive[0] if attive else (max(copie, key=lambda x: x.get("id") or 0) if copie else None)
    return (fully_get("stock.picking/orders/%s" % scelta["id"]) if scelta else None), attive


def tracking(fo):
    """Dati di ritorno letti da un ordine Fully (OrderObject)."""
    fo = fo.get("order", fo)
    carr = fo.get("carrier_name") or ""
    num = fo.get("carrier_tracking_ref") or None
    return {"fully_id": fo.get("id"), "shop": fo.get("shop_id"), "origin": fo.get("origin"), "status": fo.get("status"),
            "errors": fo.get("errors") or None, "numero": num, "link": fo.get("tracking_link") or None, "corriere_fully": carr,
            "company_shopify": COMPANY.get(carr, carr) or None, "tracking_status": fo.get("tracking_status_short"),
            "hold": bool(fo.get("hold")),
            "spedibile": fo.get("status") == "shipped" and bool(num) and carr not in SENZA_TRACKING}


def fulfill(order, t):
    fos = [f["id"] for f in order["fulfillmentOrders"]["nodes"]
           if f["status"] in ("OPEN", "IN_PROGRESS") and ((f.get("assignedLocation") or {}).get("location") or {}).get("id") == LOC]
    if not fos:
        return {"errore": "nessun fulfillment order aperto sulla sede Fully"}
    if not avrei_fatto("fulfillmentCreate %s: tracking %s %s, notifica cliente" % (order["name"], t["numero"], t["company_shopify"])):
        return {"prova": True}
    f = {"lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": x} for x in fos], "notifyCustomer": True,
         "trackingInfo": {"number": t["numero"], "url": t["link"], "company": t["company_shopify"]}}
    try:
        d = q(M % ("", ""), {"f": f})
    except RuntimeError as e:
        if "@idempotent" not in str(e): raise
        d = q(M % (", $key:String!", " @idempotent(key:$key)"), {"f": f, "key": str(uuid.uuid4())})
    r = d["fulfillmentCreate"]
    return {"fulfillment": r.get("fulfillment"), "userErrors": r["userErrors"]}


def main():
    args = sys.argv[1:]
    t0 = stamp()
    rep = {"data": t0, "modo": args[0] if args else "secco", "righe": []}
    if args and args[0] == "prova721":
        n = int(args[1]) if len(args) > 1 else 3
        d = fully_get("stock.picking/orders", {"limit": 250, "offset": 0, "sort_by": "id desc"})
        sel = [o for o in d.get("orders") or [] if o.get("shop_id") == 721 and o.get("status") == "shipped"
               and (o.get("carrier_name") or "") not in SENZA_TRACKING][:n]
        for o in sel:
            full = fully_get("stock.picking/orders/%s" % o["id"])
            t = tracking(full)
            rep["righe"].append({"fully": t, "trackingInfo_che_verrebbe_scritto":
                                 {"number": t["numero"], "url": t["link"], "company": t["company_shopify"]}})
            print("Fully %s origin %s | stato %s | tracking %s | corriere %s -> Shopify %r | link %s | %s" % (
                t["fully_id"], t["origin"], t["status"], t["numero"], t["corriere_fully"], t["company_shopify"],
                t["link"], "evadibile" if t["spedibile"] else "NON evadibile"))
    else:
        go = bool(args) and args[0] == "go"
        print("MODO:", "SCRITTURA (go)" if go else "PROVA A SECCO")
        c = None
        while True:
            d = q(Q_OPEN, {"c": c})["orders"]
            for o in d["nodes"]:
                if o["cancelledAt"] or o["displayFulfillmentStatus"] == "FULFILLED":
                    continue
                fo, attive = fully_order(o["name"])
                if len(attive) >= 2:    # cmd 256: doppione in Fully -> non evadere, segnalare
                    riga = {"ordine": o["name"], "esito": "DOPPIONE in Fully, non evaso: " + ", ".join(
                        "%s (shop %s, %s)" % (x.get("id"), x.get("shop_id"), x.get("status")) for x in attive)}
                    rep["righe"].append(riga)
                    print("%s  %s" % (o["name"], riga["esito"]))
                    if go: log("%s %s" % (o["name"], riga["esito"]))
                    continue
                if not fo:
                    continue            # non ancora in Fully
                t = tracking(fo)
                riga = {"ordine": o["name"], "fully": t}
                if t["spedibile"]:
                    riga["esito"] = "da evadere"
                    if go:
                        riga["scrittura"] = fulfill(o, t)
                        ok = riga["scrittura"].get("fulfillment") and not riga["scrittura"].get("userErrors")
                        riga["esito"] = ("da evadere (prova)" if riga["scrittura"].get("prova")
                                         else "evaso" if ok else "ERRORE evasione")
                        log("%s %s: Fully %s tracking %s %s" % (o["name"], riga["esito"], t["fully_id"], t["numero"], t["company_shopify"]))
                elif t["status"] in ("api_error", "cancelled"):
                    # cmd 243: niente tag da qui (prima del 243: FULLY-ERRORE una volta sola). Vedi docstring.
                    riga["esito"] = "solo elenco: %s" % t["status"]
                elif t["status"] == "undelivered":
                    riga["esito"] = "solo elenco: %s" % t["status"]
                else:
                    riga["esito"] = "in lavorazione (%s)" % t["status"]
                # cmd 257: allarme. Ordine gia' FULLY-INVIATO con la copia Fully in errore/hold -> FULLY-ERRORE (motivo in nota);
                # copia tornata a posto (processing/shipped, senza hold) -> via FULLY-ERRORE. Solo tagsAdd/tagsRemove mirati.
                tags = o.get("tags") or []
                bloccata = "error" in str(t["status"] or "") or t["hold"] or t["status"] == "hold"
                if bloccata and TAG_OK in tags and TAG_ERR not in tags:
                    msg = "in Fully (shop %s, id %s) ma bloccato: %s%s" % (t["shop"], t["fully_id"], t["errors"] or ("stato %s" % t["status"]),
                                                                          " (hold)" if t["hold"] else "")
                    riga["allarme"] = "metti %s: %s" % (TAG_ERR, msg)
                    if go:
                        riga["allarme_scrittura"] = etichetta(o, TAG_ERR, msg)
                        riga["esito"] = "%s -> %s" % (riga["esito"], TAG_ERR)
                        log("%s %s -> %s" % (o["name"], msg, TAG_ERR))
                elif not bloccata and t["status"] in ("processing", "shipped") and TAG_ERR in tags:
                    riga["allarme"] = "togli %s: Fully %s di nuovo %s" % (TAG_ERR, t["fully_id"], t["status"])
                    if go:
                        riga["allarme_scrittura"] = [e["message"] for e in _mut(M_RM, {"id": o["id"], "tags": [TAG_ERR]}, "tagsRemove %s su %s" % (TAG_ERR, o["name"]))["tagsRemove"]["userErrors"]]
                        log("%s Fully %s di nuovo %s -> tolto %s" % (o["name"], t["fully_id"], t["status"], TAG_ERR))
                rep["righe"].append(riga)
                if riga.get("allarme"): print("   ALLARME:", riga["allarme"])
                print("%s  %s | Fully %s shop %s %s | tracking %s %s" % (o["name"], riga["esito"], t["fully_id"], t["shop"], t["status"],
                                                                       t["numero"], t["company_shopify"]))
                if riga.get("scrittura"): print("   ", riga["scrittura"])
            if not d["pageInfo"]["hasNextPage"]: break
            c = d["pageInfo"]["endCursor"]
        print("ordini Shopify trovati in Fully: %d" % len(rep["righe"]))
        if go:
            log("ritorno: %d ordini in Fully, %d evasi, %d nuovi %s" % (
                len(rep["righe"]), sum(1 for r in rep["righe"] if r.get("esito") == "evaso"),
                sum(1 for r in rep["righe"] if str(r.get("esito")).endswith("-> %s" % TAG_ERR)), TAG_ERR))
    # cmd 289: nessun file di backup (disco di Render effimero)


if __name__ == "__main__":
    main()
