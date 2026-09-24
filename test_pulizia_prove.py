# -*- coding: utf-8 -*-
"""/admin/pulizia-prove su un database SQLite con le stesse tabelle e colonne
usate dalla pulizia: l'anteprima non cancella, la cancellazione toglie solo
le chat TEST- e lascia le altre, senza chiave admin 401."""
import sqlite3
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import main

CHIAVE = "chiave-di-prova"
SCHEMA = [
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, source TEXT, sender TEXT, chat_id TEXT, role TEXT, content TEXT)",
    "CREATE TABLE richieste_operatore (id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, tipo TEXT, "
    "stato TEXT, operatore TEXT, risposta TEXT)",
    "CREATE TABLE strumenti_log (id INTEGER PRIMARY KEY, chat_id TEXT, strumento TEXT)",
    "CREATE TABLE chat_stato (chat_id TEXT PRIMARY KEY, stato TEXT)",
    "CREATE TABLE feedback (id INTEGER PRIMARY KEY, question TEXT)",
    "CREATE TABLE knowledge_documents (id INTEGER PRIMARY KEY, title TEXT, content TEXT)",
]
# Tre chat di prova, e tre chat che NON vanno toccate: un cliente vero, una
# chat "test-" minuscola, una che contiene TEST- ma non all'inizio.
DATI = [
    ("INSERT INTO messages (sender, chat_id, content) VALUES (?, ?, ?)", [
        ("Cliente", "TEST-C26-a", "come posso pagare?"), ("bot", "TEST-C26-a", "Si paga sul sito."),
        ("Cliente", "TEST-OP-1", "operatore"), ("operatore", "TEST-OP-1", "Ciao, sono l'operatore."),
        ("Cliente", "TEST-RICH-2", "ciao"),
        ("Cliente", "cliente-vero-123", "che taglia prendo?"), ("bot", "cliente-vero-123", "A2."),
        ("Cliente", "test-minuscolo", "ciao"), ("Cliente", "chat-TEST-dentro", "ciao"),
    ]),
    ("INSERT INTO richieste_operatore (chat_id, tipo, stato, operatore, risposta) VALUES (?, ?, ?, ?, ?)", [
        ("TEST-OP-1", "cliente_chiede", "chiusa", "prova-claude", "Ciao, sono l'operatore."),
        ("TEST-RICH-2", "bot_non_sa", "aperta", None, None),
        ("cliente-vero-123", "bot_non_sa", "aperta", None, None),
        ("chat-vera-con-prova", "cliente_chiede", "risposta", "prova-claude", "ok"),
    ]),
    ("INSERT INTO strumenti_log (chat_id, strumento) VALUES (?, ?)", [
        ("TEST-C26-a", "costo_spedizione"), ("TEST-C26-a", "rispondi_dal_manuale"),
        ("cliente-vero-123", "taglia_consigliata"),
    ]),
    ("INSERT INTO chat_stato (chat_id, stato) VALUES (?, ?)", [
        ("TEST-OP-1", "conferma_operatore"), ("cliente-vero-123", "conferma_operatore"),
    ]),
    ("INSERT INTO feedback (question) VALUES (?)", [("domanda",)]),
    ("INSERT INTO knowledge_documents (title, content) VALUES (?, ?)", [("Manuale - Parte 1", "testo")]),
]
PROVE = ["TEST-C26-a", "TEST-OP-1", "TEST-RICH-2"]


class _Conn:
    """La connessione SQLite condivisa, che la pulizia puo' 'chiudere'."""

    def __init__(self, db):
        self.db = db

    def cursor(self):
        return self.db.cursor()

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass


class PuliziaProve(unittest.TestCase):

    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)  # la rotta gira in un altro thread
        for q in SCHEMA:
            self.db.execute(q)
        for q, righe in DATI:
            self.db.executemany(q, righe)
        self.db.commit()
        self.client = TestClient(main.app)
        for p in (mock.patch.object(main, "BOT_ADMIN_KEY", CHIAVE),
                  mock.patch.object(main, "_pulizia_connessione", lambda: (_Conn(self.db), "?"))):
            p.start()
            self.addCleanup(p.stop)

    def conta(self, tabella, dove="1=1"):
        return self.db.execute(f"SELECT COUNT(*) FROM {tabella} WHERE {dove}").fetchone()[0]

    def fotografia(self):
        return {t: self.db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("messages", "richieste_operatore", "strumenti_log", "chat_stato",
                          "feedback", "knowledge_documents")}

    def post(self, **params):
        return self.client.post("/admin/pulizia-prove", params=params, headers={"x-bot-admin-key": CHIAVE})

    def test_senza_chiave_401(self):
        prima = self.fotografia()
        self.assertEqual(self.client.post("/admin/pulizia-prove").status_code, 401)
        r = self.client.post("/admin/pulizia-prove", params={"conferma": main.PULIZIA_CONFERMA},
                             headers={"x-bot-admin-key": "sbagliata"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.fotografia(), prima)

    def test_anteprima_non_cancella(self):
        prima = self.fotografia()
        r = self.post()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["modalita"], "anteprima")
        self.assertEqual(d["chat_id"], sorted(PROVE))
        self.assertEqual((d["chat"], d["messaggi"], d["messaggi_operatore"]), (3, 5, 1))
        self.assertEqual([x["chat_id"] for x in d["richieste"]], ["TEST-OP-1", "TEST-RICH-2"])
        self.assertEqual((d["righe_strumenti_log"], d["righe_chat_stato"]), (2, 1))
        self.assertEqual([x["chat_id"] for x in d["da_guardare_non_cancellate"]], ["chat-vera-con-prova"])
        self.assertEqual(self.fotografia(), prima)
        # una conferma sbagliata e' ancora solo anteprima
        d = self.post(conferma="si").json()
        self.assertEqual(d["modalita"], "anteprima")
        self.assertEqual(self.fotografia(), prima)

    def test_cancella_solo_le_prove(self):
        prima = self.fotografia()
        d = self.post(conferma=main.PULIZIA_CONFERMA).json()
        self.assertEqual(d["esito"], "cancellato")
        self.assertEqual(d["righe_cancellate"],
                         {"messages": 5, "richieste_operatore": 2, "strumenti_log": 2, "chat_stato": 1})
        for tabella in ("messages", "richieste_operatore", "strumenti_log", "chat_stato"):
            self.assertEqual(self.conta(tabella, "substr(chat_id, 1, 5) = 'TEST-'"), 0, tabella)
        dopo = self.fotografia()
        # le righe rimaste sono ESATTAMENTE quelle di prima che non erano prove
        for tabella, righe in prima.items():
            col = {"messages": 3, "richieste_operatore": 1, "strumenti_log": 1, "chat_stato": 0}.get(tabella)
            attese = [r for r in righe if col is None or not str(r[col]).startswith("TEST-")]
            self.assertEqual(dopo[tabella], attese, tabella)
        self.assertEqual(self.conta("messages", "chat_id = 'cliente-vero-123'"), 2)
        self.assertEqual(self.conta("messages", "chat_id = 'test-minuscolo'"), 1)
        self.assertEqual(self.conta("messages", "chat_id = 'chat-TEST-dentro'"), 1)
        self.assertEqual(self.conta("richieste_operatore", "chat_id = 'chat-vera-con-prova'"), 1)
        # una seconda volta non c'e' piu' niente
        self.assertEqual(self.post(conferma=main.PULIZIA_CONFERMA).json()["esito"], "niente da cancellare")

    def test_una_chat_estranea_blocca_tutto(self):
        prima = self.fotografia()
        with mock.patch.object(main, "_pulizia_candidati",
                               lambda cur, ph: sorted(PROVE + ["cliente-vero-123"])):
            d = self.post(conferma=main.PULIZIA_CONFERMA).json()
        self.assertTrue(d["esito"].startswith("BLOCCATA"))
        self.assertEqual(d["chat_estranee"], ["cliente-vero-123"])
        self.assertEqual(self.fotografia(), prima)


if __name__ == "__main__":
    unittest.main()
