# -*- coding: utf-8 -*-
"""Allinea la giacenza "available" di Shopify (sede Fully) a Fully.

  base Fully  = MINIMO tra free_qty e qty_available_without_outgoing
                (un ordine Fully pending o api_error mette merce in outgoing_qty ma free_qty non scende)
  disponibile = base Fully - pezzi degli ordini Shopify aperti non ancora in Fully (min 0)
  "non ancora in Fully" = in Fully non esiste un ordine con origin SHOP-<numero ordine>,
  oppure esiste ma e' cancelled (Fully non impegna la merce).
  cmd 242: "gia' in Fully" anche se c'e' un ordine NON annullato con origin/source_ref/source_id/nome = nome ordine
  (es. "1090-W26", creato dall'app Fully shop 883): stessa ricerca di fully_gia_presente del connettore.

Solo varianti ACTIVE, esclusi bundle, senza SKU, non tracciate, SKU non in Fully.
Dal cmd 170 (29/09/26) anche le varianti componenti dei bundle citate in simple_bundles.bundled_variants
(ARASHI 2.0 White: giacca + pantalone BJJ, prodotti in bozza); le altre varianti di quei prodotti restano fuori.
Uso:  python sync_giacenze.py        -> PROVA A SECCO
      python sync_giacenze.py go     -> scrive SOLO le varianti diverse da Fully, TUTTE (cmd 291: nessun limite sul numero),
                                        a scaglioni da 50 uno dopo l'altro; scaglione non OK = riga ERRORE nel log e stop,
                                        il giro dopo riparte da cio' che e' ancora diverso
  Non scrive niente se: Fully non risponde o risponde vuoto, abbinate < 1000, SKU doppi > 10.
Fully: solo GET. cmd 289 (copia per Render): nessun backup su file; fuori da GIRO_MODO=acceso
inventorySetQuantities diventa una riga "AVREI FATTO" nel log.
"""
import json, os, re, sys, time, uuid
from collections import Counter
from _giacenze_lib import q, read_variants, read_open_orders, fully_products, fully_impegna, connector_origin, stamp, LOC, log, NON_IMPEGNA
from _seo_lib import avrei_fatto

GO = len(sys.argv) > 1 and sys.argv[1] == "go"
MIN_ABBINATE, MAX_DOPPI, BLOCCO = 1000, 10, 50      # cmd 291: scaglioni da 50, tolto il limite max= sulle varianti da cambiare
M = """mutation($input:InventorySetQuantitiesInput!, $key:String!){ inventorySetQuantities(input:$input) @idempotent(key:$key){
  inventoryAdjustmentGroup{ id } userErrors{ field message code } } }"""



def copia_attiva_in_fully(nome):
    """cmd 242: True se in Fully (qualsiasi shop) c'e' un ordine NON annullato per l'ordine Shopify `nome`.
    Stessi campi e stesso confronto di connettore_fully.fully_gia_presente, ma guarda TUTTE le copie
    (fully_gia_presente ritorna la prima, che puo' essere quella annullata del 721)."""
    from connettore_fully import tutti_fully
    nome = nome.lstrip("#")
    pat = re.compile(r"(^|[^0-9A-Za-z])#?%s($|[^0-9A-Za-z])" % re.escape(nome), re.I)
    return any((x.get("status") or "") not in NON_IMPEGNA for x in tutti_fully()
               if any(pat.search(str(x.get(k) or "")) for k in ("origin", "source_ref", "source_id", "name")))


def calcola():
    sh = read_variants(con_componenti=True)
    fby = fully_products()
    if not fby:      # cmd 291: Fully vuoto = non si scrive niente (Fully che non risponde = eccezione, gestita in main)
        raise RuntimeError("Fully ha risposto senza prodotti")
    ordini = read_open_orders()
    pend, ord_pend = Counter(), []
    for name, pezzi in ordini:
        if not fully_impegna(connector_origin(name)) and not copia_attiva_in_fully(name):
            ord_pend.append({"ordine": name, "pezzi": pezzi})
            pend.update(pezzi)
    doppi = sorted(k for k, v in Counter(r["sku"] for r in sh if r["sku"]).items() if v > 1)
    todo, esclusi = [], []
    for r in sh:
        motivo = ("bundle" if r["bundle"] else "senza SKU" if not r["sku"] else "non tracciata" if not r["tracked"]
                  else "SKU non in Fully" if r["sku"] not in fby else "SKU doppio in Fully" if len(fby[r["sku"]]) > 1
                  else "non stoccata sulla sede Fully" if not r["su_sede"] else None)
        if motivo:
            esclusi.append(dict(r, motivo=motivo)); continue
        fp = fby[r["sku"]][0]
        wo = fp.get("qty_available_without_outgoing")
        free = int(round(min(float(fp.get("free_qty") or 0), float(wo if wo is not None else fp.get("free_qty") or 0))))
        r["fully_free_qty"], r["ordini_non_in_fully"] = free, pend.get(r["sku"], 0)
        r["target"] = max(free - r["ordini_non_in_fully"], 0)
        todo.append(r)
    return sh, todo, esclusi, doppi, ord_pend


def main():
    t0 = stamp()
    print("MODO:", "SCRITTURA (go)" if GO else "PROVA A SECCO")
    try:
        sh, todo, esclusi, doppi, ord_pend = calcola()
    except Exception as e:      # cmd 291: Fully (o Shopify) non risponde -> nessuna scrittura
        print("STOP: lettura fallita, nessuna scrittura:", type(e).__name__, e)
        log("sync: STOP senza scrivere (lettura fallita: %s: %s)" % (type(e).__name__, str(e)[:200]))
        return
    cambia = [r for r in todo if r["available"] != r["target"]]
    sale = lambda r: r["target"] > (r["available"] or 0)
    pezzi_pend = sum(sum(o["pezzi"].values()) for o in ord_pend)
    print("varianti ACTIVE %d | abbinate %d | SKU doppi Shopify %d" % (len(sh), len(todo), len(doppi)))
    print("ordini aperti non in Fully %d (pezzi %d)" % (len(ord_pend), pezzi_pend))
    print("esclusi:", dict(Counter(e["motivo"] for e in esclusi)))
    comp = [r for r in sh if r.get("componente")]
    print("componenti bundle (fuori dai prodotti ACTIVE): %d, abbinati %d" % (len(comp), sum(1 for r in todo if r.get("componente"))))
    if not GO:
        for r in todo:
            if r.get("componente"):
                print("  comp %-15s %-45s %s -> %s" % (r["sku"], (r["prodotto"] + " / " + r["variante"])[:45], r["available"], r["target"]))
    print("cambierebbero %d (salgono %d, scendono %d) | gia' uguali %d" % (
        len(cambia), sum(1 for r in cambia if sale(r)), sum(1 for r in cambia if not sale(r)), len(todo) - len(cambia)))
    for r in cambia[:10]:
        print("  %-15s %-40s %s -> %s (free %s, ordini %s)" % (r["sku"], (r["prodotto"] + " / " + r["variante"])[:40],
              r["available"], r["target"], r["fully_free_qty"], r["ordini_non_in_fully"]))
    rep = {"data": t0, "modo": "go" if GO else "secco", "abbinate": len(todo), "doppi": doppi, "ordini_non_in_fully": ord_pend,
           "esclusi": [{k: e[k] for k in ("sku", "prodotto", "variante", "variant_id", "motivo")} for e in esclusi],
           "righe": [{k: r[k] for k in ("sku", "variant_id", "inventory_item", "available", "fully_free_qty",
                                        "ordini_non_in_fully", "target")} for r in todo], "blocchi": [], "stop": None}
    path = "(nessun file, cmd 289)"
    save = lambda: None          # cmd 289: nessun backup su file

    stop = []
    if len(todo) < MIN_ABBINATE: stop.append("abbinate %d < %d" % (len(todo), MIN_ABBINATE))
    if len(doppi) > MAX_DOPPI: stop.append("%d SKU doppi > %d" % (len(doppi), MAX_DOPPI))
    if stop:
        rep["stop"] = stop; save(); print("STOP:", stop, "| salvato", path)
        if GO: log("sync: STOP senza scrivere (%s)" % "; ".join(stop))
        return
    if not GO:
        save(); print("nessuna scrittura (manca 'go') | salvato", path); return
    if not cambia:   # niente da scrivere: niente backup (il giro gira ogni 10 minuti)
        print("niente da cambiare: nessuna scrittura, nessun backup")
        log("sync: abbinate %d, ordini non in Fully %d, cambiate 0" % (len(todo), len(ord_pend)))
        return

    if not avrei_fatto("inventorySetQuantities su %d varianti (sede Fully, blocchi da %d)" % (len(cambia), BLOCCO)):
        log("sync: abbinate %d, ordini non in Fully %d, da cambiare %d (prova, nulla scritto)" % (len(todo), len(ord_pend), len(cambia)))
        return
    fatti, salite, n_sc, sc_ok = 0, 0, (len(cambia) + BLOCCO - 1) // BLOCCO, 0
    for i in range(0, len(cambia), BLOCCO):     # cmd 291: uno scaglione dopo l'altro, il successivo solo se il precedente e' OK
        blk = cambia[i:i + BLOCCO]
        k = i // BLOCCO + 1
        inp = {"name": "available", "reason": "correction", "referenceDocumentUri": "logistics://kano/sync_giacenze/" + t0,
               "quantities": [{"inventoryItemId": r["inventory_item"], "locationId": LOC, "quantity": r["target"],
                               "changeFromQuantity": r["available"]} for r in blk]}
        try:
            d = q(M, {"input": inp, "key": str(uuid.uuid4())})["inventorySetQuantities"]
            ue = d["userErrors"]
        except Exception as e:      # cmd 291: nessuna risposta OK = ci si ferma qui
            d, ue = {}, [{"message": "%s: %s" % (type(e).__name__, str(e)[:200])}]
        rep["blocchi"].append({"n": len(blk), "group": (d.get("inventoryAdjustmentGroup") or {}).get("id"), "userErrors": ue})
        print("scaglione %d/%d: %d righe | userErrors %s" % (k, n_sc, len(blk), ue or "nessuno"))
        if ue:
            rep["stop"] = "ERRORE allo scaglione %d/%d" % (k, n_sc)
            log("sync: ERRORE allo scaglione %d/%d (%d righe), mi fermo qui, il giro dopo riparte: %s" % (
                k, n_sc, len(blk), "; ".join(str(x.get("message")) for x in ue)[:300]))
            break
        fatti += len(blk); salite += sum(1 for r in blk if sale(r)); sc_ok += 1
    rep["aggiornate"], rep["salite"], rep["scese"], rep["scaglioni"] = fatti, salite, fatti - salite, "%d/%d" % (sc_ok, n_sc)

    # verifica: rilettura Shopify vs target
    time.sleep(5)
    try:
        now = {r["variant_id"]: r["available"] for r in read_variants(con_componenti=True)}
    except Exception as e:
        now = {}; print("verifica non riuscita:", type(e).__name__, e)
    ok = 0
    for r in rep["righe"]:
        r["dopo"] = now.get(r["variant_id"])
        ok += r["dopo"] == r["target"]
    rep["verifica_ok"] = ok
    save()
    print("AGGIORNATE %d (salite %d, scese %d) in %d/%d scaglioni | gia' uguali %d | VERIFICA dopo = formula su %d/%d | salvato %s" % (
        fatti, salite, fatti - salite, sc_ok, n_sc, len(todo) - len(cambia), ok, len(todo), path))
    log("sync: abbinate %d, ordini non in Fully %d, da cambiare %d, cambiate %d (salite %d, scese %d), scaglioni %d/%d%s, verifica %d/%d" % (
        len(todo), len(ord_pend), len(cambia), fatti, salite, fatti - salite, sc_ok, n_sc,
        " (STOP: %s)" % rep["stop"] if rep["stop"] else "", ok, len(todo)))


if __name__ == "__main__":
    main()
