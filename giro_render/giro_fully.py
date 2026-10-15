# -*- coding: utf-8 -*-
"""Giro automatico Shopify <-> Fully (lanciato ogni 10 minuti dall'operazione pianificata "Kano Giro Fully").

In ordine (cmd 256/257: di nuovo come il 01/10, il NOSTRO connettore invia; app "Fully - KanoKimonos" spenta):
            connettore_fully.py auto   -> invia a Fully gli ordini pagati o col tag SPEDITO-NON-PAGATO (max 10), etichetta
                                          FULLY-INVIATO / FULLY-ERRORE; anti-doppio su tutte le copie e tutti gli shop (cmd 256)
            ritorno_fully.py go        -> evasione con tracking degli ordini spediti, copia cercata per nome (shop 721 e 883)
            avvisi_fully.py go         -> cmd 244: UN riepilogo al giorno a info@ "Fully: N ordini fermi" (cmd 289: giro delle 09:00-09:09 Roma)
            tag_spedito_non_pagato.py go -> toglie SPEDITO-NON-PAGATO dagli ordini diventati pagati (cmd 210)
            sync_giacenze.py go        -> giacenze del sito = Fully su TUTTE le varianti diverse, scaglioni da 50 (cmd 291;
                                          non scrive se Fully non risponde/vuoto, <1000 abbinate, >10 SKU doppi)
Log: una riga per azione, solo numeri ordine (nessun dato personale).

cmd 289 (copia per Render, cartella BambuUp/giro_render): stesse regole dell'originale in ShopifyKano, con questi adattamenti:
  - GIRO_MODO (variabile d'ambiente): "spento" = esce subito; "prova" = legge tutto, ogni scrittura diventa
    una riga "AVREI FATTO: ..."; "acceso" = lavora. Mancante o sconosciuto = "spento".
  - log solo su stdout con data e ora di Roma; niente file di log, niente cartella backup/
  - fase 2: memoria tra i giri (email del giorno, riga "saltato") in _memoria.py: database se c'e' DATABASE_URL,
    altrimenti i json del PC in logs/; riga "memoria: ..." all'inizio di ogni giro
  - niente file di lock: un giro alla volta lo garantisce la pianificazione (cron Render); qui solo un controllo in memoria
  - un passo che fallisce non ferma i successivi: riga "ERRORE passo X", codice di uscita sempre 0
Uso manuale:  GIRO_MODO=prova python giro_fully.py
"""
import os, subprocess, sys, time
from _seo_lib import giro_modo, roma_str
import _memoria

ROOT = os.path.dirname(os.path.abspath(__file__))
TIMEOUT_PASSO = 8 * 60
_IN_CORSO = False               # cmd 289: blocco solo in memoria del processo
# cmd 218 (01/10/26) spento per l'app "Fully - KanoKimonos"; cmd 218 f2 RIACCESO (l'app non prendeva gli ordini),
# il connettore ora non reinvia un ordine gia' presente in Fully su qualunque shop.
# cmd 243 (06/10/26): il connettore e' diventato un CONTROLLORE (nessun invio a Fully). CONNETTORE_SPENTO = True salta
# anche il controllo (e lo scrive nel log).
# cmd 256/257 (06/10/26): connettore di nuovo in INVIO ("auto"), passi come il 01/10. Per spegnere solo l'invio:
# CONNETTORE_SPENTO = True.
CONNETTORE_SPENTO = False
PASSI = [("connettore", ["connettore_fully.py", "auto"]),
         ("ritorno", ["ritorno_fully.py", "go"]),
         ("avvisi", ["avvisi_fully.py", "go"]),      # cmd 244: UN riepilogo al giorno, non piu' una email per ordine
         ("tag-pagati", ["tag_spedito_non_pagato.py", "go"]),
         ("sync", ["sync_giacenze.py", "go"])]


def scrivi(riga):
    """cmd 289: solo stdout, data e ora di Roma."""
    print("%s  %s" % (roma_str("%Y-%m-%d %H:%M:%S"), riga), flush=True)


def passo(nome, argv):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)       # nessuna finestra su Windows
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, argv[0])] + argv[1:], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT_PASSO, creationflags=flags)
    except subprocess.TimeoutExpired:
        scrivi("ERRORE passo %s: TIMEOUT dopo %d s" % (nome, TIMEOUT_PASSO)); return False
    for line in (r.stdout or "").splitlines():
        if line.startswith("LOG: "):
            scrivi(line[5:])
    if r.returncode != 0:
        ultima = [l for l in (r.stderr or "").strip().splitlines() if l.strip()][-1:] or ["?"]
        scrivi("ERRORE passo %s (exit %s) %s" % (nome, r.returncode, ultima[0][:200]))
        return False
    return True


def main():
    global _IN_CORSO
    os.chdir(ROOT)
    modo = giro_modo()
    if modo == "spento":
        scrivi("giro: SPENTO (GIRO_MODO=%r), nessuna azione" % os.environ.get("GIRO_MODO", "")); return
    if _IN_CORSO:
        scrivi("giro saltato: un altro giro e' ancora in corso"); return
    _IN_CORSO = True
    t = time.time()
    try:
        scrivi("giro: inizio (GIRO_MODO=%s)" % modo)
        ok, testo = _memoria.controllo()     # cmd 289 fase 2: solo lettura
        scrivi(("memoria: " + testo) if ok else ("memoria: ERRORE %s" % testo if modo == "prova" else "ERRORE memoria: %s" % testo))
        if CONNETTORE_SPENTO:
            scrivi("connettore: SPENTO (CONNETTORE_SPENTO), nessun invio ordini a Fully")
        for nome, argv in PASSI:
            if nome == "connettore" and CONNETTORE_SPENTO: continue
            passo(nome, argv)       # un passo fallito non ferma i successivi
        scrivi("giro: fine (%.0f s)" % (time.time() - t))
    finally:
        _IN_CORSO = False


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # cmd 289: codice di uscita sempre 0, l'errore resta nel log
        scrivi("ERRORE giro: %s: %s" % (type(e).__name__, str(e)[:200]))
    sys.exit(0)
