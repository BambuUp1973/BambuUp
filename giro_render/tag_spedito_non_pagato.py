# -*- coding: utf-8 -*-
"""Toglie il tag SPEDITO-NON-PAGATO dagli ordini Shopify che nel frattempo sono stati segnati pagati (cmd 210).

Il tag si mette A MANO su un ordine non pagato che deve partire comunque (connettore_fully.py lo manda a Fully).
Quando l'ordine diventa PAID il tag non serve piu': questo passo lo toglie e lo scrive nel log del giro.
Cerca fra TUTTI gli ordini col tag (aperti, evasi, archiviati); il tag deve essere esattamente SPEDITO-NON-PAGATO.

cmd 259: gli ordini in ECCEZIONI (segnati pagati senza incasso) vengono saltati.
cmd 289 (copia per Render): niente file tag_pagati_eccezioni.json, la riga "saltato (eccezione)" si scrive a ogni giro;
fuori da GIRO_MODO=acceso tagsRemove diventa una riga "AVREI FATTO" nel log.

Uso:  python tag_spedito_non_pagato.py      -> PROVA A SECCO (elenca soltanto)
      python tag_spedito_non_pagato.py go   -> toglie il tag (usato da giro_fully.py)
"""
import json, os, sys, time
ROOT = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, ROOT)
from _giacenze_lib import q, _mut, log

# cmd 259: ordini segnati pagati senza incasso: tag-pagati non tocca mai il loro tag. Aggiunti su richiesta di Bambu.
ECCEZIONI = ["1087-W26"]

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
    for o in pagati:
        if o["name"].lstrip("#") in ECCEZIONI:   # cmd 259: mai toccare il tag di questi ordini
            print("  %s: saltato (eccezione)" % o["name"])
            if go:      # cmd 289: riga a ogni giro (niente file di stato)
                log("%s pagato: saltato (eccezione), %s lasciato" % (o["name"], TAG_SNP))
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
