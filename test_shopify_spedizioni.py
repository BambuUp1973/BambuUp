# -*- coding: utf-8 -*-
"""/shopify-spedizioni: risponde solo con la chiave admin, e con la chiave
risponde. Shopify e' finto (nessuna rete, nessuna credenziale): qui si prova
la rotta, non i dati del negozio."""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import main

CHIAVE = "chiave-di-prova"
DATI_FINTI = {
    "shop": {"name": "Negozio", "currencyCode": "EUR", "taxesIncluded": True, "taxShipping": True},
    "deliveryProfiles": {"nodes": [{
        "name": "General profile", "default": True, "productVariantsCount": {"count": 3},
        "profileLocationGroups": [{"locationGroupZones": {"nodes": [{
            "zone": {"name": "Italia", "countries": [
                {"name": "Italy", "code": {"countryCode": "IT", "restOfWorld": False}, "provinces": []}]},
            "methodDefinitions": {"nodes": [{
                "name": "Standard", "active": True, "description": None,
                "rateProvider": {"__typename": "DeliveryRateDefinition",
                                 "price": {"amount": "5.9", "currencyCode": "EUR"}},
                "methodConditions": [{"field": "TOTAL_PRICE", "operator": "LESS_THAN_OR_EQUAL_TO",
                                      "conditionCriteria": {"__typename": "MoneyV2",
                                                            "amount": "99.99", "currencyCode": "EUR"}}],
            }]},
        }]}}],
    }]},
}


class RottaSpedizioni(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app)
        patch_chiave = mock.patch.object(main, "BOT_ADMIN_KEY", CHIAVE)
        patch_sessione = mock.patch.object(main, "_shopify_sessione",
                                           return_value=("negozio.myshopify.com", "token", None))
        patch_graphql = mock.patch.object(main, "_shopify_graphql", return_value=(DATI_FINTI, None))
        for p in (patch_chiave, patch_sessione, patch_graphql):
            p.start()
            self.addCleanup(p.stop)

    def test_senza_chiave_401(self):
        self.assertEqual(self.client.get("/shopify-spedizioni").status_code, 401)

    def test_chiave_sbagliata_401(self):
        r = self.client.get("/shopify-spedizioni", headers={"x-bot-admin-key": "no"})
        self.assertEqual(r.status_code, 401)

    def test_con_chiave_risponde(self):
        r = self.client.get("/shopify-spedizioni", headers={"x-bot-admin-key": CHIAVE})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["esito"], "ok")
        zona = d["profili"][0]["zone"][0]
        self.assertEqual(zona["paesi"], ["IT Italy"])
        self.assertEqual(zona["tariffe"][0]["prezzo"], "5.9")
        self.assertEqual(zona["tariffe"][0]["condizioni"], ["TOTAL_PRICE LESS_THAN_OR_EQUAL_TO 99.99 EUR"])
        self.assertTrue(d["negozio"]["prezzi_con_tasse_incluse"])


if __name__ == "__main__":
    unittest.main()
