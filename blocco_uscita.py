# -*- coding: utf-8 -*-
"""
blocco_uscita.py - l'ultima rete prima del cliente finale (22/09/2026).

Sul profilo retail nessuna risposta deve contenere coordinate bancarie, nomi
delle persone di Kano Kimonos o i sistemi interni, nemmeno se il modello
sbaglia: il prompt e' una raccomandazione, questo e' un controllo sul testo
finale. Chi trova qualcosa non corregge la frase, la SOSTITUISCE con un testo
fisso, perche' una frase ripulita a meta' e' peggio di una risposta onesta.

Non tocca: profilo staff e b2b, le risposte dell'operatore umano (arrivano da
/richieste/{id}/rispondi, non da qui), taglie.py, il flusso operatore.
"""
import re

# --- testi fissi che sostituiscono la risposta ---------------------------------
TESTO_PAGAMENTI = {
    "it": "Il pagamento si fa solo dal checkout del sito. Per esigenze particolari scrivi a info@kanokimonos.com.",
    "en": "Payment is only made through the website checkout. For special needs write to info@kanokimonos.com.",
}
TESTO_GENERICO = {
    "it": "Su questo non posso aiutarti qui: scrivi «operatore» o a info@kanokimonos.com.",
    "en": "I can't help with this here: type «operator» or write to info@kanokimonos.com.",
}

# --- pagamenti -----------------------------------------------------------------
# IBAN: paese e cifre di controllo ATTACCATI (IT60...), poi o tutto unito o a
# gruppi di quattro separati da UN solo spazio. Con gli spazi liberi "di 25
# euro a immagine" diventava un IBAN (falso positivo visto in test il 22/09).
_IBAN_RE = re.compile(
    r"\b[A-Za-z]{2}\d{2}[A-Za-z0-9]{11,30}\b"
    r"|\b[A-Za-z]{2}\d{2}(?: [A-Za-z0-9]{4}){2,7}(?: [A-Za-z0-9]{1,4})?\b"
)
# BIC/SWIFT nudo: 4 lettere (banca) + 2 di PAESE + 2 alfanumerici, piu' 3 di
# filiale. Il solo codice paese non basta ("KIMONOSX" ha NO in quinta-sesta
# posizione): si chiede anche un segno che non sia una parola comune, cioe' una
# cifra, la filiale XXX o la forma lunga a 11. Un BIC di 8 sole lettere
# (BCITITMM) passa di qui: lo prendono le parole IBAN/BIC/SWIFT, che nella
# pratica gli stanno sempre accanto.
_PAESI_BIC = ("IT|DE|FR|ES|SI|CH|AT|NL|BE|GB|IE|PT|GR|SK|HR|PL|CZ|HU|RO|BG|"
              "SE|DK|FI|NO|LU|MT|CY|EE|LV|LT|US|CA|JP|CN|AE|TR")
_BIC_RE = re.compile(
    rf"\b[A-Z]{{4}}(?:{_PAESI_BIC})"
    rf"(?:[A-Z0-9]*\d[A-Z0-9]*|[A-Z0-9]{{2}}XXX|[A-Z0-9]{{5}})\b"
)
_PAROLE_BANCA_RE = re.compile(
    r"\bIBAN\b|\bBIC\b|\bSWIFT\b|\bcoordinate bancarie\b|\bbank details\b|"
    r"\bcoordinate banc\w*\b|\bbank account details\b",
    re.IGNORECASE,
)

# --- persone -------------------------------------------------------------------
# Nomi propri: si bloccano, ma NON se e' il cliente stesso ad averli scritti in
# questa chat (si chiama Andrea, saluta "ciao sono Mauro"). I cognomi no: quelli
# non arrivano mai dal cliente per caso.
NOMI_STAFF = ("Mauro", "Ivan", "Andrea", "Kaltrina", "Angelis", "Yassine", "Yassirine", "Evelin")
COGNOMI_STAFF = ("Tomasetti", "Danesin")
_NOMI_RE = re.compile(r"\b(" + "|".join(NOMI_STAFF) + r")\b", re.IGNORECASE)
_COGNOMI_RE = re.compile(r"\b(" + "|".join(COGNOMI_STAFF) + r")\b", re.IGNORECASE)

# --- sistemi interni -----------------------------------------------------------
_PIATTAFORME_RE = re.compile(r"kanokimonos\.app|fully\.si|fullyview", re.IGNORECASE)
# "Fully" e' il nome del magazzino SOLO con la F maiuscola: "fully adjustable"
# e' inglese corrente e deve passare.
_FULLY_RE = re.compile(r"\bFully\b")

# --- lingua del testo (per scegliere il testo fisso) ---------------------------
_IT = {"il", "lo", "la", "le", "di", "che", "non", "per", "una", "un", "con", "sono", "e",
       "del", "della", "al", "alla", "si", "ti", "da", "in", "su", "come", "scrivi", "puoi",
       "taglia", "cliente", "grazie", "questo", "questa", "ordine", "euro"}
_EN = {"the", "is", "are", "you", "your", "to", "and", "of", "for", "with", "can", "we",
       "please", "write", "size", "order", "this", "that", "will", "from", "at", "it",
       "our", "not", "here", "payment", "thanks"}


def lingua_del_testo(testo: str) -> str:
    parole = [p.lower() for p in re.findall(r"[a-zà-ùA-ZÀ-Ù']+", testo or "")]
    it = sum(1 for p in parole if p in _IT)
    en = sum(1 for p in parole if p in _EN)
    return "en" if en > it else "it"


def _nel_cliente(termine: str, messaggi_cliente) -> bool:
    """True se il termine compare nei messaggi che il cliente ha scritto in
    questa chat. messaggi_cliente puo' essere una lista, una stringa o una
    funzione senza argomenti che le restituisce (si chiama solo se serve)."""
    if callable(messaggi_cliente):
        messaggi_cliente = messaggi_cliente()
    if not messaggi_cliente:
        return False
    if isinstance(messaggi_cliente, str):
        testo = messaggi_cliente
    else:
        testo = "\n".join(str(m) for m in messaggi_cliente if m)
    return re.search(r"\b" + re.escape(termine) + r"\b", testo, re.IGNORECASE) is not None


def blocco_uscita_retail(testo, messaggi_cliente=None, lingua=None):
    """L'ultima rete prima del cliente finale. Torna sempre un dizionario:
    {'bloccato': bool, 'categoria': 'pagamenti'|'staff'|'piattaforme'|None,
     'termine': str|None, 'testo': str da mandare, 'originale': str}.
    Con bloccato False il testo esce identico, carattere per carattere."""
    originale = testo if testo is not None else ""
    esito = {"bloccato": False, "categoria": None, "termine": None,
             "testo": originale, "originale": originale}
    if not originale.strip():
        return esito
    lingua = lingua if lingua in ("it", "en") else lingua_del_testo(originale)

    def blocca(categoria, termine):
        esito.update({"bloccato": True, "categoria": categoria, "termine": termine,
                      "testo": (TESTO_PAGAMENTI if categoria == "pagamenti" else TESTO_GENERICO)[lingua]})
        return esito

    # 1. pagamenti
    for rx in (_PAROLE_BANCA_RE, _IBAN_RE, _BIC_RE):
        m = rx.search(originale)
        if m:
            return blocca("pagamenti", m.group(0))
    # 2. persone
    m = _COGNOMI_RE.search(originale)
    if m:
        return blocca("staff", m.group(0))
    for m in _NOMI_RE.finditer(originale):
        if not _nel_cliente(m.group(0), messaggi_cliente):
            return blocca("staff", m.group(0))
    # 3. sistemi interni
    for rx in (_PIATTAFORME_RE, _FULLY_RE):
        m = rx.search(originale)
        if m:
            return blocca("piattaforme", m.group(0))
    return esito
