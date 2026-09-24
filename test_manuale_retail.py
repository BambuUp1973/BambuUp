# -*- coding: utf-8 -*-
"""Il cliente finale riceve dal manuale SOLO le sezioni della lista bianca
(SEZIONI_RETAIL_AMMESSE, approvata da Bambu il 24/09/2026); staff e b2b
ricevono tutto. Indice costruito dal docx vero con le stesse funzioni del
bot, senza DB e senza modello."""
import os
import re
import unittest

import main

CARTELLA = os.path.dirname(os.path.abspath(__file__))

DOMANDE_VERIFICA = [
    "quanto costa la spedizione in Italia?", "reso", "pagamento", "bonifico", "cambio taglia",
    "come posso pagare?", "carta di credito", "posso pagare con carta?", "link di pagamento",
]
VIETATI = {
    "IBAN": r"\bIBAN\b|LT29\d",
    "BIC": r"\bBIC\b|REVOLT21|\bSWIFT\b",
    "beneficiario": r"(?i)beneficiar|Kano Co\. Limited",
    "causale": r"(?i)\bcausal[ei]\b",
    "+3%": r"\+3%|costi gestione",
    "link di pagamento": r"(?i)revolut|paypal\.me|/pay\b|checkout\.",
    "nome e cognome": r"Danesin|Tomasetti",
    "Fully": r"\bFully\b|fullyview|fully\.si",
    "kanokimonos.app": r"(?i)kanokimonos\.app",
}


def _testo_docx():
    return main.extract_text_from_docx(os.path.join(CARTELLA, "manuale_operativo.docx"))


def _indice_dal_docx():
    testo = _testo_docx()
    chunks, inizio = [], 0
    while inizio < len(testo):
        fine = inizio + 4000
        chunks.append(testo[inizio:fine])
        if fine >= len(testo):
            break
        inizio = fine - main.KNOWLEDGE_CHUNK_OVERLAP
    indice = main._indicizza_manuale(
        [(f"Manuale Operativo Kano - Parte {i + 1}", c) for i, c in enumerate(chunks)])
    indice["firma"] = "test"
    return indice


def _titoli_consegnati(testo):
    return [re.sub(r"^\[MANUALE - SEZIONE(?: 2, DISTINTA dalla prima)?: ", "", t).rstrip("]")
            for t in re.findall(r"^\[MANUALE - SEZIONE[^\]]*\]", testo, re.M)]


class TestManualeRetail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.indice = _indice_dal_docx()
        testo = _testo_docx()
        cls._originali = (main._carica_indice, main._reconstruct_manuale_text)
        main._carica_indice = lambda: cls.indice
        # la guida taglie si legge dal testo ricucito del DB: qui dal docx
        main._reconstruct_manuale_text = lambda: testo
        main._INDICI_FILTRATI.clear()

    @classmethod
    def tearDownClass(cls):
        main._carica_indice, main._reconstruct_manuale_text = cls._originali
        main._INDICI_FILTRATI.clear()

    def test_ogni_voce_della_lista_bianca_trova_una_sezione(self):
        titoli = [s["titolo"] for s in self.indice["sezioni"]]
        for prefisso in main.SEZIONI_RETAIL_AMMESSE:
            with self.subTest(prefisso=prefisso):
                self.assertEqual(sum(t.startswith(prefisso) for t in titoli), 1)

    def test_il_retail_vede_solo_la_lista_bianca(self):
        f = main._indice_solo(self.indice, main.sezioni_ammesse("retail"))
        self.assertEqual(len(f["sezioni"]), len(main.SEZIONI_RETAIL_AMMESSE))
        for s in f["sezioni"]:
            self.assertTrue(s["titolo"].startswith(main.SEZIONI_RETAIL_AMMESSE), s["titolo"])
        righe_ammesse = {r for s in f["sezioni"] for r in s["righe"]}
        self.assertTrue(set(f["righe"]) <= righe_ammesse)
        # e nessuna delle altre, per titolo
        fuori = [s["titolo"] for s in self.indice["sezioni"] if s not in f["sezioni"]]
        self.assertIn("CONSIGLI TAGLIE", " ".join(fuori))
        self.assertTrue(any(t.startswith("TERMINI B2B") for t in fuori))
        self.assertTrue(any(t.startswith("USO INTERNO") for t in fuori))

    def test_staff_e_b2b_vedono_tutto(self):
        for ruolo in ("staff", "b2b"):
            self.assertIsNone(main.sezioni_ammesse(ruolo))
            self.assertIs(main._indice_solo(self.indice, main.sezioni_ammesse(ruolo)), self.indice)
        self.assertEqual(main.get_knowledge_context("reso", ammesse=main.sezioni_ammesse("b2b")),
                         main.get_knowledge_context("reso"))
        testo = main.tool_rispondi_dal_manuale("reso", "reso", role="staff")
        self.assertIn("TERMINI B2B", testo)
        testo = main.tool_rispondi_dal_manuale("magazzino in slovenia", "", role="staff")
        self.assertIn("Vipava", testo)
        testo = main.get_knowledge_context("carta di credito")
        self.assertIn("IBAN", testo)

    def test_retail_non_riceve_b2b_ne_uso_interno(self):
        for domanda in ("reso", "prodotto difettoso", "return policy refund",
                        "indirizzo per il reso", "magazzino in slovenia"):
            with self.subTest(domanda=domanda):
                testo = main.tool_rispondi_dal_manuale(domanda, domanda, role="retail")
                for vietato in ("TERMINI B2B", "USO INTERNO", "Vipava", "2,00", "2.00", "coupon"):
                    self.assertNotIn(vietato, testo)

    def test_retail_riceve_il_blocco_resi(self):
        for domanda in ("reso", "cambio taglia", "indirizzo per il reso"):
            with self.subTest(domanda=domanda):
                testo = main.tool_rispondi_dal_manuale(domanda, domanda, role="retail")
                self.assertIn("RESI E CAMBI — CLIENTI PRIVATI", testo)

    def test_le_9_domande_di_verifica(self):
        for domanda in DOMANDE_VERIFICA:
            with self.subTest(domanda=domanda):
                testo = main.tool_rispondi_dal_manuale(domanda, domanda, role="retail")
                for t in _titoli_consegnati(testo):
                    self.assertTrue(t.startswith(main.SEZIONI_RETAIL_AMMESSE), t)
                for nome, rx in VIETATI.items():
                    self.assertIsNone(re.search(rx, testo), f"{nome} in {domanda!r}")


if __name__ == "__main__":
    unittest.main()
