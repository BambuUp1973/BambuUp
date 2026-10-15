# -*- coding: utf-8 -*-
"""Parti di ShopifyKano/_seo_lib.py usate dal giro Fully (load_env, gql), adattate per Render (cmd 289):
  - chiavi da os.environ; il file .env serve solo in locale (GIRO_ENV_FILE, altrimenti .env accanto a questo file)
  - GIRO_MODO: "spento" | "prova" | "acceso" (mancante o sconosciuto = "spento")
  - ora italiana (Europe/Rome) per log, note e finestra dell'email
  - gql senza get_token(): il giro passa sempre il token dell'app Fully Warehouse.
Nessun segreto in questo file."""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

SHOP = "tipjd7-r0.myshopify.com"
API_VERSION = "2026-07"
ROOT = os.path.dirname(os.path.abspath(__file__))


def _leggi_file_env():
    path = os.environ.get("GIRO_ENV_FILE") or os.path.join(ROOT, ".env")
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_env():
    """os.environ prima di tutto; il file .env solo per le chiavi che mancano (uso locale)."""
    env = _leggi_file_env()
    env.update({k: v for k, v in os.environ.items() if v != ""})
    return env


# ---------- interruttore GIRO_MODO ----------
MODI = ("spento", "prova", "acceso")


def giro_modo():
    m = (os.environ.get("GIRO_MODO") or "").strip().lower()
    return m if m in MODI else "spento"


def scrive_davvero():
    return giro_modo() == "acceso"


def avrei_fatto(descrizione):
    """In modo diverso da "acceso": nessuna scrittura, solo la riga di log. Ritorna True se si puo' scrivere davvero."""
    if scrive_davvero():
        return True
    print("LOG: AVREI FATTO: " + descrizione, flush=True)
    return False


# ---------- ora italiana ----------
def _ultima_domenica(anno, mese):
    d = datetime(anno, mese + 1, 1) - timedelta(days=1) if mese < 12 else datetime(anno, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def ora_roma():
    """Ora di Roma senza fuso (naive). zoneinfo se disponibile (Linux/Render), altrimenti regola UE dell'ora legale
    (ultima domenica di marzo 01:00 UTC -> ultima domenica di ottobre 01:00 UTC = UTC+2, altrimenti UTC+1)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome")).replace(tzinfo=None)
    except Exception:
        u = datetime.now(timezone.utc).replace(tzinfo=None)
        inizio = _ultima_domenica(u.year, 3).replace(hour=1)
        fine = _ultima_domenica(u.year, 10).replace(hour=1)
        return u + timedelta(hours=2 if inizio <= u < fine else 1)


def roma_str(fmt):
    return ora_roma().strftime(fmt)


# ---------- Shopify GraphQL ----------
def gql(query, variables=None, token=None, retries=4):
    if not token:
        raise RuntimeError("gql: token mancante")
    if query.lstrip().startswith("mutation") and not scrive_davvero():   # rete di sicurezza: nessuna mutation fuori da "acceso"
        raise RuntimeError("mutation bloccata: GIRO_MODO=%s" % giro_modo())
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        "https://%s/admin/api/%s/graphql.json" % (SHOP, API_VERSION),
        data=body,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        method="POST")
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "errors" in data and data["errors"]:
                msgs = json.dumps(data["errors"], ensure_ascii=False)
                if "THROTTLED" in msgs and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1)); last = msgs; continue
                raise RuntimeError("GraphQL errors: " + msgs)
            return data["data"]
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1)); last = payload; continue
            raise RuntimeError("HTTP %s: %s" % (e.code, payload))
    raise RuntimeError("retries exhausted: %s" % last)
