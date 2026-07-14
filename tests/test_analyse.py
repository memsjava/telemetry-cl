#!/usr/bin/env python3
"""
Tests de la page d'analyses pandas (package optionnel analyse/).

Lancement (depuis la racine du depot) :
    python -m unittest tests.test_analyse -v

pandas est la seule dependance optionnelle du projet : sans lui, ces tests
sont sautes (le reste de la suite couvre le comportement 501 de la route).
"""

import json
import sys
import unittest

import server
from tests.test_server import (HttpTestCase, ServerTestCase, NOW_MS,
                               log_record, logs_payload, metrics_payload)

try:
    import analyse
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


def _ingerer_jeu_de_donnees():
    """Deux utilisateurs / DP / comptes, deux modeles, erreurs et cache."""
    server.ingest_logs(logs_payload("pc-a", [
        log_record("api_request", NOW_MS, model="claude-sonnet-5",
                   input_tokens=1000, output_tokens=200, cache_read_tokens=3000,
                   cost_usd=0.50),
        log_record("api_request", NOW_MS + 1000, model="claude-sonnet-5",
                   input_tokens=500, output_tokens=100, cost_usd=0.25),
        log_record("user_prompt", NOW_MS + 2000, prompt="corrige le bug",
                   prompt_length=14),
        log_record("user_prompt", NOW_MS + 3000, prompt="ajoute des tests",
                   prompt_length=16),
    ], user="alice", dp="jean", compte="GroupeAI1"), ip="10.0.0.1")
    server.ingest_logs(logs_payload("pc-b", [
        log_record("api_request", NOW_MS, model="claude-opus-4-8",
                   input_tokens=800, output_tokens=400, cost_usd=1.50),
        log_record("api_error", NOW_MS + 500, model="claude-opus-4-8"),
        log_record("user_prompt", NOW_MS + 1000, prompt="deploie", prompt_length=7),
    ], user="bob", dp="marie", compte="GroupeAI2"), ip="10.0.0.2")
    server.ingest_metrics(metrics_payload(
        "pc-a", "commit.count", NOW_MS, 3, user="alice"), ip="10.0.0.1")
    server.ingest_metrics(metrics_payload(
        "pc-a", "lines_of_code.count", NOW_MS, 120, user="alice", type="added"),
        ip="10.0.0.1")


@unittest.skipUnless(HAS_PANDAS, "pandas non installe")
class TestCompute(ServerTestCase):
    def setUp(self):
        super().setUp()
        _ingerer_jeu_de_donnees()
        self.data = server_compute()

    def test_structure_complete(self):
        attendues = {"disponible", "pandas_version", "periode_jours",
                     "evenements_analyses", "par_utilisateur", "par_dp",
                     "par_compte", "par_modele", "par_heure",
                     "par_jour_semaine", "tendance", "cache_par_machine",
                     "erreurs_par_machine", "longueur_prompts"}
        self.assertTrue(self.data["disponible"])
        self.assertLessEqual(attendues, set(self.data))
        self.assertEqual(self.data["evenements_analyses"], 7)

    def test_le_resultat_est_serialisable_en_json(self):
        # Les types numpy ne passent pas dans json.dumps : compute() doit
        # renvoyer des types Python natifs.
        json.dumps(self.data)

    def test_par_utilisateur_agrege_couts_et_prompts(self):
        alice = next(u for u in self.data["par_utilisateur"] if u["user"] == "alice")
        self.assertEqual(alice["cout_usd"], 0.75)
        self.assertEqual(alice["prompts"], 2)
        self.assertEqual(alice["requetes_api"], 2)
        self.assertEqual(alice["tokens"], 1000 + 200 + 3000 + 500 + 100)
        self.assertAlmostEqual(alice["cout_moyen_par_prompt"], 0.375)

    def test_par_utilisateur_inclut_la_productivite_des_metrics(self):
        alice = next(u for u in self.data["par_utilisateur"] if u["user"] == "alice")
        self.assertEqual(alice["commits"], 3)
        self.assertEqual(alice["lignes_ajoutees"], 120)

    def test_tri_par_cout_decroissant(self):
        couts = [u["cout_usd"] for u in self.data["par_utilisateur"]]
        self.assertEqual(couts, sorted(couts, reverse=True))
        self.assertEqual(self.data["par_utilisateur"][0]["user"], "bob")  # 1.50

    def test_par_dp_et_par_compte(self):
        jean = next(d for d in self.data["par_dp"] if d["dp"] == "jean")
        self.assertEqual(jean["cout_usd"], 0.75)
        g2 = next(c for c in self.data["par_compte"] if c["compte"] == "GroupeAI2")
        self.assertEqual(g2["cout_usd"], 1.50)

    def test_par_modele_part_du_cout(self):
        modeles = {m["modele"]: m for m in self.data["par_modele"]}
        self.assertAlmostEqual(modeles["claude-opus-4-8"]["part_cout_pct"], 66.7)
        self.assertAlmostEqual(modeles["claude-sonnet-5"]["part_cout_pct"], 33.3)
        self.assertAlmostEqual(
            sum(m["part_cout_pct"] for m in self.data["par_modele"]), 100.0)

    def test_cache_par_machine(self):
        cache = {c["machine"]: c for c in self.data["cache_par_machine"]}
        # pc-a : 3000 caches / (3000 + 1500 entree) = 66.7 %
        self.assertAlmostEqual(cache["pc-a"]["taux_cache_pct"], 66.7)
        self.assertEqual(cache["pc-b"]["taux_cache_pct"], 0.0)

    def test_erreurs_par_machine(self):
        err = {e["machine"]: e for e in self.data["erreurs_par_machine"]}
        self.assertEqual(err["pc-b"]["erreurs_api"], 1)
        self.assertEqual(err["pc-b"]["taux_erreur_pct"], 50.0)
        self.assertEqual(err["pc-a"]["taux_erreur_pct"], 0.0)

    def test_longueur_prompts(self):
        lp = self.data["longueur_prompts"]
        self.assertEqual(lp["nombre"], 3)
        self.assertEqual(lp["mediane"], 14.0)
        self.assertEqual(lp["max"], 16)

    def test_par_heure_couvre_les_24_heures(self):
        self.assertEqual(len(self.data["par_heure"]), 24)
        self.assertAlmostEqual(
            sum(h["cout_usd"] for h in self.data["par_heure"]), 2.25)

    def test_par_jour_semaine_en_francais(self):
        jours = [j["jour"] for j in self.data["par_jour_semaine"]]
        self.assertEqual(jours, list(analyse.JOURS_FR))

    def test_days_filter_exclut_les_vieux_evenements(self):
        # NOW_MS est loin dans le passe : une fenetre d'un jour ne voit rien.
        with server._db_lock:
            vide = analyse.compute(server._conn, days=1)
        self.assertEqual(vide["evenements_analyses"], 0)
        self.assertEqual(vide["par_utilisateur"], [])


@unittest.skipUnless(HAS_PANDAS, "pandas non installe")
class TestComputeBaseVide(ServerTestCase):
    def test_toutes_les_sections_sont_vides_mais_presentes(self):
        data = server_compute()
        self.assertTrue(data["disponible"])
        self.assertEqual(data["evenements_analyses"], 0)
        for cle in ("par_utilisateur", "par_dp", "par_compte", "par_modele",
                    "par_heure", "par_jour_semaine", "cache_par_machine",
                    "erreurs_par_machine"):
            self.assertEqual(data[cle], [], cle)
        self.assertEqual(data["longueur_prompts"]["nombre"], 0)
        self.assertIsNone(data["tendance"]["variation_pct"])
        json.dumps(data)


def server_compute(days=0):
    with server._db_lock:
        return analyse.compute(server._conn, days=days)


class TestRouteAnalyse(HttpTestCase):
    """La route /api/analyse repond avec ou sans pandas."""

    @unittest.skipUnless(HAS_PANDAS, "pandas non installe")
    def test_endpoint_renvoie_les_analyses(self):
        _ingerer_jeu_de_donnees()
        status, body = self.get("/api/analyse")
        self.assertEqual(status, 200)
        self.assertTrue(body["disponible"])
        self.assertEqual(len(body["par_utilisateur"]), 2)

    @unittest.skipUnless(HAS_PANDAS, "pandas non installe")
    def test_page_analyse_est_servie(self):
        status, _, body = self.request("/analyse")
        self.assertEqual(status, 200)
        self.assertIn(b"Analyse", body)

    def test_501_explicatif_quand_pandas_est_absent(self):
        # sys.modules["analyse"] = None fait echouer `import analyse` : c'est
        # le seul moyen de simuler un serveur sans pandas quand il est installe.
        sauvegarde = sys.modules.pop("analyse", None)
        sys.modules["analyse"] = None
        try:
            status, _, body = self.request("/api/analyse")
        finally:
            del sys.modules["analyse"]
            if sauvegarde is not None:
                sys.modules["analyse"] = sauvegarde
        self.assertEqual(status, 501)
        reponse = json.loads(body)
        self.assertFalse(reponse["disponible"])
        self.assertIn("pip install pandas", reponse["erreur"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
