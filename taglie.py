# -*- coding: utf-8 -*-
"""
taglie.py - la taglia la calcola il codice, non il modello (22/09/2026).

Fonte: "GUIDA TAGLIE UFFICIALE (Kano Kimonos Unified Size Guide)" dentro
manuale_operativo.docx, trascritta qui come dati. Nessun valore inventato:
test_taglie.py verifica che ogni valore di queste tabelle compaia nel testo
della guida.

Come si legge la guida (le scelte sono qui, in un posto solo):
- Le fasce sono intervalli CHIUSI, come li scrive la guida ("60-70 kg" vale
  da 60 a 70 compresi; "fino a 55" vale fino a 55; "130 kg" e' il solo 130).
- La guida lascia dei vuoti (es. 71-74 kg, altezza 176-179 cm): un valore
  nel vuoto fra due fasce adiacenti e' "sul confine" e prende ENTRAMBE le
  taglie. Un valore sotto la prima fascia o sopra l'ultima e' fuori tabella.
- Shorts e rashguard adulto: per ogni fascia di peso la guida da' due
  taglie, la seconda per "statura alta circa 170 cm o piu'": da 170 cm in su
  vale la seconda, sotto la prima.
- Kids kimono: la sigla e' data per altezza ed eta'; se ci sono entrambe si
  tengono le sigle compatibili con tutt'e due, se nessuna lo e' vale
  l'altezza. Kids rashguard e shorts: solo per eta' (le misure A/B del capo
  non sono dati del cliente).
- Kimono donna: la guida non ha una tabella -> 'non_coperto'. Rashguard
  donna: tabella unisex piu' una nota. Nessuna regola "una taglia sotto".
"""
import re

# --- GI ADULTO: per riga di altezza, le taglie con la fascia di peso -----------
# (altezza_min, altezza_max, [(taglia, peso_min, peso_max), ...]); None = senza limite
GI_ADULTO = [
    (150, 160, [("A0", None, 55), ("A1", 60, 70), ("A2", 75, 90), ("A3S", 100, 120), ("A4", 130, 130)]),
    (165, 165, [("A1", None, 70), ("A2", 75, 90), ("A3S", 100, 120), ("A4", 130, 130)]),
    (170, 175, [("A1L", None, 70), ("A2", 75, 90), ("A3S", 100, 120), ("A4", 130, 130)]),
    (180, 180, [("A2", None, 70), ("A2L", 75, 90), ("A3S", 100, 120), ("A4", 130, 130)]),
    (185, 185, [("A2L", None, 90), ("A3", 100, 120), ("A4", 130, 130)]),
    (190, 190, [("A3", None, 110), ("A4", 120, 130)]),
    (195, 195, [("A4", None, 120), ("A5", 130, 130)]),
    (200, 200, [("A5", None, None)]),
]

# --- SHORTS e RASHGUARD ADULTO: per peso, due taglie (statura bassa/media, alta)
# (peso_min, peso_max, taglia_statura_bassa, taglia_statura_alta)
ADULTO_PESO = [
    (None, 65, "XS", "S"),
    (65, 75, "S", "M"),
    (75, 80, "M", "L"),
    (80, 90, "L", "XL"),
    (100, None, "XL", "XXL"),
]
STATURA_ALTA_CM = 170          # "S statura alta circa 170 cm o piu"

# --- KIDS KIMONO: sigla = altezza / eta' ---------------------------------------
# (sigla, altezza_cm, eta_min, eta_max)
KIDS_GI = [
    ("M000", 90, 2, 3),
    ("M00", 100, 3, 4),
    ("M0", 110, 4, 5),
    ("M1", 120, 5, 6),
    ("M2", 130, 7, 8),
    ("M3", 140, 9, 10),
    ("M4", 150, 10, 11),
    ("M5", 160, 11, 12),
]

# --- KIDS RASHGUARD e SHORTS: taglia = eta' (le misure A/B sono del capo) -------
# (taglia, eta_min, eta_max, A_cm, B_cm)
KIDS_RASHGUARD = [("S", 5, 6, 47, 30), ("M", 7, 8, 51, 32), ("L", 9, 10, 55, 35), ("XL", 11, 12, 59, 39)]
KIDS_SHORTS = [("S", 5, 6, 30, 29), ("M", 7, 8, 32, 32), ("L", 9, 10, 36, 35), ("XL", 11, 12, 40, 38)]

PRODOTTI = ("gi", "rashguard", "shorts", "kids_gi", "kids_rashguard", "kids_shorts")

_ORDINE = ["A0", "A1", "A1L", "A2", "A2L", "A3S", "A3", "A3L", "A4", "A5", "A6",
           "XXS", "XS", "S", "M", "L", "XL", "XXL",
           "M000", "M00", "M0", "M1", "M2", "M3", "M4", "M5"]
_RANGO = {t: i for i, t in enumerate(_ORDINE)}

# Una taglia scritta in un testo (per la rete in main.py): A0-A6 con L/S,
# M000-M5, XXS-XXL, S/M/L isolate. La lettera isolata non vale se e' seguita
# da un apostrofo ("L'ordine", "S'intende").
TAGLIA_RE = re.compile(
    r"\bA[0-6][LS]?\b|\bM0{1,3}\b|\bM[1-5]\b|\bXXS\b|\bXS\b|\bXXL\b|\bXL\b|\b[SML]\b(?!['’])"
)


def _num(x):
    """80.0 -> '80', 62.5 -> '62,5' (per i testi)."""
    if x is None:
        return None
    if float(x).is_integer():
        return str(int(x))
    return str(x).replace(".", ",")


def _dentro(v, lo, hi):
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def _compatibili(valore, fasce):
    """fasce = [(lo, hi), ...] in ordine crescente. Torna (indici, come):
    'dentro' se il valore sta in una o piu' fasce (confine condiviso ->
    piu' d'una), 'confine' se cade nel vuoto fra due fasce adiacenti (tutte
    e due), 'fuori' se sta sotto la prima o sopra l'ultima."""
    dentro = [i for i, (lo, hi) in enumerate(fasce) if _dentro(valore, lo, hi)]
    if dentro:
        return dentro, "dentro"
    sotto = [i for i, (lo, hi) in enumerate(fasce) if hi is not None and valore > hi]
    sopra = [i for i, (lo, hi) in enumerate(fasce) if lo is not None and valore < lo]
    if sotto and sopra:
        return [max(sotto), min(sopra)], "confine"
    return [], "fuori"


def _ordina(taglie):
    viste = []
    for t in taglie:
        if t not in viste:
            viste.append(t)
    return sorted(viste, key=lambda t: _RANGO.get(t, 99))


def _fascia_testo(lo, hi, unita):
    if lo is None and hi is None:
        return f"tutti i {unita}"
    if lo is None:
        return f"fino a {hi} {unita}"
    if hi is None:
        return f"da {lo} {unita}"
    if lo == hi:
        return f"{lo} {unita}"
    return f"{lo}-{hi} {unita}"


# --- le tabelle, una funzione per prodotto -------------------------------------

def _gi_adulto(altezza, peso):
    righe, come_h = _compatibili(altezza, [(lo, hi) for lo, hi, _ in GI_ADULTO])
    if not righe:
        return [], "fuori_tabella", f"altezza {_num(altezza)} cm fuori dalla guida (150-200 cm)"
    taglie, fasce = [], []
    for i in righe:
        lo, hi, celle = GI_ADULTO[i]
        idx, come_p = _compatibili(peso, [(pmin, pmax) for _, pmin, pmax in celle])
        for j in idx:
            t, pmin, pmax = celle[j]
            taglie.append(t)
            fasce.append(f"altezza {_fascia_testo(lo, hi, 'cm')}, {t} {_fascia_testo(pmin, pmax, 'kg')}")
    if not taglie:
        return [], "fuori_tabella", f"peso {_num(peso)} kg fuori dalle fasce per {_num(altezza)} cm"
    taglie = _ordina(taglie)
    return taglie, ("unica" if len(taglie) == 1 else "doppia"), "; ".join(fasce)


def _adulto_peso(altezza, peso):
    idx, come = _compatibili(peso, [(lo, hi) for lo, hi, _, _ in ADULTO_PESO])
    if not idx:
        return [], "fuori_tabella", f"peso {_num(peso)} kg fuori dalla guida"
    alta = altezza >= STATURA_ALTA_CM
    taglie, fasce = [], []
    for i in idx:
        lo, hi, bassa, altat = ADULTO_PESO[i]
        t = altat if alta else bassa
        taglie.append(t)
        fasce.append(f"{_fascia_testo(lo, hi, 'kg')} -> {bassa}-{altat}, statura {'alta' if alta else 'bassa/media'}")
    taglie = _ordina(taglie)
    return taglie, ("unica" if len(taglie) == 1 else "doppia"), "; ".join(fasce)


def _kids_gi(altezza, eta):
    if altezza is None:
        # Solo l'eta': la guida da' la sigla anche cosi' (M2 = 130 cm / 7-8 anni).
        idx, come = _compatibili(eta, [(emin, emax) for _, _, emin, emax in KIDS_GI])
        if not idx:
            return [], "fuori_tabella", f"eta' {_num(eta)} anni fuori dalla guida bambino (2-12 anni)"
        taglie = _ordina([KIDS_GI[i][0] for i in idx])
        fascia = "eta' " + "/".join(f"{KIDS_GI[i][2]}-{KIDS_GI[i][3]} anni" for i in idx)
        return taglie, ("unica" if len(taglie) == 1 else "doppia"), fascia
    idx, come = _compatibili(altezza, [(h, h) for _, h, _, _ in KIDS_GI])
    if not idx:
        return [], "fuori_tabella", f"altezza {_num(altezza)} cm fuori dalla guida bambino (90-160 cm)"
    per_altezza = [KIDS_GI[i][0] for i in idx]
    fascia = "altezza " + "/".join(f"{KIDS_GI[i][1]} cm" for i in idx)
    taglie = per_altezza
    if eta is not None:
        per_eta = [s for s, _, emin, emax in KIDS_GI if emin <= eta <= emax]
        insieme = [t for t in per_altezza if t in per_eta]
        if insieme:
            taglie = insieme
            fascia += f", eta' {_num(eta)} anni"
        else:
            fascia += f" (eta' {_num(eta)} anni non concorde: vale l'altezza)"
    taglie = _ordina(taglie)
    return taglie, ("unica" if len(taglie) == 1 else "doppia"), fascia


def _kids_eta(eta, tabella):
    idx, come = _compatibili(eta, [(emin, emax) for _, emin, emax, _, _ in tabella])
    if not idx:
        return [], "fuori_tabella", f"eta' {_num(eta)} anni fuori dalla guida bambino (5-12 anni)"
    taglie = _ordina([tabella[i][0] for i in idx])
    fascia = "eta' " + "/".join(f"{tabella[i][1]}-{tabella[i][2]} anni" for i in idx)
    return taglie, ("unica" if len(taglie) == 1 else "doppia"), fascia


# --- testi per il cliente ------------------------------------------------------

_CODA = {
    "it": "In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere.",
    "en": "If in doubt, write «operator» and we'll help you choose.",
}
_NOTA_DONNA = {
    "it": "Le taglie donna sono più aderenti: in caso di dubbio scrivi «operatore».",
    "en": "Women's sizes fit tighter: if in doubt, write «operator».",
}
_KIMONO_DONNA = {
    "it": "Per il kimono donna non ho una tabella affidabile: scrivi «operatore» e ti aiutiamo a scegliere.",
    "en": "For the women's kimono I don't have a reliable size chart: write «operator» and we'll help you choose.",
}
_NOMI_MANCANTI = {
    "it": {"altezza": "l'altezza", "peso": "il peso", "eta": "l'età",
           "prodotto": "il prodotto (kimono, rashguard o shorts)"},
    "en": {"altezza": "your height", "peso": "your weight", "eta": "the age",
           "prodotto": "the product (kimono, rashguard or shorts)"},
}
TESTO_DATI_MANCANTI = {
    "it": "Per consigliarti la taglia mi servono altezza, peso e il prodotto (kimono, rashguard o shorts).",
    "en": "To recommend a size I need your height, weight and the product (kimono, rashguard or shorts).",
}


def _misura(lingua, altezza, peso, eta):
    parti = []
    if altezza is not None:
        parti.append(f"{_num(altezza)} cm")
    if peso is not None:
        parti.append(f"{_num(peso)} kg")
    if eta is not None:
        parti.append(f"{_num(eta)} anni" if lingua == "it" else f"{_num(eta)} years")
    return (" e " if lingua == "it" else " and ").join(parti)


def _base_di_variante_L(taglie):
    """'A2' se le due taglie sono A2 e A2L (stessa taglia, variante lunga);
    None per le coppie normali (XL/XXL, A2/A3, A3S/A3)."""
    if len(taglie) != 2:
        return None
    corta, lunga = sorted(taglie, key=len)
    return corta if lunga == corta + "L" else None


def _testo(lingua, esito, taglie, altezza, peso, eta, donna, prodotto):
    m = _misura(lingua, altezza, peso, eta)
    if esito == "unica":
        t = taglie[0]
        frase = (f"Per {m} la taglia consigliata è {t}." if lingua == "it"
                 else f"For {m} the recommended size is {t}.")
    elif esito == "doppia" and _base_di_variante_L(taglie):
        # A2 e A2L non sono due corporature: e' la stessa taglia piu' lunga.
        t = _base_di_variante_L(taglie)
        frase = (f"Per {m} puoi prendere {t}: {t} è la taglia standard, {t}L è più lunga, per chi è più alto."
                 if lingua == "it"
                 else f"For {m} you can take {t}: {t} is the standard size, {t}L is longer, for taller people.")
    elif esito == "doppia":
        t1, t2 = taglie[0], taglie[-1]
        frase = (f"Per {m} sei tra {t1} e {t2}: {t1} calza più aderente, {t2} più comodo." if lingua == "it"
                 else f"For {m} you're between {t1} and {t2}: {t1} fits tighter, {t2} more comfortable.")
    else:  # fuori_tabella
        frase = (f"Per {m} la guida non copre la tua misura." if lingua == "it"
                 else f"For {m} the guide doesn't cover your measurements.")
    if donna and prodotto == "rashguard":
        frase += " " + _NOTA_DONNA[lingua]
    return frase + " " + _CODA[lingua]


def _testo_mancanti(lingua, mancano):
    nomi = [_NOMI_MANCANTI[lingua][k] for k in mancano]
    if lingua == "it":
        elenco = nomi[0] if len(nomi) == 1 else ", ".join(nomi[:-1]) + " e " + nomi[-1]
        return f"Per consigliarti la taglia mi serve ancora {elenco}."
    elenco = nomi[0] if len(nomi) == 1 else ", ".join(nomi[:-1]) + " and " + nomi[-1]
    return f"To recommend a size I still need {elenco}."


# --- la funzione ---------------------------------------------------------------

def taglia_consigliata(prodotto, altezza_cm=None, peso_kg=None, eta=None, donna=False):
    """Deterministica. prodotto in PRODOTTI (None/altro -> dati_mancanti).
    Ritorna {'prodotto', 'taglie': [...], 'fascia': str, 'esito':
    unica|doppia|fuori_tabella|dati_mancanti|non_coperto, 'mancano': [...],
    'testo': {'it': ..., 'en': ...}}."""
    prodotto = (prodotto or "").strip().lower() or None
    donna = bool(donna)
    try:
        altezza = float(altezza_cm) if altezza_cm not in (None, "") else None
        peso = float(peso_kg) if peso_kg not in (None, "") else None
        eta_n = float(eta) if eta not in (None, "") else None
    except (TypeError, ValueError):
        altezza, peso, eta_n = None, None, None

    def esito_mancanti(mancano):
        return {"prodotto": prodotto, "taglie": [], "fascia": None, "esito": "dati_mancanti",
                "mancano": mancano, "testo": {l: _testo_mancanti(l, mancano) for l in ("it", "en")}}

    if prodotto not in PRODOTTI:
        mancano = ["prodotto"]
        if altezza is None:
            mancano.append("altezza")
        if peso is None:
            mancano.append("peso")
        return esito_mancanti(mancano)

    if prodotto == "gi" and donna:
        return {"prodotto": prodotto, "taglie": [], "fascia": None, "esito": "non_coperto",
                "mancano": [], "testo": dict(_KIMONO_DONNA)}

    if prodotto in ("gi", "rashguard", "shorts"):
        mancano = [k for k, v in (("altezza", altezza), ("peso", peso)) if v is None]
        if mancano:
            return esito_mancanti(mancano)
        taglie, esito, fascia = _gi_adulto(altezza, peso) if prodotto == "gi" else _adulto_peso(altezza, peso)
    elif prodotto == "kids_gi":
        if altezza is None and eta_n is None:
            return esito_mancanti(["altezza"])
        taglie, esito, fascia = _kids_gi(altezza, eta_n)
    else:  # kids_rashguard, kids_shorts
        if eta_n is None:
            return esito_mancanti(["eta"])
        taglie, esito, fascia = _kids_eta(eta_n, KIDS_RASHGUARD if prodotto == "kids_rashguard" else KIDS_SHORTS)

    return {"prodotto": prodotto, "taglie": taglie, "fascia": fascia, "esito": esito, "mancano": [],
            "testo": {l: _testo(l, esito, taglie, altezza, peso, eta_n, donna, prodotto) for l in ("it", "en")}}


# --- lettura del messaggio del cliente (per la rete in main.py) -----------------

_H_UNITA_RE = re.compile(r"\b(\d)[,.](\d{2})\s*m(?:etri)?\b|\b(\d{2,3})\s*cm\b|\balt[oa]\s+(\d{3})\b", re.IGNORECASE)
_P_UNITA_RE = re.compile(r"\b(\d{2,3})(?:[,.]\d)?\s*(?:kg|chil[io]|kil[io]|chilogrammi)\b|\bpes[oa]\s+(\d{2,3})\b", re.IGNORECASE)
_ETA_RE = re.compile(r"\b(\d{1,2})\s*(?:anni|anno|years?|y\.?o\.?)\b", re.IGNORECASE)
_NUMERO_RE = re.compile(r"\b\d{2,3}\b")
_NON_MISURA_RE = re.compile(r"\s*(euro|eur\b|€|%|\$|£|giorn|day|ore\b|hours?\b)", re.IGNORECASE)
_KIDS_RE = re.compile(r"bambin|bimb|figli[oa]|ragazzin|junior|kids?\b|child|\bson\b|daughter", re.IGNORECASE)
_DONNA_RE = re.compile(r"\bdonna\b|femminil|\bwomen|\bwoman\b|\bfemale\b|\bragazza\b|\blady\b|\bsignora\b", re.IGNORECASE)
_PRODOTTO_RE = [
    ("rashguard", re.compile(r"rash\s?guard|\brash\b", re.IGNORECASE)),
    ("shorts", re.compile(r"\bshorts?\b|pantalonc|bermuda", re.IGNORECASE)),
    ("gi", re.compile(r"kimon|\bgi\b|\bbjj\b", re.IGNORECASE)),
]


def estrai_misure(messaggio):
    """{'altezza', 'peso', 'eta', 'prodotto', 'donna'} letti dal messaggio;
    None quando non si riconoscono. Prima i numeri con l'unita', poi l'eta',
    poi i numeri nudi: un 3 cifre fra 100 e 220 e' l'altezza, un 2-3 cifre
    fra 20 e 200 il peso."""
    testo = messaggio or ""
    out = {"altezza": None, "peso": None, "eta": None, "prodotto": None,
           "donna": bool(_DONNA_RE.search(testo)), "sicure": []}
    resto = testo
    m = _H_UNITA_RE.search(resto)
    if m:
        if m.group(1):
            out["altezza"] = float(f"{m.group(1)}{m.group(2)}")
        else:
            out["altezza"] = float(m.group(3) or m.group(4))
        out["sicure"].append("altezza")
        resto = resto[:m.start()] + " " + resto[m.end():]
    m = _P_UNITA_RE.search(resto)
    if m:
        out["peso"] = float(m.group(1) or m.group(2))
        out["sicure"].append("peso")
        resto = resto[:m.start()] + " " + resto[m.end():]
    m = _ETA_RE.search(resto)
    if m:
        out["eta"] = float(m.group(1))
        out["sicure"].append("eta")
        resto = resto[:m.start()] + " " + resto[m.end():]
    for m in _NUMERO_RE.finditer(resto):
        if _NON_MISURA_RE.match(resto[m.end():]):     # 189 euro, 25%, ...
            continue
        v = float(m.group(0))
        if out["altezza"] is None and 100 <= v <= 220:
            out["altezza"] = v
        elif out["peso"] is None and 20 <= v <= 200:
            out["peso"] = v
    base = next((nome for nome, rx in _PRODOTTO_RE if rx.search(testo)), None)
    kids = bool(_KIDS_RE.search(testo)) or (out["eta"] is not None and out["eta"] <= 14)
    out["prodotto"] = (f"kids_{base}" if kids else base) if base else None
    return out


# --- il turno e' una domanda "che taglia prendo?" ------------------------------
# Serve alla rete in main.py, PRIMA del modello. Due porte: il cliente nomina
# la taglia/misura/fit, oppure nomina un prodotto insieme a una misura scritta
# con l'unita' ("kimono, peso 80"). Restano fuori le domande che nominano una
# taglia ma chiedono altro (disponibilita', prezzo, cambio taglia, reso): li'
# risponde Adelpina come prima.
_CHIEDE_TAGLIA_RE = re.compile(
    r"\btagli[ae]\b|\bmisur[ae]\b|\bsizes?\b|\bvestibilit|\bfit\b|\bmi sta\b"
    r"|\bche\b[^.?!]{0,30}\bprend[oa]\b|\bwh(ich|at) size\b",
    re.IGNORECASE,
)
_ALTRA_DOMANDA_RE = re.compile(
    r"\bcambi|\bres[oi]\b|\brimbors|\breturn|\bexchange|\bdisponibil|\bavailab"
    r"|\bin stock\b|\bcost[aoi]\b|\bprezz|\bpric|\bspedi|\bship"
    r"|\bsbagliat|\bwrong\b",
    re.IGNORECASE,
)


def decidi_taglia(messaggio, lingua="it"):
    """None se il turno non e' una domanda di taglia (ci pensa il modello).
    Altrimenti {'rete': 'rete_taglie_diretta'|'rete_taglie_dati_mancanti',
    'testo', 'esito', 'taglie', 'misure'}: il testo e' gia' quello per il
    cliente. Il peso non si deduce mai: se manca, si chiede."""
    testo = messaggio or ""
    lingua = lingua if lingua in ("it", "en") else "it"
    if _ALTRA_DOMANDA_RE.search(testo):
        return None
    m = estrai_misure(testo)
    chiede = bool(_CHIEDE_TAGLIA_RE.search(testo))
    con_unita = bool(m["sicure"])
    if not chiede and not (m["prodotto"] and con_unita):
        return None
    r = taglia_consigliata(m["prodotto"], m["altezza"], m["peso"], m["eta"], m["donna"])
    rete = "rete_taglie_dati_mancanti" if r["esito"] == "dati_mancanti" else "rete_taglie_diretta"
    return {"rete": rete, "testo": r["testo"][lingua], "esito": r["esito"],
            "taglie": r["taglie"], "misure": m}
