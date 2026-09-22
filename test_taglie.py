# -*- coding: utf-8 -*-
"""Test di taglie.py: (1) ogni valore delle tabelle trascritte compare nel
testo della guida dentro manuale_operativo.docx; (2) la funzione
taglia_consigliata su casi unica / doppia / fuori tabella / kids.
    python -m unittest test_taglie -v
"""
import os
import re
import unittest

import taglie
from taglie import taglia_consigliata, estrai_misure, decidi_taglia, TAGLIA_RE

QUI = os.path.dirname(os.path.abspath(__file__))


def testo_guida():
    from docx import Document
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    doc = Document(os.path.join(QUI, "manuale_operativo.docx"))
    righe = []
    for el in doc.element.body:
        tag = el.tag.split("}")[-1]
        if tag == "p":
            righe.append(Paragraph(el, doc).text)
        elif tag == "tbl":
            for row in Table(el, doc).rows:
                for c in row.cells:
                    righe.append(c.text)
    t = "\n".join(righe)
    i = t.index("Unified Size Guide")
    j = t.index("PAGAMENTI, FATTURE", i)
    return t[i:j]


class TabelleNellaGuida(unittest.TestCase):
    """Ogni valore trascritto deve comparire nel testo della guida, scritto
    come lo scrive la guida."""

    @classmethod
    def setUpClass(cls):
        cls.g = testo_guida()

    def _fascia(self, t, lo, hi):
        if lo is None and hi is None:
            return f"{t} per tutti i pesi"
        if lo is None:
            return f"{t} fino a {hi} kg"
        if lo == hi:
            return f"{t} {lo} kg"
        return f"{t} {lo}-{hi} kg"

    def test_gi_adulto(self):
        for lo, hi, celle in taglie.GI_ADULTO:
            riga = f"Altezza {lo} cm" if lo == hi else f"Altezza {lo}-{hi} cm"
            self.assertIn(riga, self.g)
            inizio = self.g.index(riga)
            fine = self.g.index("\n", inizio)
            riga_guida = self.g[inizio:fine]
            for t, pmin, pmax in celle:
                self.assertIn(self._fascia(t, pmin, pmax), riga_guida, f"{riga}: {t}")
            # e nessuna taglia in piu' rispetto alla riga della guida
            self.assertEqual(len(re.findall(r"\bA\d[LS]?\b", riga_guida)), len(celle), riga)

    def test_adulto_peso(self):
        for lo, hi, bassa, alta in taglie.ADULTO_PESO:
            if lo is None:
                atteso = f"Sotto {hi} kg: {bassa}-{alta}"
            elif hi is None:
                atteso = f"{lo} kg e oltre: {bassa}-{alta}"
            else:
                atteso = f"{lo}-{hi} kg: {bassa}-{alta}"
            self.assertIn(atteso, self.g)
        self.assertIn(f"statura alta circa {taglie.STATURA_ALTA_CM} cm o piu", self.g)

    def test_kids_gi(self):
        for sigla, h, emin, emax in taglie.KIDS_GI:
            self.assertIn(f"{sigla} = {h} cm / {emin}-{emax} anni", self.g)

    def test_kids_rashguard_shorts(self):
        for t, emin, emax, a, b in taglie.KIDS_RASHGUARD:
            self.assertIn(f"{t} = {emin}-{emax} anni / A {a} / B {b}", self.g)
        for t, emin, emax, a, b in taglie.KIDS_SHORTS:
            self.assertIn(f"{t} = {emin}-{emax} anni / vita {a} / gamba {b}", self.g)


class Funzione(unittest.TestCase):

    def esito(self, *a, **k):
        r = taglia_consigliata(*a, **k)
        return r["esito"], r["taglie"], r["testo"]["it"], r["testo"]["en"]

    def test_gi_unica(self):
        e, t, it, en = self.esito("gi", 180, 85)
        self.assertEqual((e, t), ("unica", ["A2L"]))
        self.assertEqual(it, "Per 180 cm e 85 kg la taglia consigliata è A2L. In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere.")
        self.assertEqual(en, "For 180 cm and 85 kg the recommended size is A2L. If in doubt, write «operator» and we'll help you choose.")

    def test_gi_esempio_guida_donna_174_60(self):
        e, t, _, _ = self.esito("gi", 174, 60)
        self.assertEqual((e, t), ("unica", ["A1L"]))

    def test_gi_doppia_variante_L(self):
        # 178 cm sta fra la riga 170-175 (A2 a 80 kg) e la riga 180 (A2L a 80 kg):
        # non sono due corporature, e' la stessa taglia piu' lunga.
        e, t, it, en = self.esito("gi", 178, 80)
        self.assertEqual((e, t), ("doppia", ["A2", "A2L"]))
        self.assertEqual(it, "Per 178 cm e 80 kg puoi prendere A2: A2 è la taglia standard, A2L è più lunga, "
                             "per chi è più alto. In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere.")
        self.assertEqual(en, "For 178 cm and 80 kg you can take A2: A2 is the standard size, A2L is longer, "
                             "for taller people. If in doubt, write «operator» and we'll help you choose.")

    def test_gi_doppia_A1_A1L(self):
        # 167 cm, 65 kg: riga 165 (A1) e riga 170-175 (A1L)
        e, t, it, _ = self.esito("gi", 167, 65)
        self.assertEqual((e, t), ("doppia", ["A1", "A1L"]))
        self.assertIn("puoi prendere A1: A1 è la taglia standard, A1L è più lunga", it)

    def test_doppia_normale_resta_aderente_comodo(self):
        # coppie che NON sono variante L: A3S/A3 e XL/XXL
        _, t, it, _ = self.esito("gi", 182, 110)
        self.assertEqual(t, ["A3S", "A3"])
        self.assertIn("calza più aderente", it)
        _, t, it, _ = self.esito("shorts", 190, 95)
        self.assertEqual(t, ["XL", "XXL"])
        self.assertIn("XL calza più aderente, XXL più comodo", it)

    def test_gi_doppia_confine_peso(self):
        # 165 cm, 72 kg: fra "A1 fino a 70" e "A2 75-90"
        e, t, _, _ = self.esito("gi", 165, 72)
        self.assertEqual((e, t), ("doppia", ["A1", "A2"]))

    def test_gi_peso_esatto_sul_limite(self):
        e, t, _, _ = self.esito("gi", 165, 70)
        self.assertEqual((e, t), ("unica", ["A1"]))

    def test_gi_fuori_tabella_altezza(self):
        e, t, it, en = self.esito("gi", 210, 130)
        self.assertEqual((e, t), ("fuori_tabella", []))
        self.assertEqual(it, "Per 210 cm e 130 kg la guida non copre la tua misura. In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere.")
        self.assertIn("doesn't cover", en)

    def test_gi_fuori_tabella_peso(self):
        e, t, _, _ = self.esito("gi", 180, 140)
        self.assertEqual((e, t), ("fuori_tabella", []))

    def test_gi_200_tutti_i_pesi(self):
        e, t, _, _ = self.esito("gi", 200, 140)
        self.assertEqual((e, t), ("unica", ["A5"]))

    def test_rashguard_unica_statura_bassa(self):
        e, t, _, _ = self.esito("rashguard", 165, 62)
        self.assertEqual((e, t), ("unica", ["XS"]))

    def test_rashguard_esempio_guida_174_60(self):
        e, t, _, _ = self.esito("rashguard", 174, 60)
        self.assertEqual((e, t), ("unica", ["S"]))

    def test_shorts_doppia_confine_peso(self):
        # 75 kg sta in "65-75" e in "75-80": statura alta -> M e L
        e, t, _, _ = self.esito("shorts", 180, 75)
        self.assertEqual((e, t), ("doppia", ["M", "L"]))

    def test_shorts_vuoto_90_100(self):
        e, t, _, _ = self.esito("shorts", 190, 95)
        self.assertEqual((e, t), ("doppia", ["XL", "XXL"]))

    def test_rashguard_donna_nota(self):
        e, t, it, en = self.esito("rashguard", 168, 60, donna=True)
        self.assertEqual((e, t), ("unica", ["XS"]))
        self.assertIn("Le taglie donna sono più aderenti", it)
        self.assertIn("Women's sizes fit tighter", en)
        self.assertTrue(it.endswith("In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere."))

    def test_kimono_donna_non_coperto(self):
        e, t, it, _ = self.esito("gi", 168, 60, donna=True)
        self.assertEqual((e, t), ("non_coperto", []))
        self.assertEqual(it, "Per il kimono donna non ho una tabella affidabile: scrivi «operatore» e ti aiutiamo a scegliere.")

    def test_kids_gi_altezza_ed_eta(self):
        # 128 cm sta fra M1 (120) e M2 (130); 8 anni e' M2 -> M2
        e, t, it, _ = self.esito("kids_gi", 128, 27, eta=8)
        self.assertEqual((e, t), ("unica", ["M2"]))
        self.assertTrue(it.startswith("Per 128 cm e 27 kg e 8 anni la taglia consigliata è M2."))

    def test_kids_gi_solo_altezza_confine(self):
        e, t, _, _ = self.esito("kids_gi", 128)
        self.assertEqual((e, t), ("doppia", ["M1", "M2"]))

    def test_kids_gi_altezza_esatta(self):
        e, t, _, _ = self.esito("kids_gi", 140)
        self.assertEqual((e, t), ("unica", ["M3"]))

    def test_kids_gi_fuori(self):
        e, t, _, _ = self.esito("kids_gi", 170, eta=13)
        self.assertEqual((e, t), ("fuori_tabella", []))

    def test_kids_rashguard_per_eta(self):
        e, t, _, _ = self.esito("kids_rashguard", None, None, eta=8)
        self.assertEqual((e, t), ("unica", ["M"]))
        e, t, _, _ = self.esito("kids_shorts", None, None, eta=4)
        self.assertEqual((e, t), ("fuori_tabella", []))

    def test_dati_mancanti(self):
        r = taglia_consigliata("gi", 178)
        self.assertEqual((r["esito"], r["mancano"]), ("dati_mancanti", ["peso"]))
        self.assertEqual(r["testo"]["it"], "Per consigliarti la taglia mi serve ancora il peso.")
        r = taglia_consigliata(None, 178)
        self.assertEqual((r["esito"], r["mancano"]), ("dati_mancanti", ["prodotto", "peso"]))
        self.assertEqual(r["testo"]["it"], "Per consigliarti la taglia mi serve ancora il prodotto (kimono, rashguard o shorts) e il peso.")
        r = taglia_consigliata("kids_rashguard", 130, 30)
        self.assertEqual(r["mancano"], ["eta"])


class DatiMancanti(unittest.TestCase):

    def test_solo_altezza(self):
        r = taglia_consigliata(None, 178)
        self.assertEqual((r["esito"], r["mancano"]), ("dati_mancanti", ["prodotto", "peso"]))
        self.assertEqual(r["testo"]["it"], "Per consigliarti la taglia mi serve ancora il prodotto "
                                           "(kimono, rashguard o shorts) e il peso.")

    def test_solo_peso(self):
        r = taglia_consigliata("gi", None, 80)
        self.assertEqual((r["esito"], r["mancano"]), ("dati_mancanti", ["altezza"]))
        self.assertEqual(r["testo"]["it"], "Per consigliarti la taglia mi serve ancora l'altezza.")
        self.assertEqual(r["testo"]["en"], "To recommend a size I still need your height.")

    def test_solo_prodotto(self):
        r = taglia_consigliata("gi")
        self.assertEqual((r["esito"], r["mancano"]), ("dati_mancanti", ["altezza", "peso"]))
        self.assertEqual(r["testo"]["it"], "Per consigliarti la taglia mi serve ancora l'altezza e il peso.")

    def test_niente(self):
        r = taglia_consigliata(None)
        self.assertEqual(r["mancano"], ["prodotto", "altezza", "peso"])

    def test_kids_gi_solo_eta(self):
        r = taglia_consigliata("kids_gi", None, None, eta=8)
        self.assertEqual((r["esito"], r["taglie"]), ("unica", ["M2"]))
        r = taglia_consigliata("kids_gi", None, None, eta=5)
        self.assertEqual((r["esito"], r["taglie"]), ("doppia", ["M1", "M0"][::-1]))


class DecisioneDelTurno(unittest.TestCase):
    """decidi_taglia: quando il turno NON passa dal modello."""

    def test_chiede_senza_dati(self):
        d = decidi_taglia("sono alto 178, che taglia?")
        self.assertEqual(d["rete"], "rete_taglie_dati_mancanti")
        self.assertEqual(d["testo"], "Per consigliarti la taglia mi serve ancora il prodotto "
                                     "(kimono, rashguard o shorts) e il peso.")

    def test_peso_non_si_deduce(self):
        d = decidi_taglia("sono alto 1,78 m, che kimono prendo?")
        self.assertEqual(d["rete"], "rete_taglie_dati_mancanti")
        self.assertIn("il peso", d["testo"])

    def test_prodotto_e_peso_senza_parola_taglia(self):
        d = decidi_taglia("kimono, peso 80")
        self.assertEqual(d["rete"], "rete_taglie_dati_mancanti")
        self.assertEqual(d["testo"], "Per consigliarti la taglia mi serve ancora l'altezza.")

    def test_diretta(self):
        d = decidi_taglia("178 cm 80 kg kimono")
        self.assertEqual((d["rete"], d["esito"], d["taglie"]), ("rete_taglie_diretta", "doppia", ["A2", "A2L"]))
        self.assertIn("A2 è la taglia standard", d["testo"])
        d = decidi_taglia("rashguard 165 cm 62 kg")
        self.assertEqual((d["rete"], d["esito"], d["taglie"]), ("rete_taglie_diretta", "unica", ["XS"]))

    def test_inglese(self):
        d = decidi_taglia("what size for 178 cm and 80 kg gi?", "en")
        self.assertEqual(d["esito"], "doppia")
        self.assertIn("A2 is the standard size", d["testo"])

    def test_domande_che_restano_al_modello(self):
        for msg in ("la A2 è disponibile in blu?",
                    "posso cambiare taglia dopo l'acquisto?",
                    "quanto costa la spedizione in Italia?",
                    "il kimono blu costa 189 euro?",
                    "che colori avete?"):
            self.assertIsNone(decidi_taglia(msg), msg)


class LetturaMessaggio(unittest.TestCase):

    def test_misure(self):
        m = estrai_misure("che taglia di kimono per 178 cm e 80 kg?")
        self.assertEqual((m["altezza"], m["peso"], m["prodotto"], m["donna"]), (178, 80, "gi", False))
        m = estrai_misure("shorts 1,90 m 95 kg")
        self.assertEqual((m["altezza"], m["peso"], m["prodotto"]), (190, 95, "shorts"))
        m = estrai_misure("rashguard, 165 cm e 62 kg")
        self.assertEqual((m["altezza"], m["peso"], m["prodotto"]), (165, 62, "rashguard"))
        m = estrai_misure("kimono per mio figlio di 8 anni, 128 cm e 27 kg")
        self.assertEqual((m["altezza"], m["peso"], m["eta"], m["prodotto"]), (128, 27, 8, "kids_gi"))
        m = estrai_misure("sono alto 178, che taglia?")
        self.assertEqual((m["altezza"], m["peso"], m["prodotto"]), (178, None, None))
        m = estrai_misure("kimono donna 168 cm 60 kg")
        self.assertEqual((m["altezza"], m["peso"], m["prodotto"], m["donna"]), (168, 60, "gi", True))
        m = estrai_misure("178 80 chili kimono")
        self.assertEqual((m["altezza"], m["peso"]), (178, 80))

    def test_taglia_del_cliente_non_e_un_consiglio(self):
        messaggio = "la A2 è disponibile in blu?"
        risposta = "La disponibilità della A2 in blu è sulla pagina del prodotto."
        nel_messaggio = set(TAGLIA_RE.findall(messaggio))
        nuove = [t for t in TAGLIA_RE.findall(risposta) if t not in nel_messaggio]
        self.assertEqual(nuove, [])

    def test_regex_taglia(self):
        self.assertTrue(TAGLIA_RE.search("la taglia è A2L"))
        self.assertTrue(TAGLIA_RE.search("ti consiglio la M"))
        self.assertTrue(TAGLIA_RE.search("M2 per tuo figlio"))
        self.assertTrue(TAGLIA_RE.search("XL o XXL"))
        self.assertIsNone(TAGLIA_RE.search("L'ordine parte domani. S'intende che il costo è 5,90 euro."))
        self.assertIsNone(TAGLIA_RE.search("Ciao, sono Adelpina, il risponditore AI di Kano Kimonos."))


if __name__ == "__main__":
    unittest.main()
