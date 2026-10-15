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
      python sync_giacenze.py go     -> scrive (inventorySetQuantities, blocchi da 100, stop al primo errore)
      python sync_giacenze.py go max=50 -> come go, ma NON scrive se le varianti da cambiare sono piu' di 50
Fully: solo GET. cmd 289 (copia per Render): nessun backup su file; fuori da GIRO_MODO=acceso
inventorySetQuantities diventa una riga "AVREI FATTO" nel log.
"""
import json, os, re, sys, time, uuid
from collections import Counter
from _giacenze_lib import q, read_variants, read_open_orders, fully_products, fully_impegna, connector_origin, stamp, LOC, log, NON_IMPEGNA
from _seo_lib import avrei_fatto

GO = len(sys.argv) > 1 and sys.argv[1] == "go"
MAX_CAMBI = next((int(a.split("=", 1)[1]) for a in sys.argv[2:] if a.startswith("max=")), None)
MIN_ABBINATE, MAX_DOPPI, BLOCCO = 1000, 10, 100
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
    sh, todo, esclusi, doppi, ord_pend = calcola()
    cambia = [r for r in todo if r["available"] != r["target"]]
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
    print("cambierebbero %d | gia' uguali %d" % (len(cambia), len(todo) - len(cambia)))
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
    if MAX_CAMBI is not None and len(cambia) > MAX_CAMBI: stop.append("%d varianti da cambiare > %d" % (len(cambia), MAX_CAMBI))
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
    fatti = 0
    for i in range(0, len(cambia), BLOCCO):
        blk = cambia[i:i + BLOCCO]
        inp = {"name": "available", "reason": "correction", "referenceDocumentUri": "logistics://kano/sync_giacenze/" + t0,
               "quantities": [{"inventoryItemId": r["inventory_item"], "locationId": LOC, "quantity": r["target"],
                               "changeFromQuantity": r["available"]} for r in blk]}
        d = q(M, {"input": inp, "key": str(uuid.uuid4())})["inventorySetQuantities"]
        ue = d["userErrors"]
        rep["blocchi"].append({"n": len(blk), "group": (d.get("inventoryAdjustmentGroup") or {}).get("id"), "userErrors": ue})
        print("blocco %d: %d righe | userErrors %s" % (i // BLOCCO + 1, len(blk), ue or "nessuno"))
        if ue:
            rep["stop"] = "userErrors al blocco %d" % (i // BLOCCO + 1); break
        fatti += len(blk)
    rep["aggiornate"] = fatti

    # verifica: rilettura Shopify vs target
    time.sleep(5)
    now = {r["variant_id"]: r["available"] for r in read_variants(con_componenti=True)}
    ok = 0
    for r in rep["righe"]:
        r["dopo"] = now.get(r["variant_id"])
        ok += r["dopo"] == r["target"]
    rep["verifica_ok"] = ok
    save()
    print("AGGIORNATE %d | gia' uguali %d | VERIFICA dopo = formula su %d/%d | salvato %s" % (
        fatti, len(todo) - len(cambia), ok, len(todo), path))
    log("sync: abbinate %d, ordini non in Fully %d, cambiate %d%s, verifica %d/%d" % (
        len(todo), len(ord_pend), fatti, " (STOP: %s)" % rep["stop"] if rep["stop"] else "", ok, len(todo)))


if __name__ == "__main__":
    main()
