# -*- coding: utf-8 -*-
"""Memoria del giro tra un lancio e l'altro (cmd 289 fase 2), al posto dei file del PC
logs/avvisi_fully_riepilogo.json e logs/tag_pagati_eccezioni.json. Stesso contenuto (un dict json per chiave).

  DATABASE_URL presente -> tabella giro_fully_stato (chiave text primary key, valore jsonb, aggiornato timestamptz).
                           Il giro non tocca nessun'altra tabella.
  DATABASE_URL assente  -> file json <chiave>.json in logs/ accanto agli script (come sul PC).

Lettura sempre permessa. Scrittura solo con GIRO_MODO=acceso, altrimenti riga "AVREI FATTO: memoria ...".
Database che non risponde -> MemoriaErrore (chi chiama decide: avvisi niente email, tag-pagati prosegue)."""
import json, os
from _seo_lib import load_env, avrei_fatto

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "logs")
TABELLA = "giro_fully_stato"
_CREA = """CREATE TABLE IF NOT EXISTS giro_fully_stato (
  chiave text PRIMARY KEY, valore jsonb NOT NULL, aggiornato timestamptz NOT NULL DEFAULT now())"""


class MemoriaErrore(RuntimeError):
    pass


def _url():
    return (load_env().get("DATABASE_URL") or "").strip()


def tipo():
    return "database" if _url() else "file locale"


def _conn():
    try:
        import psycopg2
        return psycopg2.connect(_url(), connect_timeout=10)
    except Exception as e:
        raise MemoriaErrore("%s: %s" % (type(e).__name__, " ".join(str(e).split())[:150]))


def _tabella_esiste(cur):
    cur.execute("SELECT to_regclass(%s)", ("public." + TABELLA,))
    return cur.fetchone()[0] is not None


def leggi(chiave):
    """Dict salvato per `chiave`, {} se non c'e' ancora. Database irraggiungibile -> MemoriaErrore."""
    if not _url():
        try: return json.load(open(os.path.join(LOGS, chiave + ".json"), encoding="utf-8"))
        except (OSError, ValueError): return {}          # come l'originale del PC
    c = _conn()
    try:
        with c.cursor() as cur:
            if not _tabella_esiste(cur):                 # in prova la tabella non si crea: vuota = {}
                return {}
            cur.execute("SELECT valore FROM giro_fully_stato WHERE chiave = %s", (chiave,))
            r = cur.fetchone()
            return (r[0] if r else None) or {}
    except Exception as e:
        raise MemoriaErrore("%s: %s" % (type(e).__name__, " ".join(str(e).split())[:150]))
    finally:
        c.close()


def scrivi(chiave, valore):
    """Salva il dict. Fuori da GIRO_MODO=acceso solo la riga AVREI FATTO. Errore database -> MemoriaErrore."""
    if not avrei_fatto("memoria %s (%s) = %s" % (chiave, tipo(), json.dumps(valore, ensure_ascii=False)[-200:])):
        return
    if not _url():
        os.makedirs(LOGS, exist_ok=True)
        path = os.path.join(LOGS, chiave + ".json")
        tmp = path + ".tmp"
        json.dump(valore, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return
    c = _conn()
    try:
        with c, c.cursor() as cur:                       # una transazione: commit o rollback
            cur.execute(_CREA)
            cur.execute("""INSERT INTO giro_fully_stato (chiave, valore, aggiornato) VALUES (%s, %s::jsonb, now())
                           ON CONFLICT (chiave) DO UPDATE SET valore = EXCLUDED.valore, aggiornato = now()""",
                        (chiave, json.dumps(valore, ensure_ascii=False)))
    except Exception as e:
        raise MemoriaErrore("%s: %s" % (type(e).__name__, " ".join(str(e).split())[:150]))
    finally:
        c.close()


def controllo():
    """Per la riga di log del giro: (ok, testo). Solo lettura."""
    if not _url():
        return True, "file locale"
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            esiste = _tabella_esiste(cur)
        return True, "database OK" + ("" if esiste else " (tabella %s non ancora creata)" % TABELLA)
    except Exception as e:
        return False, " ".join(str(e).split())[:200]
    finally:
        if c is not None: c.close()
