# -*- coding: utf-8 -*-
"""
spedizioni.py - il costo di spedizione per il cliente finale, dai dati veri
di Shopify (24/09/2026).

La tabella la legge main.py (GET /shopify-spedizioni, stessa funzione); qui si
decide soltanto:
- a quale paese si riferisce il cliente (nome italiano o inglese, o codice ISO);
- in quale zona sta quel paese (fuori da tutte le zone si spedisce lo
  stesso, con un preventivo caso per caso: 25/09/2026);
- quale tariffa vede un cliente del negozio: le "Spedizione B2B" non escono
  MAI, come fa kano-shipping-filter sul checkout;
- il testo per il cliente.
Nessuna cifra e' scritta nel codice: se Shopify non risponde, il dato "non e'
disponibile" e basta, e un valore letto piu' di un'ora fa non si usa.
"""
import re
import time
import unicodedata
from decimal import Decimal, InvalidOperation

# Stesso titolo, carattere per carattere, che kano-shipping-filter nasconde ai
# clienti retail.
TITOLO_B2B = "Spedizione B2B"
DURATA_CACHE_SECONDI = 3600

# --- paesi ---------------------------------------------------------------------
# Tutti i codici ISO 3166-1 alpha-2, piu' XK (Kosovo, usato da Shopify): un
# codice valido fuori da tutte le zone va a preventivo, un codice inventato
# e' "paese non riconosciuto".
CODICI_ISO = set((
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ "
    "BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM "
    "DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS "
    "GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN "
    "KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ "
    "MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM "
    "PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV "
    "SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI "
    "VN VU WF WS YE YT ZA ZM ZW XK"
).split())

# codice -> (nome italiano, nome inglese, altri nomi con cui il cliente lo scrive)
PAESI = {
    "IT": ("Italia", "Italy", ()),
    "AT": ("Austria", "Austria", ()),
    "BE": ("Belgio", "Belgium", ()),
    "BG": ("Bulgaria", "Bulgaria", ()),
    "HR": ("Croazia", "Croatia", ()),
    "CY": ("Cipro", "Cyprus", ()),
    "CZ": ("Repubblica Ceca", "the Czech Republic", ("cechia", "czechia", "czech republic")),
    "DK": ("Danimarca", "Denmark", ()),
    "EE": ("Estonia", "Estonia", ()),
    "FI": ("Finlandia", "Finland", ()),
    "FR": ("Francia", "France", ()),
    "DE": ("Germania", "Germany", ("deutschland",)),
    "GR": ("Grecia", "Greece", ()),
    "HU": ("Ungheria", "Hungary", ()),
    "IE": ("Irlanda", "Ireland", ("eire",)),
    "LV": ("Lettonia", "Latvia", ()),
    "LT": ("Lituania", "Lithuania", ()),
    "LU": ("Lussemburgo", "Luxembourg", ()),
    "MT": ("Malta", "Malta", ()),
    "NL": ("Paesi Bassi", "the Netherlands", ("olanda", "netherlands", "holland")),
    "PL": ("Polonia", "Poland", ()),
    "PT": ("Portogallo", "Portugal", ()),
    "RO": ("Romania", "Romania", ()),
    "SK": ("Slovacchia", "Slovakia", ()),
    "SI": ("Slovenia", "Slovenia", ()),
    "ES": ("Spagna", "Spain", ("espana",)),
    "SE": ("Svezia", "Sweden", ()),
    "GB": ("Regno Unito", "the United Kingdom",
           ("uk", "u.k.", "united kingdom", "gran bretagna", "great britain", "britain",
            "inghilterra", "england", "scozia", "scotland", "galles", "wales",
            "irlanda del nord", "northern ireland")),
    "CH": ("Svizzera", "Switzerland", ("suisse", "schweiz")),
    "NO": ("Norvegia", "Norway", ()),
    "IS": ("Islanda", "Iceland", ()),
    "LI": ("Liechtenstein", "Liechtenstein", ()),
    "MC": ("Monaco", "Monaco", ("principato di monaco",)),
    "SM": ("San Marino", "San Marino", ()),
    "VA": ("Citta' del Vaticano", "Vatican City", ("vaticano", "vatican", "citta del vaticano")),
    "AD": ("Andorra", "Andorra", ()),
    "AL": ("Albania", "Albania", ()),
    "BA": ("Bosnia ed Erzegovina", "Bosnia and Herzegovina", ("bosnia",)),
    "ME": ("Montenegro", "Montenegro", ()),
    "MK": ("Macedonia del Nord", "North Macedonia", ("macedonia",)),
    "RS": ("Serbia", "Serbia", ()),
    "XK": ("Kosovo", "Kosovo", ()),
    "MD": ("Moldavia", "Moldova", ()),
    "UA": ("Ucraina", "Ukraine", ()),
    "BY": ("Bielorussia", "Belarus", ()),
    "RU": ("Russia", "Russia", ()),
    "TR": ("Turchia", "Turkey", ("turkiye",)),
    "US": ("Stati Uniti", "the United States",
           ("usa", "u.s.a.", "us", "u.s.", "stati uniti d'america", "america", "united states",
            "united states of america")),
    "CA": ("Canada", "Canada", ()),
    "MX": ("Messico", "Mexico", ()),
    "BR": ("Brasile", "Brazil", ("brasil",)),
    "AR": ("Argentina", "Argentina", ()),
    "CL": ("Cile", "Chile", ()),
    "CO": ("Colombia", "Colombia", ()),
    "PE": ("Peru'", "Peru", ("peru",)),
    "AU": ("Australia", "Australia", ()),
    "NZ": ("Nuova Zelanda", "New Zealand", ()),
    "JP": ("Giappone", "Japan", ()),
    "CN": ("Cina", "China", ()),
    "KR": ("Corea del Sud", "South Korea", ("corea", "korea")),
    "IN": ("India", "India", ()),
    "SG": ("Singapore", "Singapore", ()),
    "HK": ("Hong Kong", "Hong Kong", ()),
    "TH": ("Thailandia", "Thailand", ()),
    "AE": ("Emirati Arabi Uniti", "the United Arab Emirates",
           ("emirati", "uae", "dubai", "united arab emirates")),
    "SA": ("Arabia Saudita", "Saudi Arabia", ()),
    "IL": ("Israele", "Israel", ()),
    "EG": ("Egitto", "Egypt", ()),
    "MA": ("Marocco", "Morocco", ()),
    "TN": ("Tunisia", "Tunisia", ()),
    "ZA": ("Sudafrica", "South Africa", ()),
}


def _normalizza(testo: str) -> str:
    t = unicodedata.normalize("NFKD", (testo or "").strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("’", "'")
    t = re.sub(r"^(the|il|lo|la|l'|i|gli|le|in|to)\s+", "", t)
    return re.sub(r"\s+", " ", t).strip(" .,;:!?")


_NOME_A_CODICE = {}
for _codice, (_it, _en, _altri) in PAESI.items():
    for _nome in (_it, _en, *_altri):
        _NOME_A_CODICE[_normalizza(_nome)] = _codice


def riconosci_paese(paese=None, codice=None):
    """Codice ISO del paese, o None se non si riconosce. Vale prima il nome
    scritto dal cliente (e' quello che ha detto davvero), poi il codice che il
    modello ha tradotto."""
    if paese:
        n = _normalizza(paese)
        if n in _NOME_A_CODICE:
            return _NOME_A_CODICE[n]
        if len(n) == 2 and n.upper() in CODICI_ISO:
            return n.upper()
    if codice:
        c = codice.strip().upper()
        if c in CODICI_ISO:
            return c
    return None


def nome_paese(codice: str, lingua: str, nome_shopify: str = None) -> str:
    if codice in PAESI:
        return PAESI[codice][0 if lingua == "it" else 1]
    return nome_shopify or codice


# --- tariffe -------------------------------------------------------------------
def _decimale(valore):
    try:
        return Decimal(str(valore))
    except (InvalidOperation, TypeError, ValueError):
        return None


def tariffa_retail_della_zona(zona: dict):
    """(prezzo, soglia_gratuita, None) oppure (None, None, motivo).
    Si guardano solo le tariffe attive e NON "Spedizione B2B". La forma attesa
    e' quella del negozio oggi: UNA tariffa a pagamento senza condizioni, piu'
    eventualmente una gratuita con "totale >= X". Qualunque altra forma non si
    interpreta: il dato risulta non disponibile."""
    retail = [t for t in zona.get("tariffe") or []
              if t.get("attiva") and (t.get("nome") or "").strip() != TITOLO_B2B]
    a_pagamento, soglie = [], []
    for t in retail:
        prezzo = _decimale(t.get("prezzo"))
        condizioni = t.get("condizioni_dati") or []
        if prezzo is None:
            return None, None, f"tariffa {t.get('nome')!r} senza prezzo"
        if prezzo > 0 and not condizioni:
            a_pagamento.append(prezzo)
        elif prezzo == 0 and condizioni and all(
                c.get("campo") == "TOTAL_PRICE" and c.get("operatore") == "GREATER_THAN_OR_EQUAL_TO"
                for c in condizioni):
            soglie.append(max(_decimale(c.get("valore")) or Decimal(0) for c in condizioni))
        else:
            return None, None, f"tariffa {t.get('nome')!r} con una forma non prevista"
    if len(a_pagamento) != 1:
        return None, None, f"{len(a_pagamento)} tariffe a pagamento senza condizioni nella zona"
    return a_pagamento[0], (min(soglie) if soglie else None), None


def _zona_del_paese(tabella: dict, codice: str):
    """La zona del paese nel profilo predefinito; con resto_del_mondo come
    ripiego se il negozio ne ha una. None se il paese non sta in nessuna zona."""
    profili = tabella.get("profili") or []
    predefiniti = [p for p in profili if p.get("predefinito")] or profili[:1]
    resto = None
    for p in predefiniti:
        for z in p.get("zone") or []:
            codici = z.get("codici") or []
            if codice in codici:
                return z
            if "RESTO_DEL_MONDO" in codici:
                resto = z
    return resto


# --- testi ---------------------------------------------------------------------
def _euro(valore: Decimal, lingua: str) -> str:
    intero = valore == valore.to_integral_value()
    if lingua == "en":
        return f"€{valore:.0f}" if intero else f"€{valore:.2f}"
    return (f"{valore:.0f}" if intero else f"{valore:.2f}".replace(".", ",")) + " €"


TESTI = {
    "it": {
        "manca_paese": "In quale paese va spedito l'ordine?",
        "non_riconosciuto": "Non ho capito in quale paese va spedito l'ordine: me lo scrivi?",
        "fuori_zona": ("Spediamo anche lì, ma per questo paese il costo lo calcoliamo caso per "
                       "caso. Scrivi a info@kanokimonos.com dicendo cosa vuoi ordinare e in che "
                       "paese, e ti mandiamo un preventivo."),
        "non_disponibile": ("In questo momento il costo della spedizione non è disponibile: "
                            "scrivi a info@kanokimonos.com."),
        "costo": "{paese}: la spedizione costa {prezzo}.",
        "soglia": " È gratuita per ordini da {soglia} in su.",
    },
    "en": {
        "manca_paese": "Which country should the order be shipped to?",
        "non_riconosciuto": "I didn't understand which country the order goes to: could you tell me?",
        "fuori_zona": ("We do ship there, but for this country we calculate the cost case by "
                       "case. Write to info@kanokimonos.com telling us what you'd like to order "
                       "and your country, and we'll send you a quote."),
        "non_disponibile": ("Shipping costs aren't available right now: "
                            "write to info@kanokimonos.com."),
        "costo": "Shipping to {paese} costs {prezzo}.",
        "soglia": " It's free for orders of {soglia} or more.",
    },
}


def costo_spedizione(paese, codice, lingua, leggi_tabella):
    """Il costo per il cliente finale. leggi_tabella() -> (tabella, errore).
    Torna sempre un dict con 'esito' e 'testo' (gia' per il cliente)."""
    lingua = lingua if lingua in ("it", "en") else "it"
    t = TESTI[lingua]
    if not (paese or codice):
        return {"esito": "manca_paese", "testo": t["manca_paese"]}
    iso = riconosci_paese(paese, codice)
    if not iso:
        return {"esito": "paese_non_riconosciuto", "paese_scritto": paese, "testo": t["non_riconosciuto"]}
    tabella, errore = leggi_tabella()
    if errore or not tabella:
        return {"esito": "non_disponibile", "codice": iso, "motivo": errore or "tabella vuota",
                "testo": t["non_disponibile"]}
    zona = _zona_del_paese(tabella, iso)
    if zona is None:
        return {"esito": "fuori_zona", "codice": iso, "paese": nome_paese(iso, lingua),
                "testo": t["fuori_zona"]}
    prezzo, soglia, motivo = tariffa_retail_della_zona(zona)
    if motivo:
        return {"esito": "non_disponibile", "codice": iso, "zona": zona.get("zona"),
                "motivo": motivo, "testo": t["non_disponibile"]}
    nome = nome_paese(iso, lingua)
    testo = t["costo"].format(paese=nome[0].upper() + nome[1:] if lingua == "it" else nome,
                              prezzo=_euro(prezzo, lingua))
    if soglia is not None:
        testo += t["soglia"].format(soglia=_euro(soglia, lingua))
    return {"esito": "ok", "codice": iso, "paese": nome, "zona": zona.get("zona"),
            "prezzo": str(prezzo), "valuta": "EUR",
            "soglia_gratuita": str(soglia) if soglia is not None else None, "testo": testo}


class CacheTabella:
    """La tabella di Shopify in memoria per un'ora. Scaduta l'ora si rilegge;
    se la rilettura fallisce il dato NON e' disponibile: il valore vecchio non
    si usa, perche' potrebbe essere cambiato proprio in quell'ora."""

    def __init__(self, leggi, durata=DURATA_CACHE_SECONDI, orologio=time.monotonic):
        self._leggi, self._durata, self._orologio = leggi, durata, orologio
        self._tabella, self._letta_il = None, None

    def __call__(self):
        adesso = self._orologio()
        if self._tabella is not None and adesso - self._letta_il < self._durata:
            return self._tabella, None
        try:
            tabella, errore = self._leggi()
        except Exception as e:
            tabella, errore = None, f"errore: {type(e).__name__}: {e}"
        if errore or not tabella:
            self._tabella, self._letta_il = None, None
            return None, errore or "tabella vuota"
        self._tabella, self._letta_il = tabella, adesso
        return tabella, None
