# -*- coding: utf-8 -*-
"""
stato_ordine.py - lo stato di spedizione di un ordine lo scrive il codice, non
il modello (25/09/2026). Solo per il cliente finale (profilo retail).

L'ordine lo legge main.py da Shopify, con l'app del bot (stessa lettura della
rotta admin GET /shopify-ordine); qui si decide soltanto:
- se il messaggio e' una domanda sullo stato del proprio ordine, e se contiene
  il numero d'ordine e l'email;
- quale delle risposte ammesse vale, e il suo testo.

La chiave e' numero d'ordine + email dell'ordine: senza tutt'e due non si
legge niente. Le risposte ammesse sono SOLO queste:
  non_spedito, spedito, in_parte, senza_tracking, annullato, non_trovato
piu' 'chiedi' (mancano numero o email), 'tetto' (troppi tentativi a vuoto
nella stessa chat) ed 'errore' (Shopify non risponde).
- Un ordine annullato non si dice annullato: si manda a info@, con la stessa
  frase del tetto.
- Numero inesistente ed email che non combacia hanno la STESSA frase, cosi'
  dalla risposta non si capisce se un numero d'ordine esiste.
- Mai importi, indirizzi, nomi, prodotti, ne' la pagina di stato di Shopify:
  di un ordine si usano solo nome, email, annullamento ed evasioni.
Cambio d'indirizzo, annullamento, modifiche, rimborsi e resi NON passano da
qui: restano ad Adelpina.
"""
import re

TETTO_TENTATIVI = 5
INFO = "info@kanokimonos.com"

# --- lettura del messaggio -------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_DATA_RE = re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")

# Queste richieste non sono "a che punto e' il mio ordine": vanno al modello.
_ESCLUSO_RE = re.compile(
    r"indirizz|\baddress|annull|\bcancel|disdir|modific|\bmodif|\bchange|\bcambi"
    r"|rimbors|refund|money back|\bres[oi]\b|restitu|\brendere\b|\breturn|\bexchange"
    r"|sostitu|\breplace|difett|\bdefect|\brott[oa]\b|\bbroken\b|danneggiat|\bdamaged"
    r"|sbagliat|\bwrong\b",
    re.IGNORECASE,
)
# Domande generali su tempi e costi ("quando arriva se ordino oggi?"): senza un
# numero d'ordine restano al modello, che risponde dal manuale.
_GENERALE_RE = re.compile(
    r"\bse (ordino|compro|acquisto|faccio)|\bif i (order|buy|purchase|place)"
    r"|\bquanto (costa|tempo|ci vuole|ci mette)|\bhow (much|long)\b|\btemp[io] di"
    r"|\b(delivery|shipping) (time|cost)|\bcost[aoi]\b|\bprezz|\bpric",
    re.IGNORECASE,
)
_OGGETTO_RE = re.compile(
    r"\bordin[ei]\b|\bordinat|\bacquist[oi]\b|\bpacc[oh]i?\b|\bpacchett|\bspedizion|\bcollo\b"
    r"|\borders?\b|\bpackage\b|\bparcel\b|\bshipment\b|\bdelivery\b|\bpurchase\b",
    re.IGNORECASE,
)
_MIO_RE = re.compile(
    r"\bmi[oae]\b|\bmiei\b|\bho (ordinato|fatto|acquistato|comprato|effettuato)|\bil pacco\b"
    r"|\bl['’ ]?ordine\b|\bnostr[oa]\b|\bmy\b|\bour\b|\bi (ordered|placed|bought|made)\b|\bthe (order|package|parcel)\b",
    re.IGNORECASE,
)
_STATO_RE = re.compile(
    r"\bdove\b|\bdov['’]|\bstato\b|a che punto|\barriv|\bpartit|\bspedit|\bconsegn|\btracci"
    r"|\bquando\b|\bancora\b|\bnovit|\bnotizie\b"
    r"|\bwhere\b|\bstatus\b|\btrack|\bshipped\b|\bdispatched\b|\bsent\b|\bdeliver|\bwhen\b|\byet\b|\bupdate",
    re.IGNORECASE,
)
_TRACKING_RE = re.compile(r"\btracking\b|\btracciament|\btrack my\b|\bnumero di spedizione\b", re.IGNORECASE)

# Il numero come lo scrive il negozio: 1001-W26. Con suffisso, con "#", o dopo
# "ordine/order" e' sicuramente un numero d'ordine; un numero nudo di 4-6 cifre
# vale solo se il cliente sta rispondendo alla domanda del bot o scrive anche
# l'email.
_NUM_SUFFISSO_RE = re.compile(r"#?\s*\b(\d{3,6})\s*-?\s*W\s*(\d{2})\b", re.IGNORECASE)
_NUM_CANCELLETTO_RE = re.compile(r"#\s*(\d{3,6})\b")
_NUM_DOPO_PAROLA_RE = re.compile(
    r"\b(?:ordine|order)\s*(?:n(?:r|um|o)?\.?|numero|number|#)?\s*[:.]?\s*(\d{3,6})\b"
    r"(?!\s*(?:€|euro|eur\b|%|pezzi|pz\b|items?\b))", re.IGNORECASE)
_NUM_NUDO_RE = re.compile(r"(?<![\d,.])\b(\d{4,6})\b(?![,.]\d)(?!\s*(?:cm|kg|€|euro|eur\b|%))", re.IGNORECASE)


def normalizza_numero(testo):
    """'# 1001 - w26' -> '1001-W26'; '1001' -> '1001'. None se non e' un numero."""
    t = re.sub(r"[\s#]", "", testo or "").upper()
    m = re.fullmatch(r"(\d{3,6})-?W(\d{2})", t)
    if m:
        return f"{m.group(1)}-W{m.group(2)}"
    return t if re.fullmatch(r"\d{3,6}", t) else None


def normalizza_email(email):
    return re.sub(r"\s", "", email or "").lower() or None


def trova_numero(messaggio, anche_nudo=False):
    """Il numero d'ordine scritto nel messaggio, normalizzato, oppure None."""
    testo = _DATA_RE.sub(" ", _EMAIL_RE.sub(" ", messaggio or ""))
    m = _NUM_SUFFISSO_RE.search(testo)
    if m:
        return f"{m.group(1)}-W{m.group(2)}"
    for rx in (_NUM_CANCELLETTO_RE, _NUM_DOPO_PAROLA_RE) + ((_NUM_NUDO_RE,) if anche_nudo else ()):
        m = rx.search(testo)
        if m:
            return m.group(1)
    return None


def trova_email(messaggio):
    m = _EMAIL_RE.search(messaggio or "")
    return normalizza_email(m.group(0)) if m else None


def chiede_stato(messaggio):
    """True se il cliente chiede dove sia il SUO ordine (senza badare ai numeri)."""
    t = _EMAIL_RE.sub(" ", messaggio or "")
    if _GENERALE_RE.search(t):
        return False
    if _TRACKING_RE.search(t):
        return True
    return bool(_OGGETTO_RE.search(t) and _STATO_RE.search(t) and _MIO_RE.search(t))


def analizza(messaggio, attesa=None):
    """Decide il turno. 'attesa' e' None, oppure {'numero', 'email'} se il bot
    ha appena chiesto i dati in questa chat (quello che il cliente aveva gia'
    dato si tiene). Torna None se il turno non e' di questo strumento,
    altrimenti {'azione': 'chiedi'|'cerca', 'numero', 'email', 'mancano'}."""
    testo = messaggio or ""
    if _ESCLUSO_RE.search(_EMAIL_RE.sub(" ", testo)):
        return None
    email = trova_email(testo)
    numero = trova_numero(testo, anche_nudo=attesa is not None or email is not None)
    if attesa is not None:
        if not numero and not email:
            return None          # il bot ha gia' chiesto una volta: non insiste
        numero = numero or attesa.get("numero")
        email = email or attesa.get("email")
    else:
        numero_forte = trova_numero(testo) is not None
        if not (chiede_stato(testo) or numero_forte or (numero and email)):
            return None
    mancano = [n for n, v in (("numero", numero), ("email", email)) if not v]
    return {"azione": "chiedi" if mancano else "cerca", "numero": numero, "email": email,
            "mancano": mancano}


# --- l'ordine ----------------------------------------------------------------------
def ordini_esatti(ordini, numero):
    """Shopify filtra 'name' anche in parte ('100' trova 1001, 1002...): si
    tengono solo il nome intero (1001-W26) o la sola cifra (order_number)."""
    cercato = normalizza_numero(numero)
    if not cercato:
        return []
    return [o for o in ordini or []
            if normalizza_numero(o.get("name")) == cercato or str(o.get("order_number")) == cercato]


def esito_ordine(ordini, numero, email):
    """Dagli ordini letti da Shopify con quel numero (anche non filtrati),
    l'esito per il cliente: {'esito', 'numero', 'spedizioni'}. Un ordine
    senza email, o con un'altra email, e' 'non_trovato' come un numero che
    non esiste."""
    chi = normalizza_email(email)
    buoni = [o for o in ordini_esatti(ordini, numero)
             if chi and normalizza_email(o.get("email")) == chi]
    if len(buoni) != 1:
        return {"esito": "non_trovato", "numero": None, "spedizioni": []}
    o = buoni[0]
    nome = o.get("name")
    if o.get("cancelled_at"):
        return {"esito": "annullato", "numero": nome, "spedizioni": []}
    spedizioni = []
    for f in o.get("fulfillments") or []:
        if f.get("status") != "success":
            continue
        numeri = f.get("tracking_numbers") or ([f["tracking_number"]] if f.get("tracking_number") else [])
        link = f.get("tracking_urls") or ([f["tracking_url"]] if f.get("tracking_url") else [])
        spedizioni.append({"corriere": f.get("tracking_company"), "tracking": numeri, "link": link})
    if not spedizioni:
        esito = "non_spedito"
    elif not any(s["tracking"] for s in spedizioni):
        esito = "senza_tracking"
    elif o.get("fulfillment_status") == "fulfilled":
        esito = "spedito"
    else:
        esito = "in_parte"
    return {"esito": esito, "numero": nome, "spedizioni": spedizioni,
            "in_parte": o.get("fulfillment_status") != "fulfilled"}


# --- testi -------------------------------------------------------------------------
_TESTI = {
    "chiedi_tutto": {
        "it": "Per dirti a che punto è la spedizione mi servono il numero d'ordine e l'email con cui hai ordinato: li trovi nell'email di conferma dell'ordine.",
        "en": "To tell you where your shipment is, I need your order number and the email you ordered with: you can find them in your order confirmation email.",
    },
    "chiedi_email": {
        "it": "Mi serve anche l'email con cui hai ordinato.",
        "en": "I also need the email you ordered with.",
    },
    "chiedi_numero": {
        "it": "Mi serve anche il numero d'ordine: lo trovi nell'email di conferma dell'ordine.",
        "en": "I also need your order number: you can find it in your order confirmation email.",
    },
    "non_spedito": {
        "it": "Il tuo ordine {numero} non è ancora partito.",
        "en": "Your order {numero} has not shipped yet.",
    },
    "spedito": {
        "it": "Il tuo ordine {numero} è stato spedito.",
        "en": "Your order {numero} has been shipped.",
    },
    "in_parte": {
        "it": "Una parte del tuo ordine {numero} è già partita, il resto seguirà. Ecco cosa è partito:",
        "en": "Part of your order {numero} has already shipped, the rest will follow. Here is what has shipped:",
    },
    "senza_tracking": {
        "it": "Il tuo ordine {numero} è stato spedito, ma il numero di tracking non è disponibile: scrivi a " + INFO + ".",
        "en": "Your order {numero} has been shipped, but the tracking number is not available: please write to " + INFO + ".",
    },
    "in_parte_senza_tracking": {
        "it": "Una parte del tuo ordine {numero} è già partita, il resto seguirà. Il numero di tracking di quello che è partito non è disponibile: scrivi a " + INFO + ".",
        "en": "Part of your order {numero} has already shipped, the rest will follow. The tracking number for what has shipped is not available: please write to " + INFO + ".",
    },
    "pacco_senza_tracking": {
        "it": "- un pacco senza numero di tracking disponibile: per questo scrivi a " + INFO + ".",
        "en": "- one parcel without an available tracking number: for this one please write to " + INFO + ".",
    },
    # Ordine annullato e tetto dei tentativi: la stessa frase, che non dice
    # perche'.
    "annullato": {
        "it": "Per questo ordine scrivi a " + INFO + " indicando il numero d'ordine: ti rispondiamo noi.",
        "en": "For this order please write to " + INFO + " with your order number: we will get back to you.",
    },
    # Numero inesistente ed email diversa: la stessa frase, sempre.
    "non_trovato": {
        "it": "Con questo numero d'ordine e questa email non trovo nessun ordine. Controlla i dati nell'email di conferma dell'ordine, oppure scrivi a " + INFO + ".",
        "en": "I can't find any order with this order number and email. Please check the details in your order confirmation email, or write to " + INFO + ".",
    },
    "errore": {
        "it": "In questo momento non riesco a leggere lo stato dell'ordine. Riprova tra qualche minuto, oppure scrivi a " + INFO + ".",
        "en": "I can't read the order status right now. Please try again in a few minutes, or write to " + INFO + ".",
    },
}
_TESTI["tetto"] = _TESTI["annullato"]


def _riga_spedizione(s, lingua):
    corriere = s.get("corriere")
    numeri = ", ".join(s["tracking"])
    if lingua == "en":
        riga = f"- {corriere}, tracking number {numeri}" if corriere else f"- tracking number {numeri}"
    else:
        riga = f"- {corriere}, numero di tracking {numeri}" if corriere else f"- numero di tracking {numeri}"
    if s.get("link"):
        riga += ": " + " ".join(s["link"])
    return riga


def testo_chiedi(mancano, lingua="it"):
    lingua = lingua if lingua in ("it", "en") else "it"
    chiave = "chiedi_tutto" if len(mancano) != 1 else "chiedi_" + mancano[0]
    return _TESTI[chiave][lingua]


def testo_esito(esito, lingua="it"):
    """Il testo per il cliente di un esito di esito_ordine, oppure di 'tetto'
    / 'errore' passati come {'esito': ...}."""
    lingua = lingua if lingua in ("it", "en") else "it"
    e = esito["esito"]
    numero = esito.get("numero") or ""
    if e in ("spedito", "in_parte"):
        righe = [_TESTI[e][lingua].format(numero=numero)]
        for s in esito["spedizioni"]:
            righe.append(_riga_spedizione(s, lingua) if s["tracking"] else _TESTI["pacco_senza_tracking"][lingua])
        return "\n".join(righe)
    if e == "senza_tracking" and esito.get("in_parte"):
        return _TESTI["in_parte_senza_tracking"][lingua].format(numero=numero)
    return _TESTI[e][lingua].format(numero=numero)
