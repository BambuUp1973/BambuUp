# -*- coding: utf-8 -*-
"""
cliente_test.py - attrezzo di collaudo: prova il bot BambuUp fingendosi un
cliente retail.

    python cliente_test.py              modo interattivo (chat nel browser)
    python cliente_test.py --batteria   modo batteria (domande_cliente.txt)

La chiave retail si legge da chiave_retail.txt (file locale, ignorato da git).
REGOLA: il valore della chiave non viene MAI stampato ne' scritto da nessuna
parte: ne' a schermo, ne' nei log, ne' nei file di output.
"""
import json
import os
import sys
import time
import uuid
import threading
import webbrowser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

CARTELLA = os.path.dirname(os.path.abspath(__file__))
FILE_CHIAVE = os.path.join(CARTELLA, "chiave_retail.txt")
FILE_DOMANDE = os.path.join(CARTELLA, "domande_cliente.txt")

BOT_URL = "https://bambuup.onrender.com/chat"
HEADER_CHIAVE = "x-bot-client-key"       # da main.py: Header x_bot_client_key
PORTA_LOCALE = 8787
TIMEOUT_BOT = 120                        # secondi
PAUSA_BATTERIA = 21                      # secondi fra una domanda e l'altra
ATTESA_429 = 60                          # secondi di attesa se manca Retry-After
ATTESA_429_MAX = 600                     # tetto di sicurezza all'attesa chiesta dal server


def leggi_chiave():
    try:
        with open(FILE_CHIAVE, encoding="utf-8-sig") as f:
            chiave = f.read().strip()
    except FileNotFoundError:
        sys.exit("Manca chiave_retail.txt nella cartella BambuUp.")
    if not chiave or "CANCELLA" in chiave or "\n" in chiave:
        sys.exit("chiave_retail.txt non contiene una chiave valida (una riga sola).")
    return chiave


CHIAVE = leggi_chiave()


def attesa_richiesta(risposta):
    """Secondi da aspettare secondo l'header Retry-After, oppure None se
    l'header manca o non si capisce. Accetta sia i secondi sia una data HTTP."""
    grezzo = (risposta.headers.get("Retry-After") or "").strip()
    if not grezzo:
        return None
    try:
        secondi = float(grezzo)
    except ValueError:
        try:
            quando = parsedate_to_datetime(grezzo)
        except (TypeError, ValueError):
            return None
        if quando is None:
            return None
        if quando.tzinfo is None:
            quando = quando.replace(tzinfo=timezone.utc)
        secondi = (quando - datetime.now(timezone.utc)).total_seconds()
    if secondi <= 0:
        return 0.0
    return min(secondi, ATTESA_429_MAX)


def nuovo_chat_id():
    return "TEST-CLIENTE-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def chiedi_al_bot(messaggio, chat_id):
    """Una chiamata al bot. Ritorna (codice_http, testo_visto_dal_cliente,
    secondi, attesa_chiesta). Il codice e' None se la rete non ha risposto;
    attesa_chiesta e' il Retry-After in secondi, None se il server non lo manda."""
    payload = {
        "source": "web",              # come il widget in static/chat.html
        "sender": "Cliente di prova",
        "chat_id": chat_id,
        "message": messaggio,
        "role": "retail",             # il server lo ignora: decide dalla chiave
    }
    inizio = time.perf_counter()
    try:
        r = requests.post(BOT_URL, json=payload,
                          headers={HEADER_CHIAVE: CHIAVE}, timeout=TIMEOUT_BOT)
    except requests.RequestException as e:
        return None, f"[errore di rete: {type(e).__name__}]", time.perf_counter() - inizio, None
    secondi = time.perf_counter() - inizio
    attesa = attesa_richiesta(r)
    try:
        dati = r.json()
    except ValueError:
        dati = {}
    if r.status_code == 200 and dati.get("status") == "saved":
        return 200, dati.get("reply", ""), secondi, attesa
    # 429 (limite), 401/403 (chiave), 503, errore interno: il testo e' quello
    # che il server manda, senza dettagli tecnici aggiunti da noi.
    testo = dati.get("reply") or dati.get("detail") or dati.get("details") or r.text[:500]
    return r.status_code, f"[HTTP {r.status_code}] {testo}", secondi, attesa


# --- MODO INTERATTIVO --------------------------------------------------------

# La pagina locale e' LO STESSO widget che andra' sul sito (static/chat.html),
# servito da qui senza configurazione: il widget parla con questa origine e
# questo server aggiunge la chiave retail e inoltra a bambuup.onrender.com.
# Cosi' storia, bottone operatore e aggiornamento ogni 15 s si collaudano
# sullo stesso HTML che vedra' il cliente.
BOT_BASE = BOT_URL[: -len("/chat")]
FILE_WIDGET = os.path.join(CARTELLA, "static", "chat.html")
INOLTRA_POST = {"/chat", "/richieste/apri"}          # percorsi inoltrati tali e quali


class Locale(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # niente log di default (sicurezza + pulizia)
        pass

    def _rispondi(self, codice, corpo, tipo, extra=None):
        self.send_response(codice)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _inoltra(self, metodo, corpo=None):
        """Inoltra la chiamata al bot con la chiave retail; riporta stato,
        corpo JSON e Retry-After come li manda il server. La chiave non
        viene mai loggata."""
        url = BOT_BASE + self.path
        inizio = time.perf_counter()
        try:
            if metodo == "POST":
                r = requests.post(url, data=corpo, headers={HEADER_CHIAVE: CHIAVE,
                                  "Content-Type": "application/json"}, timeout=TIMEOUT_BOT)
            else:
                r = requests.get(url, headers={HEADER_CHIAVE: CHIAVE}, timeout=TIMEOUT_BOT)
        except requests.RequestException as e:
            print(f"[{datetime.now():%H:%M:%S}] {metodo} {self.path} errore di rete {type(e).__name__}")
            self._rispondi(502, json.dumps({"detail": "[errore di rete verso il bot]"}).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        secondi = time.perf_counter() - inizio
        percorso = urlparse(self.path).path
        if not percorso.endswith("/messaggi"):
            print(f"[{datetime.now():%H:%M:%S}] {metodo} {percorso} HTTP {r.status_code} in {secondi:.1f}s")
        extra = {}
        if r.headers.get("Retry-After"):
            extra["Retry-After"] = r.headers["Retry-After"]
        self._rispondi(r.status_code, r.content, r.headers.get("Content-Type", "application/json"), extra)

    def do_GET(self):
        percorso = urlparse(self.path).path
        if percorso == "/":
            with open(FILE_WIDGET, "rb") as f:
                self._rispondi(200, f.read(), "text/html; charset=utf-8")
        elif percorso.startswith("/chat/") and percorso.endswith("/messaggi"):
            self._inoltra("GET")
        else:
            self.send_error(404)

    def do_POST(self):
        percorso = urlparse(self.path).path
        if percorso not in INOLTRA_POST:
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        self._inoltra("POST", self.rfile.read(n))


def modo_interattivo():
    server = ThreadingHTTPServer(("127.0.0.1", PORTA_LOCALE), Locale)
    url = f"http://localhost:{PORTA_LOCALE}"
    print(f"Server locale su {url}: serve static/chat.html e inoltra /chat, /chat/<id>/messaggi "
          f"e /richieste/apri a {BOT_BASE} con la chiave retail.")
    print("La conversazione la tiene il browser (localStorage). Ctrl+C per chiudere.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiuso.")
    finally:
        server.server_close()


# --- MODO BATTERIA ------------------------------------------------------------

def leggi_domande(percorso):
    domande = []
    with open(percorso, encoding="utf-8-sig") as f:
        for riga in f:
            riga = riga.strip()
            if riga and not riga.startswith("#"):
                domande.append(riga)
    return domande


def modo_batteria(percorso_domande):
    domande = leggi_domande(percorso_domande)
    if not domande:
        sys.exit(f"Nessuna domanda in {percorso_domande}.")
    avvio = datetime.now()
    file_out = os.path.join(CARTELLA, f"test_cliente_{avvio:%Y-%m-%d_%H%M}.txt")
    totale = len(domande)
    print(f"Batteria di {totale} domande da {os.path.basename(percorso_domande)} -> {os.path.basename(file_out)}")
    with open(file_out, "w", encoding="utf-8") as out:
        out.write(f"Collaudo cliente BambuUp (profilo retail) - avvio {avvio:%Y-%m-%d %H:%M}\n")
        out.write(f"Bot: {BOT_URL} - domande: {totale} - pausa fra domande: {PAUSA_BATTERIA}s\n")
        for i, domanda in enumerate(domande, 1):
            if i > 1:
                time.sleep(PAUSA_BATTERIA)
            print(f"  domanda {i} di {totale} ...", end="", flush=True)
            chat_id = nuovo_chat_id()          # ogni domanda e' una conversazione nuova
            codice, risposta, secondi, attesa = chiedi_al_bot(domanda, chat_id)
            nota = ""
            if codice == 429:
                if attesa is None:
                    pausa, da_dove = ATTESA_429, "senza Retry-After"
                else:
                    pausa, da_dove = attesa, "Retry-After"
                print(f" 429, attendo {pausa:.0f}s ({da_dove}) e riprovo ...", end="", flush=True)
                time.sleep(pausa)
                codice, risposta, secondi, _ = chiedi_al_bot(domanda, chat_id)
                nota = f" (dopo un 429, attesa di {pausa:.0f}s da {da_dove}, e un ritentativo)"
            print(f" HTTP {codice} in {secondi:.1f}s")
            out.write("\n" + "=" * 72 + "\n")
            out.write(f"DOMANDA {i}/{totale}: {domanda}\n")
            out.write(f"TEMPO DI RISPOSTA: {secondi:.1f} s{nota}\n")
            out.write(f"ESITO HTTP: {codice}\n")
            out.write("RISPOSTA:\n" + (risposta or "(vuota)") + "\n")
            out.flush()
            if codice in (401, 403):
                print(f"Il bot ha risposto {codice}: mi fermo. Messaggio: {risposta}")
                break
        out.write("\n" + "=" * 72 + f"\nFine {datetime.now():%Y-%m-%d %H:%M}\n")
    print(f"Scritto {file_out}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--batteria":
        modo_batteria(args[1] if len(args) > 1 else FILE_DOMANDE)
    elif args:
        sys.exit("Uso: python cliente_test.py [--batteria [file_domande]]")
    else:
        modo_interattivo()
