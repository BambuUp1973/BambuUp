# -*- coding: utf-8 -*-
"""Test di stato_ordine.py: python -m unittest test_stato_ordine"""
import unittest

import stato_ordine as so
from blocco_uscita import blocco_uscita_retail


def ordine(nome="1002-W26", numero=1002, email="cliente@example.com", annullato=False,
           stato=None, evasioni=()):
    return {"name": nome, "order_number": numero, "email": email,
            "cancelled_at": "2026-09-25T10:00:00Z" if annullato else None,
            "fulfillment_status": stato, "fulfillments": list(evasioni)}


def evasione(corriere="BRT", numero="X1", link="https://brt.example/X1", status="success"):
    return {"status": status, "tracking_company": corriere,
            "tracking_numbers": [numero] if numero else [], "tracking_urls": [link] if link else []}


class Normalizzazione(unittest.TestCase):
    def test_numero(self):
        for scritto in ("1002-W26", "#1002-w26", " 1002 - W26 ", "1002W26", "# 1002-W26"):
            self.assertEqual(so.normalizza_numero(scritto), "1002-W26", scritto)
        self.assertEqual(so.normalizza_numero("#1002"), "1002")
        self.assertIsNone(so.normalizza_numero("abc"))

    def test_email(self):
        self.assertEqual(so.normalizza_email("  Cliente@Example.COM "), "cliente@example.com")

    def test_trova_numero(self):
        self.assertEqual(so.trova_numero("ordine 1002-w26 per favore"), "1002-W26")
        self.assertEqual(so.trova_numero("il mio ordine #1002"), "1002")
        self.assertEqual(so.trova_numero("order number 1002"), "1002")
        self.assertEqual(so.trova_numero("ordine n. 1002"), "1002")
        self.assertIsNone(so.trova_numero("1002"))                       # nudo: solo se ammesso
        self.assertEqual(so.trova_numero("1002", anche_nudo=True), "1002")
        self.assertIsNone(so.trova_numero("mario1985@gmail.com", anche_nudo=True))
        self.assertIsNone(so.trova_numero("ordinato il 20/09/2026", anche_nudo=True))
        self.assertIsNone(so.trova_numero("ordine da 150 euro"))


class Analisi(unittest.TestCase):
    def test_chiede_stato(self):
        for m in ("dov'è il mio ordine?", "Dove si trova il mio pacco?", "il mio ordine è partito?",
                  "quando arriva il mio ordine?", "non mi è ancora arrivato il pacco",
                  "mi dai il tracking?", "where is my order?", "has my order shipped yet?",
                  "can I track my order?", "what's the status of my order"):
            a = so.analizza(m)
            self.assertIsNotNone(a, m)
            self.assertEqual(a["azione"], "chiedi", m)
            self.assertEqual(a["mancano"], ["numero", "email"], m)

    def test_non_e_stato_ordine(self):
        for m in ("quanto costa la spedizione in Italia?", "quando arriva se ordino oggi?",
                  "quanto tempo ci mette la spedizione in Germania?", "how long does shipping take?",
                  "che taglia prendo? sono alto 178 cm e peso 80 kg", "avete il kimono A2 nero?",
                  "come si paga sul sito?", "ciao", "how much is shipping to France?"):
            self.assertIsNone(so.analizza(m), m)

    def test_esclusioni(self):
        for m in ("voglio cambiare indirizzo del mio ordine 1002-W26",
                  "vorrei annullare l'ordine 1002-W26", "posso modificare il mio ordine?",
                  "voglio il rimborso del mio ordine", "come faccio il reso del mio ordine?",
                  "I want to change the address of my order", "please cancel my order 1002",
                  "I want a refund for my order", "how do I return my order?",
                  "my order arrived damaged", "ho ricevuto la taglia sbagliata nel mio ordine"):
            self.assertIsNone(so.analizza(m), m)
            self.assertIsNone(so.analizza(m, attesa={"numero": None, "email": None}), m)

    def test_completo(self):
        a = so.analizza("dov'è il mio ordine 1002-W26? email Cliente@Example.com")
        self.assertEqual((a["azione"], a["numero"], a["email"]), ("cerca", "1002-W26", "cliente@example.com"))
        a = so.analizza("1002 cliente@example.com")               # nudo + email
        self.assertEqual((a["azione"], a["numero"]), ("cerca", "1002"))

    def test_parole_dentro_l_email_non_contano(self):
        a = so.analizza("ordine 1002-W26 email reso.sbagliato@example.com")
        self.assertEqual(a["azione"], "cerca")

    def test_solo_numero_poi_email(self):
        a = so.analizza("il mio ordine 1002-W26 è partito?")
        self.assertEqual((a["azione"], a["mancano"]), ("chiedi", ["email"]))
        b = so.analizza("cliente@example.com", attesa={"numero": a["numero"], "email": None})
        self.assertEqual((b["azione"], b["numero"]), ("cerca", "1002-W26"))

    def test_attesa_risposta_nuda(self):
        b = so.analizza("1002 e cliente@example.com", attesa={"numero": None, "email": None})
        self.assertEqual((b["azione"], b["numero"]), ("cerca", "1002"))

    def test_attesa_non_insiste(self):
        self.assertIsNone(so.analizza("e quanto costa la spedizione?", attesa={"numero": None, "email": None}))
        self.assertIsNone(so.analizza("grazie", attesa={"numero": "1002", "email": None}))


class Esiti(unittest.TestCase):
    def esito(self, ordini, numero="1002-W26", email="cliente@example.com"):
        return so.esito_ordine(ordini, numero, email)

    def test_non_spedito(self):
        e = self.esito([ordine()])
        self.assertEqual(e["esito"], "non_spedito")
        self.assertEqual(so.testo_esito(e, "it"), "Il tuo ordine 1002-W26 non è ancora partito.")

    def test_spedito(self):
        e = self.esito([ordine(stato="fulfilled", evasioni=[evasione()])])
        self.assertEqual(e["esito"], "spedito")
        t = so.testo_esito(e, "it")
        self.assertIn("è stato spedito", t)
        self.assertIn("BRT, numero di tracking X1: https://brt.example/X1", t)

    def test_due_spedizioni(self):
        e = self.esito([ordine(stato="fulfilled", evasioni=[evasione(), evasione("GLS", "Y2", "https://gls.example/Y2")])])
        t = so.testo_esito(e, "en")
        self.assertIn("BRT, tracking number X1", t)
        self.assertIn("GLS, tracking number Y2", t)

    def test_in_parte(self):
        e = self.esito([ordine(stato="partial", evasioni=[evasione()])])
        self.assertEqual(e["esito"], "in_parte")
        t = so.testo_esito(e, "it")
        self.assertIn("il resto seguirà", t)
        self.assertIn("X1", t)

    def test_senza_tracking(self):
        e = self.esito([ordine(stato="fulfilled", evasioni=[evasione(numero=None, link=None)])])
        self.assertEqual(e["esito"], "senza_tracking")
        self.assertIn("non è disponibile: scrivi a info@kanokimonos.com", so.testo_esito(e, "it"))
        e = self.esito([ordine(stato="partial", evasioni=[evasione(numero=None, link=None)])])
        self.assertIn("il resto seguirà", so.testo_esito(e, "it"))

    def test_misto(self):
        e = self.esito([ordine(stato="fulfilled", evasioni=[evasione(), evasione(numero=None, link=None)])])
        t = so.testo_esito(e, "it")
        self.assertIn("X1", t)
        self.assertIn("un pacco senza numero di tracking", t)

    def test_evasione_annullata_non_conta(self):
        e = self.esito([ordine(evasioni=[evasione(status="cancelled")])])
        self.assertEqual(e["esito"], "non_spedito")

    def test_annullato_non_si_dice(self):
        e = self.esito([ordine(annullato=True, stato="fulfilled", evasioni=[evasione()])])
        self.assertEqual(e["esito"], "annullato")
        for lingua in ("it", "en"):
            t = so.testo_esito(e, lingua)
            self.assertNotRegex(t.lower(), r"annull|cancel")
            self.assertIn("info@kanokimonos.com", t)
            self.assertEqual(t, so.testo_esito({"esito": "tetto"}, lingua))

    def test_non_trovato_uguale_a_email_diversa(self):
        inesistente = self.esito([])
        email_diversa = self.esito([ordine()], email="altro@example.com")
        senza_email = self.esito([ordine(email=None)])
        for lingua in ("it", "en"):
            t = so.testo_esito(inesistente, lingua)
            self.assertIn("info@kanokimonos.com", t)
            self.assertEqual(t, so.testo_esito(email_diversa, lingua))
            self.assertEqual(t, so.testo_esito(senza_email, lingua))

    def test_numero_parziale_non_combacia(self):
        ordini = [ordine("1001-W26", 1001), ordine("1002-W26", 1002), ordine("1003-W26", 1003)]
        self.assertEqual(self.esito(ordini, numero="100")["esito"], "non_trovato")
        self.assertEqual(self.esito(ordini, numero="1002")["numero"], "1002-W26")
        self.assertEqual(self.esito(ordini, numero="1002-W25")["esito"], "non_trovato")

    def test_niente_dati_dell_ordine(self):
        o = ordine(stato="fulfilled", evasioni=[evasione()])
        o.update({"total_price": "123.45", "shipping_address": {"address1": "Via Segreta 1"},
                  "customer": {"first_name": "Nomefinto"}, "order_status_url": "https://stato.example",
                  "line_items": [{"title": "Kimono Segreto"}]})
        t = so.testo_esito(self.esito([o]), "it")
        for vietato in ("123", "Via Segreta", "Nomefinto", "stato.example", "Kimono Segreto", "cliente@"):
            self.assertNotIn(vietato, t)


class Testi(unittest.TestCase):
    def tutti(self):
        esempi = [
            {"esito": "non_spedito", "numero": "1001-W26", "spedizioni": []},
            {"esito": "spedito", "numero": "1002-W26", "spedizioni": [
                {"corriere": "BRT", "tracking": ["PROVAB0000000001"],
                 "link": ["https://services.brt.it/it/tracking?OP=N&CD=PROVAB0000000001"]}]},
            {"esito": "in_parte", "numero": "1004-W26", "in_parte": True, "spedizioni": [
                {"corriere": "GLS", "tracking": ["P2"], "link": ["https://gls-group.eu/track/P2"]}]},
            {"esito": "senza_tracking", "numero": "1", "spedizioni": [], "in_parte": False},
            {"esito": "senza_tracking", "numero": "1", "spedizioni": [], "in_parte": True},
            {"esito": "annullato"}, {"esito": "non_trovato"}, {"esito": "tetto"}, {"esito": "errore"},
        ]
        for lingua in ("it", "en"):
            for e in esempi:
                yield so.testo_esito(e, lingua)
            for mancano in (["numero", "email"], ["numero"], ["email"]):
                yield so.testo_chiedi(mancano, lingua)

    def test_passano_dal_blocco_intatti(self):
        for t in self.tutti():
            esito = blocco_uscita_retail(t, [])
            testo = esito["testo"] if isinstance(esito, dict) else esito
            self.assertEqual(testo, t, t)

    def test_inglese_senza_italiano(self):
        for e in ({"esito": "non_trovato"}, {"esito": "annullato"}, {"esito": "errore"}):
            self.assertNotIn("scrivi", so.testo_esito(e, "en"))


if __name__ == "__main__":
    unittest.main()
