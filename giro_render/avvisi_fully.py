# -*- coding: utf-8 -*-
"""Avviso email per gli ordini fermi nel giro Fully: UN SOLO RIEPILOGO AL GIORNO (cmd 244, 06/10/26).

Prima (cmd 220-243) partiva una email per ogni ordine: versione in backup/giro_pre_244_06_10_26/avvisi_fully.py.
Ora, al primo giro dopo le 09:00, UNA email da admin@kanokimonos.com a info@kanokimonos.com
"Fully: N ordini fermi" con gli ordini Shopify che hanno il tag FULLY-ERRORE (messo dal controllore,
connettore_fully.py) e che non sono annullati ne' evasi: numero, cliente, da quanti giorni e' fermo, motivo
in italiano, link all'ordine. Nessun ordine fermo -> nessuna email.
Massimo una email al giorno: il giorno fatto sta in logs/avvisi_fully_riepilogo.json. Se l'invio fallisce
il giorno NON viene segnato e si riprova al giro dopo. Sola lettura su Shopify e Fully: non tocca tag, note o ordini.

"Fermo da": data della prima riga FULLY-ERRORE della nota dopo l'ultima riga FULLY-INVIATO
(il blocco attuale); se la nota non ne ha, la data dell'ordine.

Uso:  python avvisi_fully.py         -> a secco: mostra il riepilogo di oggi, non manda niente, non segna niente
      python avvisi_fully.py go      -> dopo le 09:00, se oggi non e' gia' partito: manda il riepilogo (usato da giro_fully.py)
      python avvisi_fully.py salta   -> segna oggi come fatto senza mandare niente

cmd 289 fase 2 (copia per Render): stessa logica del PC. Differenze: il "giorno fatto" sta nella memoria _memoria.py
(tabella giro_fully_stato se c'e' DATABASE_URL, altrimenti logs/avvisi_fully_riepilogo.json come sul PC); ora di Roma;
fuori da GIRO_MODO=acceso email e memoria diventano righe "AVREI FATTO". Memoria illeggibile -> "ERRORE memoria",
nessuna email in quel giro.
"""
import json, os, re, sys, time, html, smtplib
from datetime import datetime, date
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from _giacenze_lib import q, log, TAG_ERR, TAG_OK
from _seo_lib import load_env, ora_roma, roma_str, avrei_fatto
import _memoria

MITT = "admin@kanokimonos.com"
DEST = "info@kanokimonos.com"
SMTP_HOST, SMTP_PORT = "smtp.hostinger.com", 465
STATO = "avvisi_fully_riepilogo"     # cmd 289: chiave in _memoria (database o logs/avvisi_fully_riepilogo.json)
ORA_INVIO = 9            # dalle 09:00 ora di Roma
ADMIN_URL = "https://admin.shopify.com/store/tipjd7-r0/orders/%s"
Q = """query($c:String){ orders(first:50, after:$c, query:"tag:%s"){ pageInfo{ hasNextPage endCursor }
  nodes{ id legacyResourceId name createdAt tags note cancelledAt displayFulfillmentStatus displayFinancialStatus
         customer{ displayName } shippingAddress{ name } app{ name } } } }""" % TAG_ERR
RIGA = re.compile(r"^\[(\d\d/\d\d/\d{4}) \d\d:\d\d\] (%s|%s) (.+)$" % (TAG_ERR, TAG_OK), re.M)


def motivo_semplice(m):
    """Motivo tecnico del giro -> italiano semplice."""
    m = m.strip()
    if " | " in m:   # cmd 243: il controllore mette piu' motivi nella stessa riga
        return "; inoltre ".join(motivo_semplice(x) for x in m.split(" | "))
    if m.startswith("dati sbagliati: "):
        return "dati dell'ordine sbagliati su Shopify: " + "; ".join(motivo_semplice_val(x) for x in m[len("dati sbagliati: "):].split("; "))
    x = re.match(r"in Fully \(shop (\d+), id (\d+)\) ma bloccato: (.*)", m)
    if x: return "l'ordine è in Fully (ordine Fully %s, shop %s) ma è fermo: %s" % (x.group(2), x.group(1), x.group(3))
    if m.startswith("DOPPIONE in Fully"): return "l'ordine è in Fully DUE o più volte (rischio di spedirlo doppio): " + m[len("DOPPIONE in Fully: "):]
    if m.startswith("non arrivato a Fully"): return "l'app Fully non ha ancora preso l'ordine dopo 30 minuti"
    if m.startswith("validazione: "):
        parti = [motivo_semplice_val(x) for x in m[len("validazione: "):].split("; ")]
        return "; ".join(parti)
    x = re.match(r"Fully id (\d+) api_error: (.*)", m)
    if x: return "il magazzino Fully ha rifiutato l'ordine con un errore suo (ordine Fully %s: %s)" % (x.group(1), x.group(2))
    x = re.match(r"Fully id (\d+) annullato in Fully", m)
    if x: return "l'ordine è stato annullato dentro Fully (ordine Fully %s)" % x.group(1)
    if m.startswith("invio fallito"): return "l'invio a Fully non è riuscito (Fully non ha risposto o ha dato errore)"
    return m


def motivo_semplice_val(e):
    x = re.match(r"numero civico mancante in '?(.*?)'?$", e)
    if x: return "manca il numero civico nella prima riga dell'indirizzo (c'è scritto \"%s\")" % x.group(1)
    tab = [(r"telefono mancante", "manca il numero di telefono (né nella spedizione, né nell'ordine, né nel cliente)"),
           (r"nome destinatario mancante", "manca il nome del destinatario"),
           (r"provincia mancante", "manca la provincia nell'indirizzo"),
           (r"paese mancante", "manca il paese nell'indirizzo"),
           (r"citta' mancante", "manca la città nell'indirizzo"),
           (r"CAP mancante", "manca il CAP nell'indirizzo"),
           (r"totale non positivo", "il totale dell'ordine è zero"),
           (r"valuta (\S+) diversa da EUR", "l'ordine non e' in euro"),
           (r"nessuna riga prodotto", "l'ordine non ha prodotti da spedire"),
           (r"riga senza SKU: (.*)", "un prodotto non ha il codice SKU: \\1"),
           (r"SKU (\S+) non esiste in Fully", "il codice \\1 non esiste nel magazzino Fully"),
           (r"SKU (\S+) doppio in Fully", "il codice \\1 compare due volte nel magazzino Fully"),
           (r"corriere(.*)", "problema col corriere\\1")]
    for pat, txt in tab:
        x = re.match(pat, e)
        if x: return x.expand(txt)
    return e


def a_html(t):
    out = []
    for b in re.split(r"\n\s*\n", t.strip()):
        r = html.escape(b.strip()).replace("\n", "<br>")
        r = re.sub(r"(https://[^\s<]+)", r'<a href="\1">\1</a>', r)
        out.append("<p>%s</p>" % r)
    return '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#111">%s</div>' % "\n".join(out)


def manda(dest, ogg, corpo):
    if not avrei_fatto("email %r da %s a %s" % (ogg, MITT, dest)):
        return
    pwd = load_env().get("ADMIN_SMTP_PASSWORD", "").strip()
    if not pwd: raise RuntimeError("ADMIN_SMTP_PASSWORD vuota nel .env")
    m = EmailMessage()
    m["From"] = formataddr(("Kano Kimonos - giro Fully", MITT)); m["To"] = dest; m["Reply-To"] = MITT
    m["Subject"] = ogg; m["Date"] = formatdate(localtime=True); m["Message-ID"] = make_msgid(domain="kanokimonos.com")
    m.set_content(corpo); m.add_alternative(a_html(corpo), subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        s.login(MITT, pwd); s.send_message(m)


def leggi_stato():
    return _memoria.leggi(STATO)        # cmd 289: file mancante/illeggibile -> {} come prima; database giu' -> MemoriaErrore


def salva_stato(s):
    try:
        _memoria.scrivi(STATO, s)
    except _memoria.MemoriaErrore as e:
        log("ERRORE memoria: giorno non segnato (%s)" % e)


def ordini_fermi():
    """Ordini con FULLY-ERRORE (tag esatto) non annullati e non evasi."""
    c, out = None, []
    while True:
        r = q(Q, {"c": c})["orders"]
        out += [o for o in r["nodes"] if TAG_ERR in (o.get("tags") or []) and not o.get("cancelledAt")
                and o.get("displayFulfillmentStatus") != "FULFILLED"]
        if not r["pageInfo"]["hasNextPage"]: break
        c = r["pageInfo"]["endCursor"]
    return sorted(out, key=lambda o: o["createdAt"])


def dati(o, oggi):
    righe = RIGA.findall(o.get("note") or "")
    dal, motivo = None, None
    for giorno, tag, testo in righe:
        if tag == TAG_OK:
            dal = None                     # sbloccato: il blocco attuale parte dalla riga ERRORE dopo
        else:
            dal = dal or datetime.strptime(giorno, "%d/%m/%Y").date()
            motivo = testo
    if dal is None:
        dal = datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).date()
    cliente = (o.get("customer") or {}).get("displayName") or (o.get("shippingAddress") or {}).get("name") or "(senza nome)"
    return {"ordine": o["name"], "cliente": cliente, "dal": dal.strftime("%d/%m/%Y"), "giorni": (oggi - dal).days,
            "motivo": motivo or "tag FULLY-ERRORE senza motivo nella nota (messo a mano?)",
            "motivo_semplice": motivo_semplice(motivo) if motivo else "tag FULLY-ERRORE messo a mano, nessun motivo nella nota",
            "pagamento": o.get("displayFinancialStatus"), "link": ADMIN_URL % o["legacyResourceId"]}


def giorni_txt(n):
    return "da oggi" if n == 0 else "da 1 giorno" if n == 1 else "da %d giorni" % n


def riepilogo(fermi, oggi):
    ogg = "Fully: %d ordin%s ferm%s" % (len(fermi), "e" if len(fermi) == 1 else "i", "o" if len(fermi) == 1 else "i")
    blocchi = []
    for i, d in enumerate(fermi, 1):
        blocchi.append("%d) Ordine %s - %s\nFermo %s (dal %s)\nMotivo: %s\nApri l'ordine: %s" % (
            i, d["ordine"], d["cliente"], giorni_txt(d["giorni"]), d["dal"], d["motivo_semplice"], d["link"]))
    corpo = ("Riepilogo del %s: %d ordin%s col tag FULLY-ERRORE, non annullat%s e non evas%s.\n\n%s\n\n"
             "Cosa fare: sistema l'ordine in Fully (e su Shopify se i dati sono sbagliati). Gli ordini li manda a Fully "
             "l'app \"Fully - KanoKimonos\". Quando in Fully c'e' una sola copia senza errori e senza hold e i dati "
             "su Shopify sono giusti, il giro toglie da solo il tag FULLY-ERRORE (entro 10 minuti).\n\n"
             "-- \nRiepilogo automatico del giro Shopify-Fully (una email al giorno, solo se ci sono ordini fermi).\n") % (
        oggi.strftime("%d/%m/%Y"), len(fermi), "e" if len(fermi) == 1 else "i", "o" if len(fermi) == 1 else "i",
        "o" if len(fermi) == 1 else "i", "\n\n".join(blocchi))
    return ogg, corpo


def main():
    a = sys.argv[1:]
    modo = a[0] if a else "secco"
    if modo not in ("secco", "go", "salta"):
        raise SystemExit("uso: python avvisi_fully.py [secco|go|salta]")
    oggi = ora_roma().date()
    try:
        stato = leggi_stato()
    except _memoria.MemoriaErrore as e:   # cmd 289: meglio un giro di ritardo che un'email doppia
        log("ERRORE memoria: %s; nessuna email in questo giro" % e); return
    if modo == "salta":
        stato[oggi.isoformat()] = {"segnato": roma_str("%Y-%m-%d %H:%M:%S"), "email": False, "nota": "salta a mano"}
        salva_stato(stato); print("oggi segnato come fatto, nessuna email"); return
    if modo == "go":
        if ora_roma().hour < ORA_INVIO:
            print("prima delle %02d:00: niente" % ORA_INVIO); return
        if oggi.isoformat() in stato:
            print("riepilogo di oggi gia' fatto: niente"); return
    fermi = [dati(o, oggi) for o in ordini_fermi()]
    if not fermi:
        print("nessun ordine fermo: nessuna email")
        if modo == "go":
            stato[oggi.isoformat()] = {"segnato": roma_str("%Y-%m-%d %H:%M:%S"), "email": False, "ordini": 0}
            salva_stato(stato); log("avvisi: nessun ordine fermo, nessuna email")
        return
    ogg, corpo = riepilogo(fermi, oggi)
    if modo != "go":
        print("A SECCO (non inviato)\nDa: %s  A: %s\nOggetto: %s\n\n%s" % (MITT, DEST, ogg, corpo)); return
    try:
        manda(DEST, ogg, corpo)
    except Exception as e:
        log("avvisi: riepilogo NON inviato (%s): si riprova al prossimo giro" % str(e)[:120]); return
    stato[oggi.isoformat()] = {"inviato": roma_str("%Y-%m-%d %H:%M:%S"), "a": DEST, "email": True,
                               "ordini": [d["ordine"] for d in fermi]}
    salva_stato(stato)
    log("avvisi: riepilogo %r -> %s" % (ogg, DEST))


if __name__ == "__main__":
    main()
