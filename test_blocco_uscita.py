# -*- coding: utf-8 -*-
"""Test di blocco_uscita.py: quello che non deve uscire, e soprattutto quello
che deve passare intatto.
    python -m unittest test_blocco_uscita -v
"""
import unittest

from blocco_uscita import blocco_uscita_retail, TESTO_PAGAMENTI, TESTO_GENERICO, NOMI_STAFF


class Pagamenti(unittest.TestCase):

    def _bloccato(self, testo, termine_atteso=None):
        r = blocco_uscita_retail(testo)
        self.assertTrue(r["bloccato"], testo)
        self.assertEqual(r["categoria"], "pagamenti")
        self.assertEqual(r["testo"], TESTO_PAGAMENTI["it"])
        if termine_atteso:
            self.assertEqual(r["termine"].replace(" ", ""), termine_atteso.replace(" ", ""))
        return r

    def test_iban_senza_spazi(self):
        self._bloccato("Puoi fare il bonifico su IT60X0542811101000000123456 intestato a noi.")

    def test_iban_con_spazi(self):
        self._bloccato("Le coordinate sono IT60 X054 2811 1010 0000 0123 456.")

    def test_iban_senza_la_parola_iban(self):
        r = blocco_uscita_retail("Il conto è DE89370400440532013000, grazie.")
        self.assertEqual((r["bloccato"], r["categoria"]), (True, "pagamenti"))

    def test_parola_iban(self):
        self._bloccato("Ti mando l'IBAN per email.", "IBAN")

    def test_bic_e_swift(self):
        self._bloccato("Il BIC è quello della banca.", "BIC")
        self._bloccato("Serve anche il codice SWIFT.", "SWIFT")

    def test_bic_nudo(self):
        r = blocco_uscita_retail("Banca: UNCRITMMXXX per il bonifico.")
        self.assertEqual((r["bloccato"], r["categoria"], r["termine"]), (True, "pagamenti", "UNCRITMMXXX"))

    def test_coordinate_bancarie_e_bank_details(self):
        self._bloccato("Ti giro le coordinate bancarie.")
        r = blocco_uscita_retail("I'll send you our bank details.")
        self.assertEqual(r["testo"], TESTO_PAGAMENTI["en"])

    def test_parola_maiuscola_non_e_un_bic(self):
        # 8 lettere maiuscole ma senza codice paese in quinta-sesta posizione
        for parola in ("ADELPINA", "KIMONOSX", "SPEDIZIO"):
            r = blocco_uscita_retail(f"Scritto tutto maiuscolo: {parola}.")
            self.assertFalse(r["bloccato"], parola)


class Persone(unittest.TestCase):

    def test_ogni_nome_staff(self):
        for nome in NOMI_STAFF:
            r = blocco_uscita_retail(f"Ti risponde {nome} appena possibile.")
            self.assertEqual((r["bloccato"], r["categoria"], r["termine"]), (True, "staff", nome), nome)
            self.assertEqual(r["testo"], TESTO_GENERICO["it"])

    def test_nome_minuscolo_e_dentro_una_frase(self):
        r = blocco_uscita_retail("se ne occupa mauro, ti scrive lui")
        self.assertEqual((r["bloccato"], r["categoria"]), (True, "staff"))

    def test_cognomi_sempre_bloccati(self):
        for cognome in ("Tomasetti", "Danesin"):
            r = blocco_uscita_retail(f"Scrivi a {cognome}.", messaggi_cliente=[f"sono io, {cognome}"])
            self.assertEqual((r["bloccato"], r["categoria"], r["termine"]), (True, "staff", cognome), cognome)

    def test_cliente_che_si_chiama_andrea(self):
        r = blocco_uscita_retail("Ciao Andrea, la taglia consigliata è A2.",
                                 messaggi_cliente=["ciao sono Andrea, che taglia prendo?"])
        self.assertFalse(r["bloccato"])
        self.assertEqual(r["testo"], "Ciao Andrea, la taglia consigliata è A2.")

    def test_cliente_mauro_ma_cognome_no(self):
        messaggi = ["buongiorno, sono Mauro"]
        self.assertFalse(blocco_uscita_retail("Ciao Mauro, dimmi pure.", messaggi)["bloccato"])
        self.assertTrue(blocco_uscita_retail("Ti risponde Mauro Danesin.", messaggi)["bloccato"])

    def test_nome_non_scritto_dal_cliente(self):
        r = blocco_uscita_retail("Ti risponde Andrea.", messaggi_cliente=["ciao sono Luca"])
        self.assertEqual((r["bloccato"], r["termine"]), (True, "Andrea"))

    def test_messaggi_cliente_pigri(self):
        chiamate = []

        def leggi():
            chiamate.append(1)
            return ["sono Andrea"]

        # risposta pulita: la lista dei messaggi non serve nemmeno leggerla
        blocco_uscita_retail("La taglia consigliata è A2.", leggi)
        self.assertEqual(chiamate, [])
        blocco_uscita_retail("Ciao Andrea.", leggi)
        self.assertEqual(len(chiamate), 1)


class Piattaforme(unittest.TestCase):

    def test_kanokimonos_app(self):
        r = blocco_uscita_retail("Trovi l'ordine su kanokimonos.app nella tua area.")
        self.assertEqual((r["bloccato"], r["categoria"], r["termine"]), (True, "piattaforme", "kanokimonos.app"))

    def test_fully_maiuscolo(self):
        r = blocco_uscita_retail("Il pacco è partito dal magazzino Fully.")
        self.assertEqual((r["bloccato"], r["categoria"], r["termine"]), (True, "piattaforme", "Fully"))

    def test_fully_minuscolo_passa(self):
        for testo in ("The waistband is fully adjustable.",
                      "La cintura è fully adjustable, come da scheda."):
            r = blocco_uscita_retail(testo)
            self.assertFalse(r["bloccato"], testo)
            self.assertEqual(r["testo"], testo)

    def test_fully_si_e_fullyview(self):
        self.assertTrue(blocco_uscita_retail("vedi su fully.si")["bloccato"])
        self.assertTrue(blocco_uscita_retail("aperto in FullyView")["bloccato"])

    def test_kanokimonos_com_passa(self):
        testo = "La disponibilità è sulla pagina del prodotto su kanokimonos.com."
        self.assertEqual(blocco_uscita_retail(testo)["testo"], testo)


class RisposteNormali(unittest.TestCase):
    """Il grosso del lavoro: non rompere niente."""

    BUONE = [
        "Per 178 cm e 80 kg puoi prendere A2: A2 è la taglia standard, A2L è più lunga, per chi è più alto. "
        "In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere.",
        "Per 165 cm e 62 kg la taglia consigliata è XS. In caso di dubbi scrivi «operatore» e ti aiutiamo a scegliere.",
        "Per consigliarti la taglia mi serve ancora il prodotto (kimono, rashguard o shorts) e il peso.",
        "Posso aprire una richiesta a un operatore: risponde qui in chat, di norma entro alcune ore in orario di "
        "lavoro. Vuoi che la apra, o preferisci prima provare a chiedere a me?",
        "Richiesta aperta. Un operatore ti risponde qui in chat, di norma entro alcune ore in orario di lavoro. "
        "Puoi chiudere e tornare più tardi: la conversazione resta.",
        "Va bene, dimmi pure.",
        "L'operatore ha chiuso la conversazione. Se hai altre domande sono qui.",
        "Ciao, sono Adelpina, il risponditore AI di Kano Kimonos. Buongiorno, come posso aiutarti?",
        "Il costo della spedizione in Italia per i prodotti da catalogo è di 5,90 euro. I tempi sono di 2-3 giorni.",
        "Si paga completando l'ordine su kanokimonos.com con carta o PayPal.",
        "Le immagini vanno inviate in formato vettoriale (.AI, .EPS, .PDF, .SVG). Se hai solo un file raster "
        "(JPEG, PNG) lo accettiamo lo stesso: la conversione la facciamo noi e il costo e' di 25 euro a immagine.",
        "For 180 cm and 85 kg the recommended size is A2L. If in doubt, write «operator» and we'll help you choose.",
        "Payment is completed at checkout on kanokimonos.com.",
        "La taglia A2 corrisponde a una vestibilità standard; la A3 è più ampia.",
        "Il kimono è disponibile in bianco, blu e nero: la disponibilità aggiornata è sulla pagina del prodotto.",
    ]

    def test_passano_identiche(self):
        for testo in self.BUONE:
            r = blocco_uscita_retail(testo)
            self.assertFalse(r["bloccato"], f"BLOCCATA per {r['categoria']}/{r['termine']}: {testo[:80]}")
            self.assertEqual(r["testo"], testo)

    def test_vuoto_e_none(self):
        self.assertFalse(blocco_uscita_retail("")["bloccato"])
        self.assertFalse(blocco_uscita_retail(None)["bloccato"])
        self.assertEqual(blocco_uscita_retail(None)["testo"], "")


class Lingua(unittest.TestCase):

    def test_testo_fisso_nella_lingua_della_risposta(self):
        r = blocco_uscita_retail("Our bank details: the IBAN is IT60X0542811101000000123456.")
        self.assertEqual(r["testo"], TESTO_PAGAMENTI["en"])
        r = blocco_uscita_retail("The order is on kanokimonos.app, you can check it there.")
        self.assertEqual(r["testo"], TESTO_GENERICO["en"])
        r = blocco_uscita_retail("L'ordine lo trovi su kanokimonos.app nella tua area personale.")
        self.assertEqual(r["testo"], TESTO_GENERICO["it"])

    def test_lingua_forzata(self):
        r = blocco_uscita_retail("Ti risponde Ivan.", lingua="en")
        self.assertEqual(r["testo"], TESTO_GENERICO["en"])


if __name__ == "__main__":
    unittest.main()
