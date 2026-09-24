# -*- coding: utf-8 -*-
"""Costo di spedizione per il cliente finale (spedizioni.py e lo strumento in
main.py). Shopify e' finto: la tabella ha la forma di quella vera del
24/09/2026, con le tariffe B2B che al retail non devono uscire mai."""
import re
import unittest
from unittest import mock

import spedizioni
from spedizioni import costo_spedizione, CacheTabella, tariffa_retail_della_zona


def _tariffa(nome, prezzo, *condizioni):
    return {"nome": nome, "attiva": True, "prezzo": prezzo, "valuta": "EUR",
            "condizioni_dati": [{"campo": c, "operatore": o, "valore": v, "unita": u}
                                for c, o, v, u in condizioni]}


GRATIS = ("TOTAL_PRICE", "GREATER_THAN_OR_EQUAL_TO", "100.0", "EUR")


def _b2b(prezzo, da, a):
    return _tariffa("Spedizione B2B", prezzo, ("TOTAL_WEIGHT", "GREATER_THAN_OR_EQUAL_TO", da, "GRAMS"),
                    ("TOTAL_WEIGHT", "LESS_THAN_OR_EQUAL_TO", a, "GRAMS"))


TABELLA = {"esito": "ok", "profili": [{"profilo": "General profile", "predefinito": True, "zone": [
    {"zona": "Italia", "codici": ["IT"], "tariffe": [
        _tariffa("Spedizione gratuita", "0.0", GRATIS), _tariffa("Spedizione rapida", "5.9"),
        _b2b("9.9", "0.0", "19999.0"), _b2b("15.9", "20000.0", "30000.0")]},
    {"zona": "Malta", "codici": ["MT"], "tariffe": [
        _tariffa("Spedizione veloce", "16.9"), _tariffa("Spedizione gratuita", "0.0", GRATIS),
        _b2b("14.5", "0.0", "1999.0"), _b2b("74.5", "20000.0", "30000.0")]},
    {"zona": "Regno Unito", "codici": ["GB"], "tariffe": [
        _tariffa("Spedizione veloce", "11.9"), _tariffa("Spedizione gratuita", "0.0", GRATIS),
        _b2b("9.9", "0.0", "4999.0"), _b2b("35.9", "20000.0", "30000.0")]},
    {"zona": "Svizzera e Norvegia", "codici": ["CH", "NO"], "tariffe": [
        _tariffa("Spedizione veloce", "16.9"), _tariffa("Spedizione gratuita", "0.0", GRATIS),
        _b2b("13.5", "0.0", "1999.0")]},
    {"zona": "Unione Europea", "codici": ["AT", "BE", "BG", "HR", "CZ", "DE", "DK", "EE", "FI", "FR",
                                          "GR", "HU", "IE", "LV", "LT", "LU", "NL", "PL", "PT", "RO",
                                          "SK", "SI", "ES", "SE", "CY"], "tariffe": [
        _tariffa("Spedizione veloce", "9.9"), _tariffa("Spedizione gratuita", "0.0", GRATIS),
        _b2b("9.9", "0.0", "1999.0"), _b2b("12.5", "2000.0", "19999.0"), _b2b("25.0", "20000.0", "30000.0")]},
]}]}


FUORI_ZONA_IT = ("Spediamo anche lì, ma per questo paese il costo lo calcoliamo caso per caso. "
                 "Scrivi a info@kanokimonos.com dicendo cosa vuoi ordinare e in che paese, "
                 "e ti mandiamo un preventivo.")
FUORI_ZONA_EN = ("We do ship there, but for this country we calculate the cost case by case. "
                 "Write to info@kanokimonos.com telling us what you'd like to order and your "
                 "country, and we'll send you a quote.")


def leggi_ok():
    return TABELLA, None


def leggi_ko():
    return None, "errore: GraphQL, connessione fallita (ConnectTimeout)"


def costo(paese=None, codice=None, lingua="it", leggi=leggi_ok):
    return costo_spedizione(paese, codice, lingua, leggi)


class Paesi(unittest.TestCase):

    def test_italia(self):
        r = costo("Italia")
        self.assertEqual((r["esito"], r["prezzo"], r["soglia_gratuita"]), ("ok", "5.9", "100.0"))
        self.assertEqual(r["testo"], "Italia: la spedizione costa 5,90 €. È gratuita per ordini da 100 € in su.")

    def test_grecia(self):
        r = costo("Grecia")
        self.assertEqual((r["esito"], r["prezzo"], r["zona"]), ("ok", "9.9", "Unione Europea"))
        self.assertEqual(r["testo"], "Grecia: la spedizione costa 9,90 €. È gratuita per ordini da 100 € in su.")

    def test_regno_unito_in_tutti_i_modi(self):
        for nome in ("Regno Unito", "UK", "U.K.", "United Kingdom", "the UK", "Inghilterra",
                     "England", "Scozia", "Gran Bretagna"):
            r = costo(nome)
            self.assertEqual((r["esito"], r["codice"], r["prezzo"]), ("ok", "GB", "11.9"), nome)

    def test_malta_ha_la_sua_tariffa(self):
        r = costo("Malta")
        self.assertEqual((r["esito"], r["prezzo"], r["zona"]), ("ok", "16.9", "Malta"))

    def test_cipro_sta_nella_ue(self):
        self.assertEqual(costo("Cipro")["prezzo"], "9.9")

    def test_nomi_in_inglese_e_testo_inglese(self):
        for nome, iso, prezzo in (("Greece", "GR", "9.9"), ("Germany", "DE", "9.9"), ("Italy", "IT", "5.9"),
                                  ("Switzerland", "CH", "16.9"), ("the Netherlands", "NL", "9.9")):
            r = costo(nome, lingua="en")
            self.assertEqual((r["esito"], r["codice"], r["prezzo"]), ("ok", iso, prezzo), nome)
        self.assertEqual(costo("Greece", lingua="en")["testo"],
                         "Shipping to Greece costs €9.90. It's free for orders of €100 or more.")
        self.assertEqual(costo("UK", lingua="en")["testo"],
                         "Shipping to the United Kingdom costs €11.90. It's free for orders of €100 or more.")

    def test_maiuscole_accenti_e_articoli(self):
        for nome in ("GRECIA", "grecia", " la Grecia ", "in Grecia", "Grecia."):
            self.assertEqual(costo(nome)["codice"], "GR", nome)
        self.assertEqual(costo("España")["codice"], "ES")

    def test_solo_codice_iso(self):
        self.assertEqual(costo(codice="gr")["prezzo"], "9.9")
        self.assertEqual(costo("Hellas", codice="GR")["prezzo"], "9.9")

    def test_paese_inesistente(self):
        r = costo("Narnia")
        self.assertEqual(r["esito"], "paese_non_riconosciuto")
        self.assertEqual(r["testo"], spedizioni.TESTI["it"]["non_riconosciuto"])
        self.assertEqual(costo(codice="QQ")["esito"], "paese_non_riconosciuto")

    def test_paese_fuori_zona(self):
        for nome, codice in (("Brasile", None), ("Brazil", None), ("Stati Uniti", None), ("USA", None),
                             (None, "UY"), ("Giappone", None)):
            r = costo(nome, codice)
            self.assertEqual(r["esito"], "fuori_zona", nome or codice)
            self.assertEqual(r["testo"], FUORI_ZONA_IT)
            self.assertNotIn("prezzo", r)
            self.assertIsNone(re.search(r"\d", r["testo"]), r["testo"])
        self.assertEqual(costo("Brazil", lingua="en")["testo"], FUORI_ZONA_EN)

    def test_fuori_zona_non_dice_mai_non_spediamo(self):
        # Kano spedisce in tutto il mondo: fuori zona c'e' il preventivo.
        for codice in ("BR", "AU", "US", "JP", "UY", "CA", "ZA"):
            for lingua in ("it", "en"):
                r = costo(codice=codice, lingua=lingua)
                self.assertEqual(r["esito"], "fuori_zona", codice)
                testo = r["testo"].lower()
                for vietata in ("non spediamo", "don't ship", "do not ship", "not ship"):
                    self.assertNotIn(vietata, testo, (codice, lingua))
                self.assertIn("info@kanokimonos.com", r["testo"])

    def test_manca_il_paese(self):
        r = costo()
        self.assertEqual((r["esito"], r["testo"]), ("manca_paese", "In quale paese va spedito l'ordine?"))


class ShopifyNonRisponde(unittest.TestCase):

    def test_non_disponibile_senza_cifre(self):
        for lingua in ("it", "en"):
            r = costo("Grecia", lingua=lingua, leggi=leggi_ko)
            self.assertEqual(r["esito"], "non_disponibile")
            self.assertEqual(r["testo"], spedizioni.TESTI[lingua]["non_disponibile"])
            self.assertIsNone(re.search(r"\d", r["testo"]), r["testo"])

    def test_lettura_che_solleva(self):
        def esplode():
            raise TimeoutError("timeout")
        cache = CacheTabella(esplode)
        self.assertEqual(costo("Italia", leggi=cache)["esito"], "non_disponibile")


class NessunaTariffaB2B(unittest.TestCase):

    PREZZI_B2B = {"14.5", "74.5", "35.9", "13.5", "12.5", "25.0", "15.9"}

    def test_mai_un_prezzo_b2b(self):
        for zona in TABELLA["profili"][0]["zone"]:
            for iso in zona["codici"]:
                for lingua in ("it", "en"):
                    r = costo(codice=iso, lingua=lingua)
                    self.assertEqual(r["esito"], "ok", iso)
                    self.assertNotIn(r["prezzo"], self.PREZZI_B2B, iso)
                    self.assertNotIn("B2B", r["testo"])
                    for p in self.PREZZI_B2B:
                        self.assertNotIn(p.replace(".", ",") + "0 €", r["testo"])
                        self.assertNotIn("€" + p + "0", r["testo"])

    def test_zona_con_sole_tariffe_b2b_non_da_prezzi(self):
        zona = {"zona": "X", "codici": ["IT"], "tariffe": [_b2b("9.9", "0.0", "19999.0")]}
        prezzo, soglia, motivo = tariffa_retail_della_zona(zona)
        self.assertIsNone(prezzo)
        self.assertTrue(motivo)
        tabella = {"esito": "ok", "profili": [{"predefinito": True, "zone": [zona]}]}
        r = costo("Italia", leggi=lambda: (tabella, None))
        self.assertEqual(r["esito"], "non_disponibile")

    def test_due_tariffe_a_pagamento_non_si_sceglie_a_caso(self):
        # una seconda tariffa a pagamento senza condizioni rende il dato non
        # interpretabile: non se ne sceglie una a caso
        zona = {"zona": "X", "codici": ["IT"], "tariffe": [
            _tariffa("Spedizione rapida", "5.9"), _tariffa("Express", "19.9")]}
        self.assertIsNone(tariffa_retail_della_zona(zona)[0])


class Cache(unittest.TestCase):

    def test_un_ora_poi_rilegge_e_non_usa_il_vecchio(self):
        adesso = [1000.0]
        letture = []
        esiti = [(TABELLA, None), (None, "errore: Shopify giu'")]

        def leggi():
            letture.append(adesso[0])
            return esiti[min(len(letture) - 1, len(esiti) - 1)]

        cache = CacheTabella(leggi, durata=3600, orologio=lambda: adesso[0])
        self.assertEqual(costo("Italia", leggi=cache)["esito"], "ok")
        adesso[0] += 3599
        self.assertEqual(costo("Italia", leggi=cache)["esito"], "ok")
        self.assertEqual(len(letture), 1)
        adesso[0] += 2
        self.assertEqual(costo("Italia", leggi=cache)["esito"], "non_disponibile")
        self.assertEqual(len(letture), 2)


class StrumentoInMain(unittest.TestCase):
    """Dalla risposta GraphQL (finta) alla frase per il cliente, con le stesse
    funzioni della rotta /shopify-spedizioni."""

    def test_dal_graphql_al_testo(self):
        import main
        profili = {"shop": {"name": "N", "currencyCode": "EUR", "taxesIncluded": True, "taxShipping": False},
                   "deliveryProfiles": {"nodes": [{
                       "id": "p1", "name": "General profile", "default": True,
                       "productVariantsCount": {"count": 1},
                       "profileLocationGroups": [{"locationGroup": {"id": "g1"}}]}]}}

        def metodo(nome, prezzo, cond=()):
            return {"name": nome, "active": True, "description": None,
                    "rateProvider": {"__typename": "DeliveryRateDefinition",
                                     "price": {"amount": prezzo, "currencyCode": "EUR"}},
                    "methodConditions": [{"field": f, "operator": o,
                                          "conditionCriteria": {"__typename": t, **v}} for f, o, t, v in cond]}

        zone = {"deliveryProfile": {"profileLocationGroups": [{"locationGroupZones": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"zone": {"name": "Unione Europea", "countries": [
                {"name": "Greece", "code": {"countryCode": "GR", "restOfWorld": False}, "provinces": []}]},
                "methodDefinitions": {"nodes": [
                    metodo("Spedizione veloce", "9.9"),
                    metodo("Spedizione gratuita", "0.0", [("TOTAL_PRICE", "GREATER_THAN_OR_EQUAL_TO",
                                                           "MoneyV2", {"amount": "100.0", "currencyCode": "EUR"})]),
                    metodo("Spedizione B2B", "25.0", [("TOTAL_WEIGHT", "GREATER_THAN_OR_EQUAL_TO",
                                                       "Weight", {"value": 20000.0, "unit": "GRAMS"})]),
                ]}}]}}]}}

        def graphql(store, token, query, variabili=None):
            return (profili if "deliveryProfiles" in query else zone), None

        with mock.patch.object(main, "_shopify_sessione", return_value=("n.myshopify.com", "t", None)), \
                mock.patch.object(main, "_shopify_graphql", side_effect=graphql), \
                mock.patch.object(main, "_TABELLA_SPEDIZIONI", CacheTabella(main._tabella_per_strumento)):
            r = main.tool_costo_spedizione({"paese": "Grecia", "lingua": "it"}, "quanto costa in Grecia?", {})
            self.assertEqual(r["esito"], "ok")
            self.assertEqual(r["testo_da_riferire"],
                             "Grecia: la spedizione costa 9,90 €. È gratuita per ordini da 100 € in su.")
            self.assertNotIn("motivo", r)
            fuori = main.tool_costo_spedizione({"paese": "Brasile", "lingua": "it"}, "e in Brasile?", {})
            self.assertEqual(fuori["esito"], "fuori_zona")
            self.assertEqual(fuori["testo_da_riferire"], FUORI_ZONA_IT)

    def test_shopify_giu_nello_strumento(self):
        import main
        with mock.patch.object(main, "_shopify_sessione",
                               return_value=(None, None, {"esito": "errore: token, connessione fallita"})), \
                mock.patch.object(main, "_TABELLA_SPEDIZIONI", CacheTabella(main._tabella_per_strumento)):
            r = main.tool_costo_spedizione({"paese": "Italia", "lingua": "it"}, "spedizione Italia?", {})
            self.assertEqual(r["esito"], "non_disponibile")
            self.assertEqual(r["testo_da_riferire"], spedizioni.TESTI["it"]["non_disponibile"])

    def test_solo_retail_ha_lo_strumento(self):
        import main
        self.assertIn("costo_spedizione", main.ROLE_TOOLS["retail"])
        self.assertNotIn("costo_spedizione", main.ROLE_TOOLS["staff"])
        self.assertNotIn("costo_spedizione", main.ROLE_TOOLS["b2b"])


if __name__ == "__main__":
    unittest.main()
