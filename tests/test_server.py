#!/usr/bin/env python3
"""
Tests du Moniteur Claude Code.

Lancement (depuis la racine du depot) :
    python -m unittest tests.test_server -v
    python -m unittest tests.test_server.TestPrompts -v          (une classe)
    python -m unittest tests.test_server.TestPrompts.test_search  (un seul test)

Aucune dependance externe (unittest standard).
"""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


def ts_ns(ms: int) -> str:
    """OTLP transporte les timestamps en nanosecondes, encodes en chaine."""
    return str(ms * 1_000_000)


def attr(key: str, value):
    """Construit un attribut OTLP typé {key, value:{...Value}}."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def logs_payload(machine, records, user=None, dp=None, compte=None):
    resource_attrs = [attr("machine", machine)]
    if user is not None:
        resource_attrs.append(attr("user", user))
    if dp is not None:
        resource_attrs.append(attr("dp", dp))
    if compte is not None:
        resource_attrs.append(attr("compte", compte))
    return {
        "resourceLogs": [{
            "resource": {"attributes": resource_attrs},
            "scopeLogs": [{"logRecords": records}],
        }]
    }


def log_record(event_name, ms, **attrs):
    return {
        "timeUnixNano": ts_ns(ms),
        "attributes": [attr("event.name", f"claude_code.{event_name}")]
                      + [attr(k, v) for k, v in attrs.items()],
    }


def metrics_payload(machine, name, ms, value, user=None, dp=None, compte=None, **attrs):
    resource_attrs = [attr("machine", machine)]
    if user is not None:
        resource_attrs.append(attr("user", user))
    if dp is not None:
        resource_attrs.append(attr("dp", dp))
    if compte is not None:
        resource_attrs.append(attr("compte", compte))
    return {
        "resourceMetrics": [{
            "resource": {"attributes": resource_attrs},
            "scopeMetrics": [{"metrics": [{
                "name": f"claude_code.{name}",
                "sum": {"dataPoints": [{
                    "timeUnixNano": ts_ns(ms),
                    "asInt": str(int(value)),
                    "attributes": [attr(k, v) for k, v in attrs.items()],
                }]},
            }]}],
        }]
    }


NOW_MS = 1_760_000_000_000  # horodatage fixe (2025) — les tests utilisent days=0


class ServerTestCase(unittest.TestCase):
    """Base : une base SQLite temporaire et isolee par test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = server.DB_PATH
        server.DB_PATH = str(Path(self._tmp.name) / "test.db")
        server.init_db()

    def tearDown(self):
        if server._conn is not None:
            server._conn.close()
            server._conn = None
        server.DB_PATH = self._orig_db_path
        self._tmp.cleanup()


class TestAttributes(unittest.TestCase):
    def test_attr_value_extracts_each_otlp_type(self):
        self.assertEqual(server._attr_value({"stringValue": "abc"}), "abc")
        self.assertEqual(server._attr_value({"intValue": "42"}), 42)
        self.assertEqual(server._attr_value({"doubleValue": 1.5}), 1.5)
        self.assertIs(server._attr_value({"boolValue": True}), True)

    def test_attr_value_survives_garbage(self):
        self.assertEqual(server._attr_value({"intValue": "pas-un-nombre"}), 0)
        self.assertEqual(server._attr_value({"doubleValue": "nan!"}), 0.0)
        self.assertIsNone(server._attr_value({"unknownValue": 1}))

    def test_attrs_to_dict_flattens_and_skips_keyless(self):
        got = server.attrs_to_dict([
            attr("a", "x"), attr("n", 3), {"value": {"stringValue": "orphan"}},
        ])
        self.assertEqual(got, {"a": "x", "n": 3})

    def test_attrs_to_dict_handles_none(self):
        self.assertEqual(server.attrs_to_dict(None), {})


class TestResolveMachine(unittest.TestCase):
    def test_point_attrs_win_over_resource_attrs(self):
        got = server.resolve_machine({"machine": "resource"}, {"machine": "point"})
        self.assertEqual(got, "point")

    def test_falls_back_through_key_priority(self):
        self.assertEqual(server.resolve_machine({"host.name": "h1"}, {}), "h1")
        self.assertEqual(
            server.resolve_machine({"service.instance.id": "i1"}, {}), "i1")

    def test_machine_key_beats_host_name(self):
        got = server.resolve_machine({"machine": "pc-1", "host.name": "h"}, {})
        self.assertEqual(got, "pc-1")

    def test_email_terminal_fallback(self):
        got = server.resolve_machine({"user.email": "a@b.c", "terminal.type": "vscode"}, {})
        self.assertEqual(got, "a@b.c / vscode")

    def test_unknown_when_nothing_identifies_the_machine(self):
        self.assertEqual(server.resolve_machine({}, {}), "inconnu")


class TestResolveUser(unittest.TestCase):
    def test_reads_the_user_resource_attribute(self):
        self.assertEqual(server.resolve_user({"user": "eric"}, {}), "eric")

    def test_point_attrs_win_over_resource_attrs(self):
        got = server.resolve_user({"user": "resource"}, {"user": "point"})
        self.assertEqual(got, "point")

    def test_empty_string_when_absent(self):
        # Contrairement a resolve_machine, pas de repli : Claude Code n'expose
        # pas nativement le nom d'utilisateur OS (voir docstring de resolve_user).
        self.assertEqual(server.resolve_user({}, {}), "")

    def test_ignores_unrelated_attributes(self):
        self.assertEqual(server.resolve_user({"user.email": "a@b.c"}, {}), "")


class TestResolveDpCompte(unittest.TestCase):
    """dp/compte suivent le meme contrat que user : injectes par le
    configurateur, aucune chaine de repli, vides si absents."""

    def test_reads_the_dp_resource_attribute(self):
        self.assertEqual(server.resolve_dp({"dp": "jean"}, {}), "jean")

    def test_reads_the_compte_resource_attribute(self):
        self.assertEqual(server.resolve_compte({"compte": "GroupeAI1"}, {}), "GroupeAI1")

    def test_point_attrs_win_over_resource_attrs(self):
        self.assertEqual(server.resolve_dp({"dp": "resource"}, {"dp": "point"}), "point")
        self.assertEqual(
            server.resolve_compte({"compte": "resource"}, {"compte": "point"}), "point")

    def test_empty_string_when_absent(self):
        self.assertEqual(server.resolve_dp({}, {}), "")
        self.assertEqual(server.resolve_compte({}, {}), "")

    def test_keys_do_not_leak_into_each_other(self):
        attrs = {"user": "eric", "dp": "jean", "compte": "GroupeAI1"}
        self.assertEqual(server.resolve_user(attrs, {}), "eric")
        self.assertEqual(server.resolve_dp(attrs, {}), "jean")
        self.assertEqual(server.resolve_compte(attrs, {}), "GroupeAI1")


class TestIngestLogs(ServerTestCase):
    def test_captures_prompt_text_and_client_ip(self):
        payload = logs_payload("pc-bureau", [
            log_record("user_prompt", NOW_MS,
                       prompt="refactor le module auth",
                       prompt_length=23,
                       **{"session.id": "s-1"}),
        ])
        n, n_prompts = server.ingest_logs(payload, ip="192.168.1.42")
        self.assertEqual((n, n_prompts), (1, 1))

        row = server._conn.execute(
            "SELECT machine, ip, name, prompt, prompt_length, session_id FROM events"
        ).fetchone()
        self.assertEqual(row, ("pc-bureau", "192.168.1.42", "user_prompt",
                               "refactor le module auth", 23.0, "s-1"))

    def test_captures_the_os_user_resource_attribute(self):
        payload = logs_payload("pc-bureau", [
            log_record("user_prompt", NOW_MS, prompt="salut"),
        ], user="eric")
        server.ingest_logs(payload, ip="192.168.1.42")
        (user,) = server._conn.execute("SELECT user FROM events").fetchone()
        self.assertEqual(user, "eric")

    def test_user_defaults_to_empty_string_when_not_configured(self):
        payload = logs_payload("pc-bureau", [log_record("user_prompt", NOW_MS, prompt="x")])
        server.ingest_logs(payload, ip="10.0.0.1")
        (user,) = server._conn.execute("SELECT user FROM events").fetchone()
        self.assertEqual(user, "")

    def test_captures_the_dp_and_compte_resource_attributes(self):
        payload = logs_payload("pc-bureau", [
            log_record("user_prompt", NOW_MS, prompt="salut"),
        ], user="eric", dp="jean", compte="GroupeAI1")
        server.ingest_logs(payload, ip="192.168.1.42")
        row = server._conn.execute("SELECT user, dp, compte FROM events").fetchone()
        self.assertEqual(row, ("eric", "jean", "GroupeAI1"))

    def test_dp_and_compte_default_to_empty_strings_when_not_configured(self):
        payload = logs_payload("pc-bureau", [log_record("user_prompt", NOW_MS, prompt="x")])
        server.ingest_logs(payload, ip="10.0.0.1")
        row = server._conn.execute("SELECT dp, compte FROM events").fetchone()
        self.assertEqual(row, ("", ""))

    def test_api_request_tokens_and_cost(self):
        payload = logs_payload("pc-1", [
            log_record("api_request", NOW_MS, model="claude-sonnet-5",
                       input_tokens=100, output_tokens=50,
                       cache_read_tokens=10, cache_creation_tokens=5,
                       cost_usd=0.25),
        ])
        server.ingest_logs(payload, ip="10.0.0.1")
        row = server._conn.execute(
            "SELECT model, input_tokens, output_tokens, cache_read, "
            "cache_creation, cost_usd FROM events"
        ).fetchone()
        self.assertEqual(row, ("claude-sonnet-5", 100.0, 50.0, 10.0, 5.0, 0.25))

    def test_event_without_prompt_stores_empty_string_not_null(self):
        server.ingest_logs(logs_payload("pc-1", [
            log_record("api_request", NOW_MS, model="m"),
        ]), ip="10.0.0.1")
        (prompt,) = server._conn.execute("SELECT prompt FROM events").fetchone()
        self.assertEqual(prompt, "")

    def test_prompt_counter_only_counts_records_with_text(self):
        payload = logs_payload("pc-1", [
            log_record("user_prompt", NOW_MS, prompt="un"),
            log_record("user_prompt", NOW_MS + 1),  # prompts non logges
            log_record("api_request", NOW_MS + 2, model="m"),
        ])
        n, n_prompts = server.ingest_logs(payload, ip="10.0.0.1")
        self.assertEqual((n, n_prompts), (3, 1))

    def test_empty_payload_is_a_noop(self):
        self.assertEqual(server.ingest_logs({}, ip="10.0.0.1"), (0, 0))


class TestIngestMetrics(ServerTestCase):
    def test_stores_value_type_and_ip(self):
        payload = metrics_payload("pc-1", "lines_of_code.count", NOW_MS, 120,
                                  type="added")
        n = server.ingest_metrics(payload, ip="192.168.1.7")
        self.assertEqual(n, 1)
        row = server._conn.execute(
            "SELECT machine, ip, name, type, value FROM metrics").fetchone()
        self.assertEqual(row, ("pc-1", "192.168.1.7", "lines_of_code.count",
                               "added", 120.0))

    def test_captures_the_dp_and_compte_resource_attributes(self):
        payload = metrics_payload("pc-1", "commit.count", NOW_MS, 3,
                                  user="eric", dp="jean", compte="GroupeAI1")
        server.ingest_metrics(payload, ip="10.0.0.1")
        row = server._conn.execute("SELECT user, dp, compte FROM metrics").fetchone()
        self.assertEqual(row, ("eric", "jean", "GroupeAI1"))

    def test_captures_the_os_user_resource_attribute(self):
        payload = metrics_payload("pc-1", "commit.count", NOW_MS, 3, user="eric")
        server.ingest_metrics(payload, ip="192.168.1.7")
        (user,) = server._conn.execute("SELECT user FROM metrics").fetchone()
        self.assertEqual(user, "eric")

    def test_gauge_datapoints_are_ingested_like_sums(self):
        payload = {"resourceMetrics": [{
            "resource": {"attributes": [attr("machine", "pc-1")]},
            "scopeMetrics": [{"metrics": [{
                "name": "claude_code.active_time.total",
                "gauge": {"dataPoints": [
                    {"timeUnixNano": ts_ns(NOW_MS), "asDouble": 42.5},
                ]},
            }]}],
        }]}
        self.assertEqual(server.ingest_metrics(payload, ip="10.0.0.1"), 1)
        (value,) = server._conn.execute("SELECT value FROM metrics").fetchone()
        self.assertEqual(value, 42.5)


class TestStats(ServerTestCase):
    def setUp(self):
        super().setUp()
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", NOW_MS, model="claude-sonnet-5",
                       input_tokens=1000, output_tokens=200, cost_usd=0.50),
            log_record("user_prompt", NOW_MS + 1, prompt="salut"),
            log_record("tool_decision", NOW_MS + 2, tool_name="Bash",
                       decision="accept"),
            log_record("tool_decision", NOW_MS + 3, tool_name="Edit",
                       decision="reject"),
        ], user="alice", dp="jean", compte="GroupeAI1"), ip="192.168.1.10")
        server.ingest_logs(logs_payload("pc-b", [
            log_record("api_request", NOW_MS, model="claude-opus-4-8",
                       input_tokens=500, output_tokens=100, cost_usd=1.50),
            log_record("api_error", NOW_MS + 1, model="claude-opus-4-8"),
        ], user="bob"), ip="192.168.1.11")
        server.ingest_metrics(metrics_payload(
            "pc-a", "lines_of_code.count", NOW_MS, 80, type="added"), ip="192.168.1.10")
        server.ingest_metrics(metrics_payload(
            "pc-a", "commit.count", NOW_MS, 3), ip="192.168.1.10")

    def test_machines_are_separated_and_sorted_by_cost(self):
        stats = server.get_stats()
        names = [m["machine"] for m in stats["machines"]]
        self.assertEqual(names, ["pc-b", "pc-a"])  # pc-b coute plus cher

    def test_each_machine_reports_its_ip(self):
        by_name = {m["machine"]: m for m in server.get_stats()["machines"]}
        self.assertEqual(by_name["pc-a"]["ip"], "192.168.1.10")
        self.assertEqual(by_name["pc-b"]["ip"], "192.168.1.11")

    def test_multiple_ips_per_machine_are_all_listed(self):
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", NOW_MS + 100, model="m"),
        ]), ip="10.8.0.3")  # meme machine, nouveau reseau (VPN)
        pc_a = next(m for m in server.get_stats()["machines"] if m["machine"] == "pc-a")
        self.assertCountEqual(pc_a["ips"], ["192.168.1.10", "10.8.0.3"])
        self.assertEqual(pc_a["ip"], "10.8.0.3")  # la plus recente

    def test_each_machine_reports_its_os_user(self):
        by_name = {m["machine"]: m for m in server.get_stats()["machines"]}
        self.assertEqual(by_name["pc-a"]["user"], "alice")
        self.assertEqual(by_name["pc-b"]["user"], "bob")

    def test_multiple_users_per_machine_are_all_listed(self):
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", NOW_MS + 100, model="m"),
        ], user="charlie"), ip="192.168.1.10")  # meme PC, autre session
        pc_a = next(m for m in server.get_stats()["machines"] if m["machine"] == "pc-a")
        self.assertCountEqual(pc_a["users"], ["alice", "charlie"])
        self.assertEqual(pc_a["user"], "charlie")  # le plus recent

    def test_each_machine_reports_its_dp_and_compte(self):
        by_name = {m["machine"]: m for m in server.get_stats()["machines"]}
        self.assertEqual(by_name["pc-a"]["dp"], "jean")
        self.assertEqual(by_name["pc-a"]["compte"], "GroupeAI1")
        # pc-b n'a pas declare de dp/compte a l'installation
        self.assertEqual(by_name["pc-b"]["dp"], "")
        self.assertEqual(by_name["pc-b"]["dps"], [])
        self.assertEqual(by_name["pc-b"]["compte"], "")
        self.assertEqual(by_name["pc-b"]["comptes"], [])

    def test_multiple_comptes_per_machine_are_all_listed(self):
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", NOW_MS + 100, model="m"),
        ], compte="GroupeAI2"), ip="192.168.1.10")  # meme PC, compte reattribue
        pc_a = next(m for m in server.get_stats()["machines"] if m["machine"] == "pc-a")
        self.assertCountEqual(pc_a["comptes"], ["GroupeAI1", "GroupeAI2"])
        self.assertEqual(pc_a["compte"], "GroupeAI2")  # le plus recent

    def test_user_is_empty_when_never_configured(self):
        server.ingest_logs(logs_payload("pc-c", [
            log_record("api_request", NOW_MS, model="m"),
        ]), ip="10.0.0.5")  # OTEL_RESOURCE_ATTRIBUTES sans "user="
        pc_c = next(m for m in server.get_stats()["machines"] if m["machine"] == "pc-c")
        self.assertEqual(pc_c["user"], "")
        self.assertEqual(pc_c["users"], [])

    def test_per_machine_counters(self):
        by_name = {m["machine"]: m for m in server.get_stats()["machines"]}
        pc_a = by_name["pc-a"]
        self.assertEqual(pc_a["cost_usd"], 0.50)
        self.assertEqual(pc_a["tokens"]["input"], 1000)
        self.assertEqual(pc_a["tokens"]["output"], 200)
        self.assertEqual(pc_a["tokens"]["total"], 1200)
        self.assertEqual(pc_a["prompts"], 1)
        self.assertEqual(pc_a["prompts_logged"], 1)
        self.assertEqual(pc_a["tools"], {"accepted": 1, "rejected": 1})
        self.assertEqual(pc_a["lines_added"], 80)
        self.assertEqual(pc_a["commits"], 3)
        self.assertEqual(by_name["pc-b"]["api_errors"], 1)

    def test_totals_sum_every_machine(self):
        totals = server.get_stats()["totals"]
        self.assertEqual(totals["cost_usd"], 2.00)          # 0.50 + 1.50
        self.assertEqual(totals["tokens"]["input"], 1500)   # 1000 + 500
        self.assertEqual(totals["api_requests"], 2)
        self.assertEqual(totals["api_errors"], 1)
        self.assertEqual(totals["commits"], 3)

    def test_totals_expose_no_machine_specific_fields(self):
        totals = server.get_stats()["totals"]
        for key in ("by_model", "ip", "ips", "user", "users",
                    "dp", "dps", "compte", "comptes", "first_ms", "last_ms",
                    "installation_ms", "desinstallation_ms"):
            self.assertNotIn(key, totals)

    def test_by_model_breakdown_is_per_machine(self):
        by_name = {m["machine"]: m for m in server.get_stats()["machines"]}
        models_a = {bm["model"]: bm for bm in by_name["pc-a"]["by_model"]}
        self.assertEqual(list(models_a), ["claude-sonnet-5"])
        self.assertEqual(models_a["claude-sonnet-5"]["cost_usd"], 0.50)

    def test_stats_no_longer_embed_the_activity_feed(self):
        # L'activite est servie paginee par /api/activite : le poll de 30 s
        # du tableau de bord ne doit plus transporter tout le flux.
        self.assertNotIn("recent", server.get_stats())

    def test_days_filter_excludes_old_rows(self):
        # NOW_MS est loin dans le passe : une fenetre de 1 jour ne doit rien voir.
        stats = server.get_stats(days=1)
        self.assertEqual(stats["machines"], [])
        self.assertEqual(stats["totals"]["cost_usd"], 0)

    def test_sessions_fall_back_to_distinct_session_ids(self):
        server.ingest_logs(logs_payload("pc-c", [
            log_record("api_request", NOW_MS, **{"session.id": "s-1"}),
            log_record("api_request", NOW_MS + 1, **{"session.id": "s-2"}),
            log_record("api_request", NOW_MS + 2, **{"session.id": "s-1"}),
        ]), ip="10.0.0.9")
        pc_c = next(m for m in server.get_stats()["machines"] if m["machine"] == "pc-c")
        self.assertEqual(pc_c["sessions"], 2)


class TestInstallation(ServerTestCase):
    """Pings d'installation / desinstallation des installeurs (npx et python)."""

    PING = {"action": "installation", "machine": "pc-a", "utilisateur": "eric",
            "dp": "jean", "compte": "GroupeAI1"}

    def test_creates_a_dated_event_with_the_full_identity(self):
        avant = int(time.time() * 1000)
        action = server.ingest_installation(dict(self.PING), ip="10.0.0.1")
        apres = int(time.time() * 1000)
        self.assertEqual(action, "installation")
        row = server._conn.execute(
            "SELECT ts_ms, machine, ip, user, dp, compte, name FROM events"
        ).fetchone()
        self.assertEqual(row[1:], ("pc-a", "10.0.0.1", "eric", "jean",
                                   "GroupeAI1", "installation"))
        # l'horodatage vient de l'horloge du serveur, pas du client
        self.assertTrue(avant <= row[0] <= apres)

    def test_rejects_an_unknown_action(self):
        with self.assertRaises(ValueError):
            server.ingest_installation({"action": "reboot", "machine": "pc"}, "")

    def test_rejects_a_missing_machine(self):
        with self.assertRaises(ValueError):
            server.ingest_installation({"action": "installation"}, "")
        with self.assertRaises(ValueError):
            server.ingest_installation(
                {"action": "installation", "machine": "  "}, "")

    def test_stats_expose_the_installation_date(self):
        server.ingest_installation(dict(self.PING), ip="10.0.0.1")
        pc = next(m for m in server.get_stats()["machines"]
                  if m["machine"] == "pc-a")
        self.assertIsNotNone(pc["installation_ms"])
        self.assertIsNone(pc["desinstallation_ms"])

    def test_stats_expose_the_desinstallation_date(self):
        server.ingest_installation(dict(self.PING), ip="10.0.0.1")
        server.ingest_installation(
            {"action": "desinstallation", "machine": "pc-a"}, ip="10.0.0.1")
        pc = next(m for m in server.get_stats()["machines"]
                  if m["machine"] == "pc-a")
        self.assertIsNotNone(pc["desinstallation_ms"])
        self.assertGreaterEqual(pc["desinstallation_ms"], pc["installation_ms"])

    def test_machine_appears_on_the_dashboard_as_soon_as_installed(self):
        # Aucune telemetrie encore : le ping suffit a faire exister la machine.
        server.ingest_installation(dict(self.PING), ip="10.0.0.1")
        stats = server.get_stats()
        pc = next(m for m in stats["machines"] if m["machine"] == "pc-a")
        self.assertEqual(pc["cost_usd"], 0.0)
        self.assertEqual(pc["user"], "eric")

    def test_date_is_never_none_d_by_the_period_filter(self):
        # La date d'installation est une propriete de la machine : elle reste
        # visible meme quand le ping est plus vieux que la periode affichee.
        server.ingest_installation(dict(self.PING), ip="10.0.0.1")
        with server._db_lock:
            server._conn.execute("UPDATE events SET ts_ms=1 WHERE name='installation'")
            # une activite recente maintient la machine dans la fenetre
            server._conn.commit()
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", int(time.time() * 1000),
                       model="m"),
        ]), ip="10.0.0.1")
        pc = next(m for m in server.get_stats(days=7)["machines"]
                  if m["machine"] == "pc-a")
        self.assertEqual(pc["installation_ms"], 1)


class TestActivite(ServerTestCase):
    """Flux d'activite pagine (GET /api/activite)."""

    def setUp(self):
        super().setUp()
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", NOW_MS + i, model="m") for i in range(5)
        ], user="alice", dp="jean", compte="GroupeAI1"), ip="192.168.1.10")
        server.ingest_logs(logs_payload("pc-b", [
            log_record("tool_decision", NOW_MS + 100, tool_name="Bash",
                       decision="accept"),
        ], user="bob", dp="marie", compte="GroupeAI2"), ip="192.168.1.11")

    def test_carries_machine_ip_and_user(self):
        got = server.get_activite()
        self.assertEqual(got["count"], 6)
        self.assertTrue(all(r["ip"] for r in got["activite"]))
        self.assertEqual({r["machine"] for r in got["activite"]}, {"pc-a", "pc-b"})
        self.assertEqual(got["activite"][0]["user"], "bob")  # le plus recent en tete

    def test_pagination_walks_the_whole_feed_without_overlap(self):
        page1 = server.get_activite(limit=4, offset=0)
        page2 = server.get_activite(limit=4, offset=4)
        self.assertEqual(page1["count"], 6)  # total, pas la taille de page
        self.assertEqual(len(page1["activite"]), 4)
        self.assertEqual(len(page2["activite"]), 2)
        times = [r["time"] for r in page1["activite"] + page2["activite"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_filter_by_machine(self):
        got = server.get_activite(machine="pc-b")
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["activite"][0]["tool"], "Bash")

    def test_filter_by_user(self):
        got = server.get_activite(user="alice")
        self.assertEqual(got["count"], 5)
        self.assertTrue(all(r["user"] == "alice" for r in got["activite"]))

    def test_filter_by_dp_and_by_compte(self):
        self.assertEqual(server.get_activite(dp="jean")["count"], 5)
        self.assertEqual(server.get_activite(compte="GroupeAI2")["count"], 1)
        # les filtres se combinent en ET
        self.assertEqual(server.get_activite(dp="jean", compte="GroupeAI2")["count"], 0)

    def test_rows_carry_dp_and_compte(self):
        row = server.get_activite(machine="pc-b")["activite"][0]
        self.assertEqual((row["dp"], row["compte"]), ("marie", "GroupeAI2"))

    def test_offset_beyond_the_end_gives_an_empty_page(self):
        got = server.get_activite(limit=20, offset=40)
        self.assertEqual(got["count"], 6)
        self.assertEqual(got["activite"], [])

    def test_days_filter_excludes_old_rows(self):
        got = server.get_activite(days=1)  # NOW_MS est loin dans le passe
        self.assertEqual((got["count"], got["activite"]), (0, []))


class TestPrompts(ServerTestCase):
    def setUp(self):
        super().setUp()
        server.ingest_logs(logs_payload("pc-a", [
            log_record("user_prompt", NOW_MS, prompt="corrige le bug de login",
                       prompt_length=23, **{"session.id": "s-1"}),
            log_record("user_prompt", NOW_MS + 1000, prompt="ajoute des tests",
                       prompt_length=16, **{"session.id": "s-1"}),
        ], user="alice", dp="jean", compte="GroupeAI1"), ip="192.168.1.10")
        server.ingest_logs(logs_payload("pc-b", [
            log_record("user_prompt", NOW_MS + 2000, prompt="deploie en prod",
                       prompt_length=15, **{"session.id": "s-2"}),
            log_record("api_request", NOW_MS + 3000, model="m"),  # sans prompt
        ], user="bob", dp="marie", compte="GroupeAI2"), ip="192.168.1.11")

    def test_returns_only_records_that_carry_prompt_text(self):
        res = server.get_prompts()
        self.assertEqual(res["count"], 3)
        self.assertTrue(all(p["text"] for p in res["prompts"]))

    def test_newest_first(self):
        texts = [p["text"] for p in server.get_prompts()["prompts"]]
        self.assertEqual(texts, ["deploie en prod", "ajoute des tests",
                                 "corrige le bug de login"])

    def test_prompt_carries_machine_ip_user_and_session(self):
        p = server.get_prompts(machine="pc-b")["prompts"][0]
        self.assertEqual(p["machine"], "pc-b")
        self.assertEqual(p["ip"], "192.168.1.11")
        self.assertEqual(p["user"], "bob")
        self.assertEqual(p["session_id"], "s-2")
        self.assertEqual(p["length"], 15)

    def test_filter_by_machine(self):
        res = server.get_prompts(machine="pc-a")
        self.assertEqual(res["count"], 2)
        self.assertEqual({p["machine"] for p in res["prompts"]}, {"pc-a"})

    def test_filter_by_user(self):
        res = server.get_prompts(user="alice")
        self.assertEqual(res["count"], 2)
        self.assertEqual({p["user"] for p in res["prompts"]}, {"alice"})

    def test_filter_by_user_and_machine_combine(self):
        self.assertEqual(server.get_prompts(user="alice", machine="pc-b")["count"], 0)
        self.assertEqual(server.get_prompts(user="bob", machine="pc-b")["count"], 1)

    def test_filter_by_dp_and_by_compte(self):
        self.assertEqual(server.get_prompts(dp="jean")["count"], 2)
        self.assertEqual(server.get_prompts(compte="GroupeAI2")["count"], 1)
        self.assertEqual(server.get_prompts(dp="jean", query="prod")["count"], 0)

    def test_prompt_rows_carry_dp_and_compte(self):
        p = server.get_prompts(machine="pc-a")["prompts"][0]
        self.assertEqual((p["dp"], p["compte"]), ("jean", "GroupeAI1"))

    def test_search(self):
        res = server.get_prompts(query="bug")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["prompts"][0]["text"], "corrige le bug de login")

    def test_search_is_substring_not_prefix(self):
        self.assertEqual(server.get_prompts(query="prod")["count"], 1)

    def test_search_and_machine_filters_combine(self):
        self.assertEqual(server.get_prompts(machine="pc-a", query="prod")["count"], 0)
        self.assertEqual(server.get_prompts(machine="pc-b", query="prod")["count"], 1)

    def test_search_with_no_match(self):
        self.assertEqual(server.get_prompts(query="introuvable")["count"], 0)

    def test_limit_is_honoured_and_clamped(self):
        res = server.get_prompts(limit=2)
        self.assertEqual(len(res["prompts"]), 2)
        self.assertEqual(res["count"], 3)  # count = total, pas la taille de page
        self.assertEqual(server.get_prompts(limit=0)["limit"], 1)      # plancher
        self.assertEqual(server.get_prompts(limit=99999)["limit"], 1000)  # plafond

    def test_pagination_walks_all_prompts_without_overlap(self):
        page1 = server.get_prompts(limit=2, offset=0)
        page2 = server.get_prompts(limit=2, offset=2)
        self.assertEqual((page1["count"], page2["count"]), (3, 3))
        texts = [p["text"] for p in page1["prompts"] + page2["prompts"]]
        self.assertEqual(texts, ["deploie en prod", "ajoute des tests",
                                 "corrige le bug de login"])

    def test_pagination_and_search_combine(self):
        # 2 prompts de pc-a : page 2 de taille 1 = le plus ancien des deux.
        res = server.get_prompts(machine="pc-a", limit=1, offset=1)
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["prompts"][0]["text"], "corrige le bug de login")

    def test_negative_offset_is_clamped_to_zero(self):
        res = server.get_prompts(limit=1, offset=-5)
        self.assertEqual(res["offset"], 0)
        self.assertEqual(res["prompts"][0]["text"], "deploie en prod")

    def test_sql_wildcards_in_query_are_not_injected(self):
        # '%' est un joker SQL : passe en valeur, il ne doit rien matcher ici.
        self.assertEqual(server.get_prompts(query="%")["count"], 0)


class TestMigration(unittest.TestCase):
    """Une base creee par une version anterieure doit etre migree sans perte."""

    LEGACY_SCHEMA = """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER, day TEXT, machine TEXT, name TEXT, session_id TEXT,
            model TEXT, input_tokens REAL DEFAULT 0, output_tokens REAL DEFAULT 0,
            cache_read REAL DEFAULT 0, cache_creation REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0, tool_name TEXT, decision TEXT, success TEXT
        );
        CREATE TABLE metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER, day TEXT, machine TEXT, name TEXT, type TEXT,
            model TEXT, tool_name TEXT, decision TEXT, value REAL DEFAULT 0
        );
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "legacy.db")
        con = sqlite3.connect(self.db)
        con.executescript(self.LEGACY_SCHEMA)
        con.execute(
            "INSERT INTO events (ts_ms,day,machine,name,cost_usd) VALUES (?,?,?,?,?)",
            (NOW_MS, "2025-10-09", "pc-legacy", "api_request", 0.75),
        )
        con.execute(
            "INSERT INTO metrics (ts_ms,day,machine,name,value) VALUES (?,?,?,?,?)",
            (NOW_MS, "2025-10-09", "pc-legacy", "commit.count", 2),
        )
        con.commit()
        con.close()
        self._orig_db_path = server.DB_PATH
        server.DB_PATH = self.db

    def tearDown(self):
        if server._conn is not None:
            server._conn.close()
            server._conn = None
        server.DB_PATH = self._orig_db_path
        self._tmp.cleanup()

    def test_adds_columns_without_losing_rows(self):
        server.init_db()
        events_cols = {r[1] for r in server._conn.execute("PRAGMA table_info(events)")}
        metrics_cols = {r[1] for r in server._conn.execute("PRAGMA table_info(metrics)")}
        self.assertLessEqual({"ip", "user", "dp", "compte", "prompt", "prompt_length"},
                             events_cols)
        self.assertLessEqual({"ip", "user", "dp", "compte"}, metrics_cols)

        (n_events,) = server._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        (n_metrics,) = server._conn.execute("SELECT COUNT(*) FROM metrics").fetchone()
        self.assertEqual((n_events, n_metrics), (1, 1))

    def test_legacy_rows_still_aggregate_after_migration(self):
        server.init_db()
        stats = server.get_stats()
        pc = next(m for m in stats["machines"] if m["machine"] == "pc-legacy")
        self.assertEqual(pc["cost_usd"], 0.75)
        self.assertEqual(pc["commits"], 2)
        self.assertEqual(pc["ip"], "")  # aucune IP connue pour l'historique
        self.assertEqual(pc["user"], "")  # aucun utilisateur connu pour l'historique
        self.assertEqual(pc["dp"], "")
        self.assertEqual(pc["compte"], "")

    def test_migration_is_idempotent(self):
        server.init_db()
        server._conn.close()
        server._conn = None
        server.init_db()  # ne doit pas lever "duplicate column name"
        (n,) = server._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        self.assertEqual(n, 1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Ne suit pas les 302 : les tests d'auth inspectent la redirection elle-meme."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTestCase(ServerTestCase):
    """Base bout en bout : un vrai serveur HTTP sur un port ephemere."""

    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self._opener = urllib.request.build_opener(_NoRedirect)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(self, path, payload, ctype="application/json"):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": ctype}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, json.loads(r.read())

    def request(self, path, data=None, headers=None, method=None):
        """Requete brute sans suivi de redirection : (statut, en-tetes, corps)."""
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers or {}, method=method)
        try:
            with self._opener.open(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()


class TestHttpEndpoints(HttpTestCase):

    def test_health(self):
        status, body = self.get("/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

    def test_dashboard_is_served_from_its_web_subfolder(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            status, ctype, body = r.status, r.headers.get("Content-Type"), r.read()
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Moniteur Claude Code", body)

    def test_logs_ingestion_records_the_caller_ip(self):
        status, _ = self.post("/v1/logs", logs_payload("pc-x", [
            log_record("user_prompt", NOW_MS, prompt="bonjour"),
        ]))
        self.assertEqual(status, 200)

        _, body = self.get("/api/prompts")
        self.assertEqual(body["count"], 1)
        p = body["prompts"][0]
        self.assertEqual(p["text"], "bonjour")
        self.assertEqual(p["machine"], "pc-x")
        self.assertEqual(p["ip"], "127.0.0.1")  # capturee depuis la connexion TCP

    def test_metrics_ingestion_then_stats(self):
        self.post("/v1/metrics", metrics_payload(
            "pc-y", "commit.count", NOW_MS, 5))
        _, body = self.get("/api/stats")
        pc = next(m for m in body["machines"] if m["machine"] == "pc-y")
        self.assertEqual(pc["commits"], 5)
        self.assertEqual(pc["ip"], "127.0.0.1")

    def test_os_user_flows_end_to_end_into_stats_and_prompts(self):
        self.post("/v1/logs", logs_payload("pc-w", [
            log_record("user_prompt", NOW_MS, prompt="deploie"),
        ], user="eric", dp="jean", compte="GroupeAI1"))
        self.post("/v1/metrics", metrics_payload(
            "pc-w", "commit.count", NOW_MS, 1, user="eric", dp="jean", compte="GroupeAI1"))

        _, stats = self.get("/api/stats")
        pc = next(m for m in stats["machines"] if m["machine"] == "pc-w")
        self.assertEqual(pc["user"], "eric")
        self.assertEqual(pc["dp"], "jean")
        self.assertEqual(pc["compte"], "GroupeAI1")

        _, prompts = self.get("/api/prompts")
        self.assertEqual(prompts["prompts"][0]["user"], "eric")

    def test_x_forwarded_for_takes_precedence_over_socket_ip(self):
        req = urllib.request.Request(
            self.base + "/v1/logs",
            data=json.dumps(logs_payload("pc-z", [
                log_record("user_prompt", NOW_MS, prompt="via proxy"),
            ])).encode(),
            headers={"Content-Type": "application/json",
                     "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)

        _, body = self.get("/api/prompts")
        self.assertEqual(body["prompts"][0]["ip"], "203.0.113.9")

    def test_prompts_query_params_are_applied(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("user_prompt", NOW_MS, prompt="alpha"),
        ]))
        self.post("/v1/logs", logs_payload("pc-b", [
            log_record("user_prompt", NOW_MS + 1, prompt="beta"),
        ]))
        _, body = self.get("/api/prompts?machine=pc-b")
        self.assertEqual([p["text"] for p in body["prompts"]], ["beta"])

        _, body = self.get("/api/prompts?q=alph")
        self.assertEqual([p["text"] for p in body["prompts"]], ["alpha"])

    def test_traces_are_accepted_and_discarded(self):
        status, _ = self.post("/v1/traces", {"resourceSpans": []})
        self.assertEqual(status, 200)
        _, body = self.get("/api/stats")
        self.assertEqual(body["machines"], [])

    def test_non_json_content_type_is_rejected_with_415(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/logs", {}, ctype="application/x-protobuf")
        self.assertEqual(ctx.exception.code, 415)

    def test_invalid_json_is_rejected_with_400(self):
        req = urllib.request.Request(
            self.base + "/v1/logs", data=b"{pas du json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_post_route_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/nope", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_bad_days_param_falls_back_to_zero(self):
        status, body = self.get("/api/stats?days=abc")
        self.assertEqual(status, 200)
        self.assertEqual(body["days"], 0)

    def test_activite_endpoint_paginates(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("api_request", NOW_MS + i, model="m") for i in range(3)
        ]))
        _, body = self.get("/api/activite?limit=2&offset=2")
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["activite"]), 1)

    def test_prompts_endpoint_paginates(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("user_prompt", NOW_MS + i, prompt=f"p{i}") for i in range(3)
        ]))
        _, body = self.get("/api/prompts?limit=2&offset=2")
        self.assertEqual(body["count"], 3)
        self.assertEqual([p["text"] for p in body["prompts"]], ["p0"])

    def test_dp_and_compte_query_params_filter_the_apis(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("user_prompt", NOW_MS, prompt="prompt de jean"),
        ], user="alice", dp="jean", compte="GroupeAI1"))
        self.post("/v1/logs", logs_payload("pc-b", [
            log_record("user_prompt", NOW_MS, prompt="prompt de marie"),
        ], user="bob", dp="marie", compte="GroupeAI2"))

        _, activite = self.get("/api/activite?dp=jean")
        self.assertEqual(activite["count"], 1)
        self.assertEqual(activite["activite"][0]["user"], "alice")

        _, prompts = self.get("/api/prompts?compte=GroupeAI2")
        self.assertEqual(prompts["count"], 1)
        self.assertEqual(prompts["prompts"][0]["text"], "prompt de marie")

        _, _, body = self.request("/api/export?dp=marie")
        text = body.decode("utf-8")
        self.assertIn("bob", text)
        self.assertNotIn("alice", text)

    def test_installation_ping_flows_into_stats(self):
        status, _ = self.post("/v1/installation", {
            "action": "installation", "machine": "pc-npx",
            "utilisateur": "eric", "dp": "jean", "compte": "GroupeAI1"})
        self.assertEqual(status, 200)
        _, stats = self.get("/api/stats")
        pc = next(m for m in stats["machines"] if m["machine"] == "pc-npx")
        self.assertIsNotNone(pc["installation_ms"])
        self.assertEqual(pc["ip"], "127.0.0.1")  # IP lue sur la connexion

    def test_installation_ping_with_a_bad_action_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/installation", {"action": "reboot", "machine": "pc"})
        self.assertEqual(ctx.exception.code, 400)

    def test_export_csv_downloads_as_attachment(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("api_request", NOW_MS, model="m", input_tokens=10,
                       output_tokens=5, cost_usd=0.5),
        ], user="eric"))
        status, headers, body = self.request("/api/export?user=eric&format=csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("moniteur_eric", headers["Content-Disposition"])
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))  # BOM : accents corrects dans Excel
        self.assertIn(";".join(server.EXPORT_HEADERS), text)
        self.assertIn("eric", text)

    def test_export_filters_by_user(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("api_request", NOW_MS, model="m"),
        ], user="eric"))
        self.post("/v1/logs", logs_payload("pc-b", [
            log_record("api_request", NOW_MS, model="m"),
        ], user="bob"))
        _, _, body = self.request("/api/export?user=bob")
        text = body.decode("utf-8")
        self.assertIn("bob", text)
        self.assertNotIn("eric", text)

    def test_export_xls_is_spreadsheetml(self):
        self.post("/v1/logs", logs_payload("pc-a", [
            log_record("api_request", NOW_MS, model="m"),
        ], user="eric"))
        status, headers, body = self.request("/api/export?user=eric&format=xls")
        self.assertEqual(status, 200)
        self.assertIn("application/vnd.ms-excel", headers["Content-Type"])
        self.assertIn(".xls", headers["Content-Disposition"])
        self.assertIn(b"<Worksheet", body)

    def test_export_rejects_an_unknown_format(self):
        status, _, _ = self.request("/api/export?format=pdf")
        self.assertEqual(status, 400)


class TestExportFormats(ServerTestCase):
    """Serialisation CSV / SpreadsheetML des lignes d'export."""

    ROWS = [("2025-10-09 10:00:00", "pc-a", "10.0.0.1", "eric", "jean", "GroupeAI1",
             "api_request", "claude-sonnet-5", "s-1", 100, 50, 10, 5, 0.25,
             "Bash", "accept", 'ligne1\navec "guillemets"; et point-virgule')]

    def test_csv_quotes_separators_newlines_and_doubles_quotes(self):
        text = server.export_csv(self.ROWS)
        self.assertIn('"ligne1\navec ""guillemets""; et point-virgule"', text)
        self.assertTrue(text.startswith("\ufeff" + ";".join(server.EXPORT_HEADERS)))

    def test_csv_has_one_line_per_row_plus_header(self):
        # Le seul \r\n qui compte : le retour a la ligne DANS une cellule est
        # un \n nu, donc il ne cree pas de ligne CSV supplementaire.
        text = server.export_csv(self.ROWS)
        self.assertEqual(text.count("\r\n"), 2)  # en-tete + 1 ligne

    def test_xls_escapes_xml_and_types_numbers(self):
        rows = [row[:16] + ("<script>&\"fin\"",) for row in self.ROWS]
        xml = server.export_xls(rows)
        self.assertIn("&lt;script&gt;&amp;&quot;fin&quot;", xml)
        self.assertIn('<Data ss:Type="Number">100</Data>', xml)
        self.assertIn('<Data ss:Type="Number">0.25</Data>', xml)
        self.assertNotIn("<script>", xml)

    def test_get_export_rows_orders_newest_first(self):
        server.ingest_logs(logs_payload("pc-a", [
            log_record("user_prompt", NOW_MS, prompt="ancien"),
            log_record("user_prompt", NOW_MS + 1000, prompt="recent"),
        ], user="eric"), ip="10.0.0.1")
        rows = server.get_export_rows(user="eric")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][-1], "recent")
        self.assertEqual(rows[0][3], "eric")

    def test_get_export_rows_filters_by_dp_and_compte(self):
        server.ingest_logs(logs_payload("pc-a", [
            log_record("api_request", NOW_MS, model="m"),
        ], user="eric", dp="jean", compte="GroupeAI1"), ip="10.0.0.1")
        server.ingest_logs(logs_payload("pc-b", [
            log_record("api_request", NOW_MS, model="m"),
        ], user="bob", dp="marie", compte="GroupeAI2"), ip="10.0.0.2")
        self.assertEqual(len(server.get_export_rows(dp="jean")), 1)
        self.assertEqual(server.get_export_rows(compte="GroupeAI2")[0][3], "bob")
        self.assertEqual(server.get_export_rows(dp="jean", compte="GroupeAI2"), [])


class TestSessions(unittest.TestCase):
    """Cycle de vie des sessions d'authentification."""

    def tearDown(self):
        with server._sessions_lock:
            server._sessions.clear()

    def test_created_session_is_valid_then_destroyed(self):
        token = server.create_session()
        self.assertTrue(server.session_valid(token))
        server.destroy_session(token)
        self.assertFalse(server.session_valid(token))

    def test_unknown_or_empty_token_is_invalid(self):
        self.assertFalse(server.session_valid("inconnu"))
        self.assertFalse(server.session_valid(""))
        self.assertFalse(server.session_valid(None))

    def test_expired_session_is_rejected_and_purged(self):
        token = server.create_session()
        with server._sessions_lock:
            server._sessions[token] = 0  # expiree depuis 1970
        self.assertFalse(server.session_valid(token))
        with server._sessions_lock:
            self.assertNotIn(token, server._sessions)

    def test_check_password_without_configured_password_always_passes(self):
        self.assertTrue(server.check_password("n-importe-quoi"))
        self.assertTrue(server.check_password(None))


class TestAuth(HttpTestCase):
    """Mot de passe configure : dashboard et API proteges, ingestion ouverte."""

    def setUp(self):
        super().setUp()
        server.AUTH_PASSWORD = "secret"

    def tearDown(self):
        server.AUTH_PASSWORD = None
        with server._sessions_lock:
            server._sessions.clear()
        super().tearDown()

    def _login(self, mot_de_passe="secret"):
        status, headers, _ = self.request(
            "/login", data=f"mot_de_passe={mot_de_passe}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        cookie = (headers.get("Set-Cookie") or "").split(";")[0]
        return status, headers, cookie

    def test_api_without_session_is_401(self):
        for path in ("/api/stats", "/api/prompts", "/api/activite", "/api/export"):
            status, _, _ = self.request(path)
            self.assertEqual(status, 401, path)

    def test_dashboard_without_session_redirects_to_login(self):
        status, headers, _ = self.request("/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login")

    def test_login_page_is_served_without_session(self):
        status, _, body = self.request("/login")
        self.assertEqual(status, 200)
        self.assertIn(b"mot_de_passe", body)

    def test_wrong_password_redirects_back_with_error(self):
        status, headers, cookie = self._login("mauvais")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login?erreur=1")
        self.assertEqual(cookie, "")  # aucun cookie pose

    def test_good_password_opens_a_session_that_unlocks_the_api(self):
        status, headers, cookie = self._login()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        self.assertTrue(cookie.startswith(server.SESSION_COOKIE + "="))
        self.assertIn("HttpOnly", headers["Set-Cookie"])

        status, _, body = self.request("/api/stats", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("totals", json.loads(body))

    def test_logout_invalidates_the_session(self):
        _, _, cookie = self._login()
        status, headers, _ = self.request("/logout", headers={"Cookie": cookie})
        self.assertEqual(status, 302)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])  # cookie efface
        status, _, _ = self.request("/api/stats", headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    def test_otlp_ingestion_stays_open_without_any_session(self):
        # Principe du projet : les machines suivies postent sans identifiant.
        status, _ = self.post("/v1/logs", logs_payload("pc-a", [
            log_record("user_prompt", NOW_MS, prompt="sans auth"),
        ]))
        self.assertEqual(status, 200)

    def test_installation_ping_stays_open_without_any_session(self):
        status, _ = self.post("/v1/installation",
                              {"action": "installation", "machine": "pc-a"})
        self.assertEqual(status, 200)

    def test_health_stays_open(self):
        status, _, _ = self.request("/health")
        self.assertEqual(status, 200)

    def test_login_page_redirects_home_once_authenticated(self):
        _, _, cookie = self._login()
        status, headers, _ = self.request("/login", headers={"Cookie": cookie})
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
