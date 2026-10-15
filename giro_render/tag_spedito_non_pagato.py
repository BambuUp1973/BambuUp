# -*- coding: utf-8 -*-
"""Toglie il tag SPEDITO-NON-PAGATO dagli ordini Shopify che nel frattempo sono stati segnati pagati (cmd 210).

Il tag si mette A MANO su un ordine non pagato che deve partire comunque (connettore_fully.py lo manda a Fully).
Quando l'ordine diventa PAID il tag non serve piu': questo passo lo toglie e lo scrive nel log del giro.
Cerca fra TUTTI gli ordini col tag (aperti, evasi, archiviati); il tag deve essere esattamente SPEDITO-NON-PAGATO.

cmd 259: gli ordini in ECCEZIONI (segnati pagati senza incasso) vengono saltati; riga "saltato (eccezione)" una volta al giorno.

Uso:  python tag_spedito_non_pagato.py      -> PROVA A SECCO (elenca soltanto)
      python tag_spedito_non_pagato.py go   -> toglie il tag (usato da giro_fully.py)

cmd 289 fase 2 (copia per Render): stessa logica del PC. Differenze: il giorno della riga "saltato" sta nella memoria
_memoria.py (tabella giro_fully_stato se c'e' DATABASE_URL, altrimenti logs/tag_pagati_eccezioni.json come sul PC);
ora di Roma; fuori da GIRO_MODO=acceso tagsRemove e memoria diventano righe "AVREI FATTO".
"""
import json, os, sys, time
ROOT = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, ROOT)
from _giacenze_lib import q, _mut, log
from _seo_lib import roma_str
import _memoria

# cmd 259: ordini segnati pagati senza incasso: tag-pagati non tocca mai il loro tag. Aggiunti su richiesta di Bambu.
ECCEZIONI = ["1087-W26"]
STATO_ECC = "tag_pagati_eccezioni"   # giorno in cui e' gia' stata scritta la riga "saltato" (cmd 289: chiave in _memoria)

TAG_SNP = "SPEDITO-NON-PAGATO"
Q = """query($c:String){ orders(first:50, after:$c, query:"tag:'%s'"){
  pageInfo{ hasNextPage endCursor } nodes{ id name tags closed cancelledAt displayFinancialStatus displayFulfillmentStatus } } }""" % TAG_SNP
M_RM = """mutation($id:ID!, $tags:[String!]!%s){ tagsRemove(id:$id, tags:$tags)%s{ userErrors{ field message } } }"""


def main():
    go = sys.argv[1:2] == ["go"]
    ordini, c = [], None
    while True:
        d = q(Q, {"c": c})["orders"]
        ordini += [o for o in d["nodes"] if TAG_SNP in (o.get("tags") or [])]   # tag esatto
        if not d["pageInfo"]["hasNextPage"]: break
        c = d["pageInfo"]["endCursor"]
    pagati = [o for o in ordini if o["displayFinancialStatus"] == "PAID"]
    print("MODO: %s | ordini con %s: %d | diventati PAID: %d" % ("SCRITTURA (go)" if go else "PROVA A SECCO", TAG_SNP, len(ordini), len(pagati)))
    for o in ordini:
        print("  %s | %s | %s | %s" % (o["name"], o["displayFinancialStatus"], o["displayFulfillmentStatus"], "archiviato" if o["closed"] else "aperto"))
    try: gia = _memoria.leggi(STATO_ECC)
    except _memoria.MemoriaErrore as e:
        gia = {}; log("ERRORE memoria: %s" % e)
    oggi = roma_str("%Y-%m-%d")
    for o in pagati:
        if o["name"].lstrip("#") in ECCEZIONI:   # cmd 259: mai toccare il tag di questi ordini
            print("  %s: saltato (eccezione)" % o["name"])
            if go and gia.get(o["name"]) != oggi:      # riga nel log solo la prima volta al giorno
                log("%s pagato: saltato (eccezione), %s lasciato" % (o["name"], TAG_SNP))
                gia[o["name"]] = oggi
                try: _memoria.scrivi(STATO_ECC, gia)
                except _memoria.MemoriaErrore as e: log("ERRORE memoria: %s" % e)
            continue
        if not go:
            print("  %s: il tag verrebbe tolto (a secco)" % o["name"]); continue
        err = [e["message"] for e in _mut(M_RM, {"id": o["id"], "tags": [TAG_SNP]}, "tagsRemove %s su %s" % (TAG_SNP, o["name"]))["tagsRemove"]["userErrors"]]
        if err:
            log("%s pagato: %s NON tolto (%s)" % (o["name"], TAG_SNP, "; ".join(err)))
        else:
            log("%s pagato: tolto il tag %s" % (o["name"], TAG_SNP))


if __name__ == "__main__":
    main()
