#!/usr/bin/env python3
"""
Tests du configurateur de machine (fusion dans ~/.claude/settings.json).

Lancement (depuis la racine du depot) :
    python -m unittest tests.test_configurer -v
"""

import json
import platform
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cli import configurer_machine as cm


class TestBuildEnv(unittest.TestCase):
    def test_endpoint_is_built_from_collector_and_port(self):
        env = cm.build_env("192.168.1.20", "pc-bureau", port=4318)
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://192.168.1.20:4318")

    def test_custom_port(self):
        env = cm.build_env("localhost", "pc", port=9999)
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://localhost:9999")

    def test_machine_label_lands_in_resource_attributes(self):
        env = cm.build_env("localhost", "mac-maison", os_user="eric")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], "machine=mac-maison,user=eric")

    def test_os_user_defaults_to_the_current_session_when_not_given(self):
        env = cm.build_env("localhost", "pc")
        self.assertIn(f"user={cm.detect_os_user()}", env["OTEL_RESOURCE_ATTRIBUTES"])

    def test_os_user_can_be_overridden(self):
        env = cm.build_env("localhost", "pc", os_user="alice")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], "machine=pc,user=alice")

    def test_reserved_characters_in_values_are_percent_encoded(self):
        # "=" et "," delimitent la syntaxe OTEL_RESOURCE_ATTRIBUTES : une valeur
        # inhabituelle ne doit pas casser le parsing des attributs suivants.
        env = cm.build_env("localhost", "pc", os_user="a,b=c")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], "machine=pc,user=a%2Cb%3Dc")

    def test_dp_and_compte_land_in_resource_attributes(self):
        env = cm.build_env("localhost", "pc", os_user="eric",
                           dp="jean", compte="GroupeAI1")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"],
                         "machine=pc,user=eric,dp=jean,compte=GroupeAI1")

    def test_empty_dp_and_compte_are_omitted_from_resource_attributes(self):
        # Pas de "dp=" vide : le serveur traite deja l'absence comme "".
        env = cm.build_env("localhost", "pc", os_user="eric")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], "machine=pc,user=eric")

    def test_dp_and_compte_values_are_percent_encoded(self):
        env = cm.build_env("localhost", "pc", os_user="eric",
                           dp="a,b", compte="c=d")
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"],
                         "machine=pc,user=eric,dp=a%2Cb,compte=c%3Dd")

    def test_protocol_is_json_because_server_cannot_read_protobuf(self):
        self.assertEqual(cm.build_env("h", "m")["OTEL_EXPORTER_OTLP_PROTOCOL"], "http/json")

    def test_prompts_logged_by_default(self):
        self.assertEqual(cm.build_env("h", "m")["OTEL_LOG_USER_PROMPTS"], "1")

    def test_prompts_can_be_disabled(self):
        env = cm.build_env("h", "m", log_prompts=False)
        self.assertEqual(env["OTEL_LOG_USER_PROMPTS"], "0")

    def test_assistant_responses_are_never_logged(self):
        for flag in (True, False):
            env = cm.build_env("h", "m", log_prompts=flag)
            self.assertEqual(env["OTEL_LOG_ASSISTANT_RESPONSES"], "0")

    def test_every_managed_key_is_produced(self):
        self.assertEqual(set(cm.build_env("h", "m")), set(cm.MANAGED_KEYS))


class TestEncodeResourceValue(unittest.TestCase):
    def test_plain_value_is_untouched(self):
        self.assertEqual(cm._encode_resource_value("eric"), "eric")

    def test_comma_and_equals_are_percent_encoded(self):
        self.assertEqual(cm._encode_resource_value("a,b"), "a%2Cb")
        self.assertEqual(cm._encode_resource_value("a=b"), "a%3Db")

    def test_percent_sign_is_encoded_first_to_stay_reversible(self):
        # Sinon un "%2C" litteral dans la valeur d'origine serait indiscernable
        # d'une virgule encodee par cette fonction.
        self.assertEqual(cm._encode_resource_value("100%"), "100%25")


class TestParseResourceAttrs(unittest.TestCase):
    def test_round_trip_with_build_env(self):
        env = cm.build_env("h", "pc", os_user="a,b=c", dp="100%", compte="GroupeAI1")
        got = cm.parse_resource_attrs(env["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertEqual(got, {"machine": "pc", "user": "a,b=c",
                               "dp": "100%", "compte": "GroupeAI1"})

    def test_empty_string_gives_empty_dict(self):
        self.assertEqual(cm.parse_resource_attrs(""), {})

    def test_parts_without_equals_are_ignored(self):
        self.assertEqual(cm.parse_resource_attrs("machine=pc,garbage,user=eric"),
                         {"machine": "pc", "user": "eric"})


class TestDemander(unittest.TestCase):
    def _with_input(self, typed, label="Question", defaut=""):
        from unittest.mock import patch
        with patch("builtins.input", return_value=typed):
            return cm.demander(label, defaut)

    def test_typed_answer_wins(self):
        self.assertEqual(self._with_input("jean", defaut="marc"), "jean")

    def test_empty_answer_keeps_the_default(self):
        self.assertEqual(self._with_input("", defaut="marc"), "marc")

    def test_whitespace_only_answer_keeps_the_default(self):
        self.assertEqual(self._with_input("   ", defaut="marc"), "marc")

    def test_eof_keeps_the_default(self):
        # stdin ferme en plein prompt (Ctrl+D) : on retombe sur le defaut.
        from unittest.mock import patch
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(cm.demander("Question", "marc"), "marc")


class TestResoudreIdentite(unittest.TestCase):
    """flags CLI > question interactive > valeurs deja configurees."""

    @staticmethod
    def _poser_fixe(reponses):
        """Simule l'operateur : repond `reponses[label]` a chaque question."""
        questions = []

        def poser(label, defaut=""):
            questions.append(label)
            return reponses.get(label, defaut)
        return poser, questions

    def test_flags_short_circuit_every_question(self):
        poser, questions = self._poser_fixe({})
        got = cm.resoudre_identite("eric", "jean", "GroupeAI1", {},
                                   interactif=True, poser=poser)
        self.assertEqual(got, ("eric", "jean", "GroupeAI1"))
        self.assertEqual(questions, [])  # rien n'a ete demande

    def test_interactive_mode_asks_only_the_missing_values(self):
        poser, questions = self._poser_fixe({
            "Directeur de projet (dp)": "jean",
            "Compte Claude (ex. GroupeAI1)": "GroupeAI1",
        })
        got = cm.resoudre_identite("eric", None, None, {},
                                   interactif=True, poser=poser)
        self.assertEqual(got, ("eric", "jean", "GroupeAI1"))
        self.assertEqual(len(questions), 2)  # utilisateur donne en flag : pas demande

    def test_interactive_defaults_come_from_the_existing_configuration(self):
        # Entree sur chaque question : les valeurs deja configurees sont gardees.
        defauts = []

        def poser(label, defaut=""):
            defauts.append(defaut)
            return defaut
        existants = {"user": "alice", "dp": "jean", "compte": "GroupeAI1"}
        got = cm.resoudre_identite(None, None, None, existants,
                                   interactif=True, poser=poser)
        self.assertEqual(got, ("alice", "jean", "GroupeAI1"))
        self.assertEqual(defauts, ["alice", "jean", "GroupeAI1"])

    def test_non_interactive_keeps_existing_dp_and_compte(self):
        existants = {"dp": "jean", "compte": "GroupeAI1"}
        utilisateur, dp, compte = cm.resoudre_identite(
            None, None, None, existants, interactif=False)
        self.assertEqual(utilisateur, cm.detect_os_user())
        self.assertEqual((dp, compte), ("jean", "GroupeAI1"))

    def test_non_interactive_first_install_yields_empty_dp_and_compte(self):
        utilisateur, dp, compte = cm.resoudre_identite(
            None, None, None, {}, interactif=False)
        self.assertEqual(utilisateur, cm.detect_os_user())
        self.assertEqual((dp, compte), ("", ""))


class TestDetectOsUser(unittest.TestCase):
    def test_returns_a_non_empty_string(self):
        # getpass.getuser() depend de l'environnement d'execution : on verifie
        # juste le contrat (chaine non vide), pas une valeur precise.
        self.assertTrue(cm.detect_os_user())


class TestMergeEnv(unittest.TestCase):
    def test_creates_env_when_settings_are_empty(self):
        got = cm.merge_env({}, {"A": "1"})
        self.assertEqual(got, {"env": {"A": "1"}})

    def test_preserves_unrelated_top_level_settings(self):
        existing = {
            "permissions": {"allow": ["Bash(git status)"]},
            "model": "claude-sonnet-5",
        }
        got = cm.merge_env(existing, {"A": "1"})
        self.assertEqual(got["permissions"], {"allow": ["Bash(git status)"]})
        self.assertEqual(got["model"], "claude-sonnet-5")
        self.assertEqual(got["env"], {"A": "1"})

    def test_preserves_unrelated_env_vars(self):
        existing = {"env": {"MON_VAR": "garde-moi"}}
        got = cm.merge_env(existing, {"A": "1"})
        self.assertEqual(got["env"], {"MON_VAR": "garde-moi", "A": "1"})

    def test_reconfiguring_overwrites_previous_values(self):
        existing = {"env": {"OTEL_RESOURCE_ATTRIBUTES": "machine=ancien"}}
        got = cm.merge_env(existing, {"OTEL_RESOURCE_ATTRIBUTES": "machine=nouveau"})
        self.assertEqual(got["env"]["OTEL_RESOURCE_ATTRIBUTES"], "machine=nouveau")

    def test_does_not_mutate_the_input(self):
        existing = {"env": {"A": "1"}}
        cm.merge_env(existing, {"B": "2"})
        self.assertEqual(existing, {"env": {"A": "1"}})

    def test_rejects_non_object_env(self):
        with self.assertRaises(ValueError):
            cm.merge_env({"env": "pas-un-objet"}, {"A": "1"})


class TestRemoveEnv(unittest.TestCase):
    def test_removes_only_managed_keys(self):
        settings = {"env": {"OTEL_LOGS_EXPORTER": "otlp", "MON_VAR": "garde-moi"}}
        got = cm.remove_env(settings)
        self.assertEqual(got["env"], {"MON_VAR": "garde-moi"})

    def test_drops_env_entirely_when_nothing_else_remains(self):
        settings = {"model": "x", "env": cm.build_env("h", "m")}
        got = cm.remove_env(settings)
        self.assertNotIn("env", got)
        self.assertEqual(got["model"], "x")

    def test_is_a_noop_when_never_configured(self):
        self.assertEqual(cm.remove_env({"model": "x"}), {"model": "x"})

    def test_round_trip_restores_the_original_settings(self):
        original = {"permissions": {"allow": []}, "env": {"MON_VAR": "v"}}
        configured = cm.merge_env(original, cm.build_env("h", "m"))
        self.assertEqual(cm.remove_env(configured), original)


class TestSettingsFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".claude" / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(data, encoding="utf-8")

    def test_load_missing_file_gives_empty_settings(self):
        self.assertEqual(cm.load_settings(self.path), {})

    def test_load_empty_file_gives_empty_settings(self):
        self.write("")
        self.assertEqual(cm.load_settings(self.path), {})

    def test_load_rejects_a_json_array(self):
        self.write("[1, 2]")
        with self.assertRaises(ValueError):
            cm.load_settings(self.path)

    def test_load_raises_on_invalid_json(self):
        self.write("{pas du json")
        with self.assertRaises(json.JSONDecodeError):
            cm.load_settings(self.path)

    def test_write_creates_parent_directory(self):
        self.assertFalse(self.path.parent.exists())
        cm.write_settings(self.path, {"env": {"A": "1"}})
        self.assertEqual(json.loads(self.path.read_text()), {"env": {"A": "1"}})

    def test_write_backs_up_the_existing_file(self):
        self.write('{"model": "ancien"}')
        backup = cm.write_settings(self.path, {"model": "nouveau"})
        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(backup.read_text()), {"model": "ancien"})
        self.assertEqual(json.loads(self.path.read_text()), {"model": "nouveau"})

    def test_write_reports_no_backup_for_a_fresh_install(self):
        self.assertIsNone(cm.write_settings(self.path, {"env": {}}))

    def test_full_cycle_preserves_a_real_settings_file(self):
        self.write(json.dumps({
            "permissions": {"allow": ["Bash(python server.py)"]},
            "model": "claude-sonnet-5",
            "env": {"MON_VAR": "garde-moi"},
        }))
        before = cm.load_settings(self.path)

        cm.write_settings(self.path, cm.merge_env(
            before, cm.build_env("192.168.1.20", "pc-bureau", os_user="eric")))
        after = cm.load_settings(self.path)

        self.assertEqual(after["permissions"], {"allow": ["Bash(python server.py)"]})
        self.assertEqual(after["model"], "claude-sonnet-5")
        self.assertEqual(after["env"]["MON_VAR"], "garde-moi")
        self.assertEqual(after["env"]["OTEL_RESOURCE_ATTRIBUTES"],
                         "machine=pc-bureau,user=eric")

        cm.write_settings(self.path, cm.remove_env(after))
        self.assertEqual(cm.load_settings(self.path), before)


class TestSignalerInstallation(unittest.TestCase):
    """Ping de datation envoye au collecteur a l'install / au retrait."""

    def setUp(self):
        self.recu = {}
        recu = self.recu

        class Collecteur(BaseHTTPRequestHandler):
            def do_POST(self):
                longueur = int(self.headers.get("Content-Length") or 0)
                recu["path"] = self.path
                recu["corps"] = json.loads(self.rfile.read(longueur))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, fmt, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Collecteur)
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def test_posts_the_full_identity_to_the_collector(self):
        ok = cm.signaler_installation(self.endpoint, "installation", "pc-a",
                                      "eric", "jean", "GroupeAI1")
        self.assertTrue(ok)
        self.assertEqual(self.recu["path"], "/v1/installation")
        self.assertEqual(self.recu["corps"], {
            "action": "installation", "machine": "pc-a",
            "utilisateur": "eric", "dp": "jean", "compte": "GroupeAI1"})

    def test_desinstallation_action_is_sent_verbatim(self):
        cm.signaler_installation(self.endpoint, "desinstallation", "pc-a")
        self.assertEqual(self.recu["corps"]["action"], "desinstallation")

    def test_trailing_slash_in_endpoint_is_tolerated(self):
        self.assertTrue(cm.signaler_installation(
            self.endpoint + "/", "installation", "pc-a"))
        self.assertEqual(self.recu["path"], "/v1/installation")

    def test_unreachable_collector_returns_false_without_raising(self):
        # Port 9 (discard) : connexion refusee immediatement. L'installation
        # elle-meme ne doit jamais echouer a cause du collecteur.
        self.assertFalse(cm.signaler_installation(
            "http://127.0.0.1:9", "installation", "pc-a", timeout=1.0))


class TestTemplateStaysInSync(unittest.TestCase):
    """Le template livre doit exposer exactement les cles que le script pose."""

    def test_template_keys_match_build_env(self):
        template = json.loads(
            Path("claude-settings.template.json").read_text(encoding="utf-8"))
        self.assertEqual(set(template["env"]), set(cm.build_env("h", "m")))

    def test_template_endpoint_matches_the_default_collecteur(self):
        # Le template est copie a la main : s'il derive du vrai defaut, on ne
        # le saurait pas sans ce test.
        template = json.loads(
            Path("claude-settings.template.json").read_text(encoding="utf-8"))
        endpoint = template["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"]
        self.assertEqual(endpoint, f"http://{cm.DEFAULT_COLLECTEUR}:{cm.DEFAULT_PORT}")


class TestCliDefaults(unittest.TestCase):
    """Invoque le vrai CLI (sous-processus) pour verifier les valeurs par defaut."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = str(Path(self._tmp.name) / "settings.json")
        self.script = Path(__file__).resolve().parent.parent / "cli" / "configurer_machine.py"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args):
        # stdin=DEVNULL : jamais de questions interactives, meme quand la suite
        # de tests est lancee depuis un vrai terminal.
        result = subprocess.run(
            [sys.executable, str(self.script), "--settings", self.settings,
             "--simuler", *args],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        )
        return json.loads(result.stdout.split("deviendrait :", 1)[1])

    def test_collecteur_defaults_to_the_configured_server_without_being_passed(self):
        updated = self._run()
        self.assertEqual(
            updated["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"],
            f"http://{cm.DEFAULT_COLLECTEUR}:{cm.DEFAULT_PORT}")

    def test_machine_defaults_to_the_current_hostname_without_being_passed(self):
        updated = self._run()
        self.assertIn(f"machine={platform.node()}",
                      updated["env"]["OTEL_RESOURCE_ATTRIBUTES"])

    def test_explicit_collecteur_overrides_the_default(self):
        updated = self._run("--collecteur", "localhost")
        self.assertEqual(
            updated["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://localhost:4318")

    def test_dp_and_compte_flags_land_in_resource_attributes(self):
        updated = self._run("--utilisateur", "eric", "--dp", "jean",
                            "--compte", "GroupeAI1")
        self.assertTrue(updated["env"]["OTEL_RESOURCE_ATTRIBUTES"]
                        .endswith(",user=eric,dp=jean,compte=GroupeAI1"))

    def test_without_flags_nor_terminal_dp_and_compte_stay_absent(self):
        updated = self._run()
        attrs = updated["env"]["OTEL_RESOURCE_ATTRIBUTES"]
        self.assertNotIn("dp=", attrs)
        self.assertNotIn("compte=", attrs)

    def test_rerun_preserves_dp_and_compte_from_the_existing_settings(self):
        # Machine deja installee avec dp/compte : les relances scriptees
        # (stdin non interactif, aucun flag) ne doivent pas les perdre.
        Path(self.settings).write_text(json.dumps({
            "env": {"OTEL_RESOURCE_ATTRIBUTES":
                    "machine=pc,user=eric,dp=jean,compte=GroupeAI1"},
        }), encoding="utf-8")
        updated = self._run("--utilisateur", "eric")
        self.assertTrue(updated["env"]["OTEL_RESOURCE_ATTRIBUTES"]
                        .endswith(",user=eric,dp=jean,compte=GroupeAI1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
