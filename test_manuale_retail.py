# -*- coding: utf-8 -*-
"""Il cliente finale non riceve dal manuale le sezioni "TERMINI B2B" e
"USO INTERNO"; lo staff le riceve come prima. Indice costruito dal docx vero
con le stesse funzioni del bot, senza DB e senza modello."""
import os
import unittest

import main

CARTELLA = os.path.dirname(os.path.abspath(__file__))


def _indice_dal_docx():
    testo = main.extract_text_from_docx(os.path.join(CARTELLA, "manuale_operativo.docx"))
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


class TestManualeRetail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.indice = _indice_dal_docx()
        cls._originali = (main._carica_indice, main.get_size_guide_block)
        main._carica_indice = lambda: cls.indice
        # la guida taglie si legge dal DB: qui non serve, si passa oltre
        main.get_size_guide_block = lambda: ""

    @classmethod
    def tearDownClass(cls):
        main._carica_indice, main.get_size_guide_block = cls._originali

    def test_le_sezioni_esistono_nel_manuale(self):
        titoli = [s["titolo"] for s in self.indice["sezioni"]]
        self.assertTrue(any(t.startswith("TERMINI B2B") for t in titoli))
        self.assertTrue(any(t.startswith("USO INTERNO") for t in titoli))

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

    def test_staff_riceve_ancora_tutto(self):
        testo = main.tool_rispondi_dal_manuale("reso", "reso", role="staff")
        self.assertIn("TERMINI B2B", testo)
        testo = main.tool_rispondi_dal_manuale("magazzino in slovenia", "", role="staff")
        self.assertIn("Vipava", testo)

    def test_b2b_come_prima(self):
        self.assertEqual(main.sezioni_escluse("b2b"), ())
        self.assertEqual(main.sezioni_escluse("staff"), ())
        self.assertEqual(main.get_knowledge_context("reso", escludi=main.sezioni_escluse("b2b")),
                         main.get_knowledge_context("reso"))


if __name__ == "__main__":
    unittest.main()
