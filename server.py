#!/usr/bin/env python3
"""
Moniteur Claude Code — serveur de collecte + tableau de bord.

Recoit la telemetrie OpenTelemetry (OTLP/HTTP JSON) emise par Claude Code
sur chacune de vos machines, la stocke dans une base SQLite locale, et sert
un tableau de bord web affichant les statistiques par machine.

Suit : cout, tokens, sessions, outils, commits/PR/lignes de code, ainsi que
l'adresse IP et l'utilisateur systeme (session Windows/Mac/Linux) de chaque
machine, et — si OTEL_LOG_USER_PROMPTS=1 est active cote machine — le contenu
des prompts.

Aucune dependance externe : Python 3.8+ standard uniquement.

Lancement :
    python server.py            (ecoute sur le port 4318, dashboard sans mot de passe)
    python server.py --port 4318 --host 0.0.0.0
    python server.py --mot-de-passe secret        (dashboard + API proteges)
    MONITEUR_MOT_DE_PASSE=secret python server.py (idem, via l'environnement)

Routes :
    POST /v1/metrics   -> ingestion des metriques OTLP (jamais protegee)
    POST /v1/logs      -> ingestion des evenements OTLP (jamais protegee)
    POST /v1/traces    -> accepte et ignore (compat)
    GET  /             -> tableau de bord
    GET  /api/stats    -> donnees agregees (JSON), parametre optionnel ?days=N
    GET  /api/prompts  -> journal des prompts (?days=N&machine=X&q=recherche
                          &limit=N&offset=N — pagine)
    GET  /api/activite -> activite recente paginee (?days=N&machine=X&limit=N&offset=N)
    GET  /api/export   -> export des evenements (?user=X&days=N&format=csv|xls)
    GET  /login, POST /login, GET /logout -> authentification (si mot de passe)
    GET  /health       -> etat du serveur (jamais protegee)
"""

import argparse
import gzip
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "telemetry.db")
DASHBOARD_PATH = os.path.join(BASE_DIR, "web", "dashboard.html")
LOGIN_PATH = os.path.join(BASE_DIR, "web", "login.html")

_db_lock = threading.Lock()
_conn = None

# Mot de passe du tableau de bord (None = acces libre, comportement historique).
# L'ingestion OTLP (/v1/*) n'est JAMAIS protegee : les machines suivies postent
# sans identifiant, c'est un principe de ce projet.
AUTH_PASSWORD = None
SESSION_TTL = 7 * 86400
SESSION_COOKIE = "moniteur_session"
_sessions = {}  # token -> horodatage d'expiration
_sessions_lock = threading.Lock()


# --------------------------------------------------------------------------
# Base de donnees
# --------------------------------------------------------------------------
def init_db():
    global _conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER,
            day TEXT,
            machine TEXT,
            ip TEXT DEFAULT '',
            user TEXT DEFAULT '',
            dp TEXT DEFAULT '',
            compte TEXT DEFAULT '',
            name TEXT,
            session_id TEXT,
            model TEXT,
            input_tokens REAL DEFAULT 0,
            output_tokens REAL DEFAULT 0,
            cache_read REAL DEFAULT 0,
            cache_creation REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            tool_name TEXT,
            decision TEXT,
            success TEXT,
            prompt TEXT DEFAULT '',
            prompt_length REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER,
            day TEXT,
            machine TEXT,
            ip TEXT DEFAULT '',
            user TEXT DEFAULT '',
            dp TEXT DEFAULT '',
            compte TEXT DEFAULT '',
            name TEXT,
            type TEXT,
            model TEXT,
            tool_name TEXT,
            decision TEXT,
            value REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);
        CREATE INDEX IF NOT EXISTS idx_events_machine ON events(machine);
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts_ms);
        CREATE INDEX IF NOT EXISTS idx_metrics_machine ON metrics(machine);
        """
    )
    _migrate_columns()
    _conn.commit()


def _migrate_columns():
    """Ajoute les colonnes absentes des bases creees par une version anterieure."""
    wanted = {
        "events": (
            ("ip", "TEXT DEFAULT ''"),
            ("user", "TEXT DEFAULT ''"),
            ("dp", "TEXT DEFAULT ''"),
            ("compte", "TEXT DEFAULT ''"),
            ("prompt", "TEXT DEFAULT ''"),
            ("prompt_length", "REAL DEFAULT 0"),
        ),
        "metrics": (
            ("ip", "TEXT DEFAULT ''"),
            ("user", "TEXT DEFAULT ''"),
            ("dp", "TEXT DEFAULT ''"),
            ("compte", "TEXT DEFAULT ''"),
        ),
    }
    for table, columns in wanted.items():
        existing = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in columns:
            if col not in existing:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                print(f"[db] colonne ajoutee : {table}.{col}")


# --------------------------------------------------------------------------
# Helpers OTLP
# --------------------------------------------------------------------------
def _attr_value(v):
    """Extrait la valeur d'un AnyValue OTLP (stringValue / intValue / ...)."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return 0
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return 0.0
    if "boolValue" in v:
        return bool(v["boolValue"])
    return None


def attrs_to_dict(attributes):
    """Convertit une liste d'attributs OTLP [{key,value}] en dict."""
    out = {}
    for a in attributes or []:
        k = a.get("key")
        if k is not None:
            out[k] = _attr_value(a.get("value"))
    return out


def resolve_machine(resource_attrs, point_attrs):
    """Determine le nom de la machine a partir des attributs disponibles."""
    for src in (point_attrs, resource_attrs):
        for key in ("machine", "host.name", "service.instance.id"):
            val = src.get(key)
            if val:
                return str(val)
    # Dernier recours : e-mail + type de terminal
    email = resource_attrs.get("user.email") or point_attrs.get("user.email")
    term = resource_attrs.get("terminal.type") or point_attrs.get("terminal.type")
    if email or term:
        return f"{email or 'compte'} / {term or '?'}"
    return "inconnu"


def _resolve_attr(key, resource_attrs, point_attrs):
    """Attribut pose par le configurateur de machine (aucune chaine de repli).

    Ces cles ("user", "dp", "compte") n'existent pas dans la telemetrie native
    de Claude Code : c'est le configurateur qui les injecte dans
    OTEL_RESOURCE_ATTRIBUTES. Absentes -> chaine vide.
    """
    for src in (point_attrs, resource_attrs):
        val = src.get(key)
        if val:
            return str(val)
    return ""


def resolve_user(resource_attrs, point_attrs):
    """Utilisateur systeme (session Windows/Mac/Linux), pose via OTEL_RESOURCE_ATTRIBUTES.

    Claude Code n'expose pas nativement le nom d'utilisateur OS (seul `user.email`,
    identique pour tous quand un compte Pro/Max est partage) : c'est le
    configurateur de machine qui ajoute cette cle "user" lui-meme.
    """
    return _resolve_attr("user", resource_attrs, point_attrs)


def resolve_dp(resource_attrs, point_attrs):
    """Directeur de projet declare a l'installation (cle "dp")."""
    return _resolve_attr("dp", resource_attrs, point_attrs)


def resolve_compte(resource_attrs, point_attrs):
    """Nom du compte Claude partage declare a l'installation (cle "compte")."""
    return _resolve_attr("compte", resource_attrs, point_attrs)


def _to_num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ns_to_ms(ns):
    try:
        return int(int(ns) // 1_000_000)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


def _day_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _point_value(dp):
    if "asInt" in dp:
        try:
            return float(int(dp["asInt"]))
        except (TypeError, ValueError):
            return 0.0
    if "asDouble" in dp:
        return _to_num(dp["asDouble"])
    return 0.0


def short(name):
    """Retire le prefixe claude_code. d'un nom de metrique/evenement."""
    return name.replace("claude_code.", "") if name else name


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def ingest_metrics(payload, ip=""):
    rows = []
    for rm in payload.get("resourceMetrics", []):
        r_attrs = attrs_to_dict(rm.get("resource", {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                name = short(m.get("name", ""))
                data = m.get("sum") or m.get("gauge") or {}
                for dp in data.get("dataPoints", []):
                    try:
                        p_attrs = attrs_to_dict(dp.get("attributes"))
                        machine = resolve_machine(r_attrs, p_attrs)
                        user = resolve_user(r_attrs, p_attrs)
                        dp_name = resolve_dp(r_attrs, p_attrs)
                        compte = resolve_compte(r_attrs, p_attrs)
                        ms = _ns_to_ms(dp.get("timeUnixNano") or dp.get("startTimeUnixNano"))
                        rows.append((
                            ms, _day_of(ms), machine, ip, user, dp_name, compte, name,
                            str(p_attrs.get("type", "")),
                            str(p_attrs.get("model", "")),
                            str(p_attrs.get("tool_name", "")),
                            str(p_attrs.get("decision", "")),
                            _point_value(dp),
                        ))
                    except Exception as e:  # noqa: BLE001
                        print(f"[warn] point metrique ignore: {e}")
    if rows:
        with _db_lock:
            _conn.executemany(
                "INSERT INTO metrics (ts_ms,day,machine,ip,user,dp,compte,name,type,model,tool_name,decision,value)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            _conn.commit()
    return len(rows)


def ingest_logs(payload, ip=""):
    rows = []
    prompts_seen = 0
    for rl in payload.get("resourceLogs", []):
        r_attrs = attrs_to_dict(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                try:
                    a = attrs_to_dict(rec.get("attributes"))
                    name = short(a.get("event.name") or _attr_value(rec.get("body")) or "")
                    machine = resolve_machine(r_attrs, a)
                    user = resolve_user(r_attrs, a)
                    dp_name = resolve_dp(r_attrs, a)
                    compte = resolve_compte(r_attrs, a)
                    ms = _ns_to_ms(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano"))
                    decision = a.get("decision")
                    prompt = a.get("prompt") or ""
                    if prompt:
                        prompts_seen += 1
                    rows.append((
                        ms, _day_of(ms), machine, ip, user, dp_name, compte, name,
                        str(a.get("session.id") or a.get("session_id") or ""),
                        str(a.get("model") or ""),
                        _to_num(a.get("input_tokens")),
                        _to_num(a.get("output_tokens")),
                        _to_num(a.get("cache_read_tokens")),
                        _to_num(a.get("cache_creation_tokens")),
                        _to_num(a.get("cost_usd")),
                        str(a.get("tool_name") or ""),
                        str(decision) if decision is not None else "",
                        str(a.get("success")) if a.get("success") is not None else "",
                        str(prompt),
                        _to_num(a.get("prompt_length")),
                    ))
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] evenement ignore: {e}")
    if rows:
        with _db_lock:
            _conn.executemany(
                "INSERT INTO events (ts_ms,day,machine,ip,user,dp,compte,name,session_id,model,"
                "input_tokens,output_tokens,cache_read,cache_creation,cost_usd,"
                "tool_name,decision,success,prompt,prompt_length)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            _conn.commit()
    return len(rows), prompts_seen


# --------------------------------------------------------------------------
# Agregation pour le tableau de bord
# --------------------------------------------------------------------------
def _norm_decision(d):
    d = (d or "").lower()
    if d.startswith("accept"):
        return "accepted"
    if d.startswith("reject"):
        return "rejected"
    return "other"


def _cutoff_ms(days):
    if days and days > 0:
        return int((time.time() - days * 86400) * 1000)
    return 0


def get_stats(days=0):
    cutoff = _cutoff_ms(days)

    machines = {}

    def m(name):
        if name not in machines:
            machines[name] = {
                "machine": name,
                "ip": "",
                "ips": [],
                "user": "",
                "users": [],
                "dp": "",
                "dps": [],
                "compte": "",
                "comptes": [],
                "cost_usd": 0.0,
                "tokens": {"input": 0, "output": 0, "cache_read": 0,
                           "cache_creation": 0, "total": 0},
                "api_requests": 0,
                "api_errors": 0,
                "prompts": 0,
                "prompts_logged": 0,
                "sessions": 0,
                "lines_added": 0,
                "lines_removed": 0,
                "commits": 0,
                "pull_requests": 0,
                "active_seconds": 0,
                "tools": {"accepted": 0, "rejected": 0},
                "by_model": {},
                "first_ms": None,
                "last_ms": None,
            }
        return machines[name]

    with _db_lock:
        cur = _conn.cursor()

        # --- Evenements : tokens, cout, requetes, prompts ---
        cur.execute(
            """
            SELECT machine,
                   SUM(CASE WHEN name='api_request' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN name='api_error' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN name='user_prompt' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN prompt<>'' THEN 1 ELSE 0 END),
                   SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_read), SUM(cache_creation), SUM(cost_usd),
                   MIN(ts_ms), MAX(ts_ms)
            FROM events WHERE ts_ms>=? GROUP BY machine
            """,
            (cutoff,),
        )
        for row in cur.fetchall():
            d = m(row[0])
            d["api_requests"] += int(row[1] or 0)
            d["api_errors"] += int(row[2] or 0)
            d["prompts"] += int(row[3] or 0)
            d["prompts_logged"] += int(row[4] or 0)
            d["tokens"]["input"] += int(row[5] or 0)
            d["tokens"]["output"] += int(row[6] or 0)
            d["tokens"]["cache_read"] += int(row[7] or 0)
            d["tokens"]["cache_creation"] += int(row[8] or 0)
            d["cost_usd"] += float(row[9] or 0)
            d["first_ms"] = row[10]
            d["last_ms"] = row[11]

        # --- IP / utilisateur / DP / compte vus par machine (le plus recent en tete) ---
        for col, list_key in (("ip", "ips"), ("user", "users"),
                              ("dp", "dps"), ("compte", "comptes")):
            cur.execute(
                f"""
                SELECT machine, {col}, MAX(ts_ms) AS last_seen
                FROM (
                    SELECT machine, {col}, ts_ms FROM events WHERE {col}<>'' AND ts_ms>=?
                    UNION ALL
                    SELECT machine, {col}, ts_ms FROM metrics WHERE {col}<>'' AND ts_ms>=?
                )
                GROUP BY machine, {col} ORDER BY last_seen DESC
                """,
                (cutoff, cutoff),
            )
            for machine, val, _last in cur.fetchall():
                d = m(machine)
                if val not in d[list_key]:
                    d[list_key].append(val)
            for d in machines.values():
                d[col] = d[list_key][0] if d[list_key] else ""

        # --- Decisions d'outils ---
        cur.execute(
            "SELECT machine, decision, COUNT(*) FROM events "
            "WHERE name='tool_decision' AND ts_ms>=? GROUP BY machine, decision",
            (cutoff,),
        )
        for machine, decision, count in cur.fetchall():
            d = m(machine)
            nd = _norm_decision(decision)
            if nd in ("accepted", "rejected"):
                d["tools"][nd] += int(count or 0)

        # --- Detail par modele ---
        cur.execute(
            """
            SELECT machine, model,
                   SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_read), SUM(cache_creation), SUM(cost_usd)
            FROM events WHERE name='api_request' AND ts_ms>=?
            GROUP BY machine, model
            """,
            (cutoff,),
        )
        for row in cur.fetchall():
            d = m(row[0])
            model = row[1] or "(inconnu)"
            d["by_model"][model] = {
                "model": model,
                "input": int(row[2] or 0),
                "output": int(row[3] or 0),
                "cache_read": int(row[4] or 0),
                "cache_creation": int(row[5] or 0),
                "cost_usd": round(float(row[6] or 0), 4),
            }

        # --- Metriques : lignes de code, commits, PR, sessions, temps actif ---
        cur.execute(
            """
            SELECT machine,
              SUM(CASE WHEN name LIKE '%lines_of_code%' AND type='added' THEN value ELSE 0 END),
              SUM(CASE WHEN name LIKE '%lines_of_code%' AND type='removed' THEN value ELSE 0 END),
              SUM(CASE WHEN name LIKE '%commit%' THEN value ELSE 0 END),
              SUM(CASE WHEN name LIKE '%pull_request%' THEN value ELSE 0 END),
              SUM(CASE WHEN name LIKE '%session.count%' THEN value ELSE 0 END),
              SUM(CASE WHEN name LIKE '%active_time%' THEN value ELSE 0 END),
              MIN(ts_ms), MAX(ts_ms)
            FROM metrics WHERE ts_ms>=? GROUP BY machine
            """,
            (cutoff,),
        )
        for row in cur.fetchall():
            d = m(row[0])
            d["lines_added"] += int(row[1] or 0)
            d["lines_removed"] += int(row[2] or 0)
            d["commits"] += int(row[3] or 0)
            d["pull_requests"] += int(row[4] or 0)
            d["sessions"] += int(row[5] or 0)
            d["active_seconds"] += int(row[6] or 0)
            if row[7] is not None:
                d["first_ms"] = row[7] if d["first_ms"] is None else min(d["first_ms"], row[7])
            if row[8] is not None:
                d["last_ms"] = row[8] if d["last_ms"] is None else max(d["last_ms"], row[8])

        # --- Fallback sessions : distinct session_id si la metrique manque ---
        cur.execute(
            "SELECT machine, COUNT(DISTINCT session_id) FROM events "
            "WHERE session_id<>'' AND ts_ms>=? GROUP BY machine",
            (cutoff,),
        )
        for machine, n in cur.fetchall():
            d = m(machine)
            if d["sessions"] == 0:
                d["sessions"] = int(n or 0)

        # --- Chronologie (par jour et par machine) ---
        cur.execute(
            """
            SELECT day, machine, SUM(cost_usd),
                   SUM(input_tokens+output_tokens+cache_read+cache_creation)
            FROM events WHERE name='api_request' AND ts_ms>=?
            GROUP BY day, machine ORDER BY day
            """,
            (cutoff,),
        )
        timeline = [
            {"day": r[0], "machine": r[1],
             "cost_usd": round(float(r[2] or 0), 4), "tokens": int(r[3] or 0)}
            for r in cur.fetchall()
        ]

    # --- Finalisation + totaux ---
    totals = m("__TOTAL__")
    for name, d in list(machines.items()):
        if name == "__TOTAL__":
            continue
        d["tokens"]["total"] = sum(v for k, v in d["tokens"].items() if k != "total")
        d["by_model"] = sorted(d["by_model"].values(), key=lambda x: -x["cost_usd"])
        d["cost_usd"] = round(d["cost_usd"], 4)
        # cumul dans les totaux
        totals["cost_usd"] += d["cost_usd"]
        for k in ("api_requests", "api_errors", "prompts", "prompts_logged",
                  "sessions", "lines_added", "lines_removed", "commits",
                  "pull_requests", "active_seconds"):
            totals[k] += d[k]
        for k in d["tokens"]:
            totals["tokens"][k] += d["tokens"][k]
        totals["tools"]["accepted"] += d["tools"]["accepted"]
        totals["tools"]["rejected"] += d["tools"]["rejected"]

    totals["cost_usd"] = round(totals["cost_usd"], 4)
    for key in ("by_model", "first_ms", "last_ms", "ip", "ips", "user", "users",
                "dp", "dps", "compte", "comptes"):
        totals.pop(key, None)

    per_machine = [machines[k] for k in machines if k != "__TOTAL__"]
    per_machine.sort(key=lambda x: -x["cost_usd"])

    return {
        "generated_at": _fmt_ts(int(time.time() * 1000)) + " UTC",
        "days": days,
        "totals": totals,
        "machines": per_machine,
        "timeline": timeline,
    }


def _clamp_limit(limit, default=20):
    limit = default if limit is None else limit
    return max(1, min(int(limit), 1000))


def _identity_filters(machine="", user="", dp="", compte=""):
    """Clauses WHERE communes aux vues filtrables (activite, prompts, export)."""
    where = []
    params = []
    for col, val in (("machine", machine), ("user", user),
                     ("dp", dp), ("compte", compte)):
        if val:
            where.append(f"{col}=?")
            params.append(val)
    return where, params


def get_activite(days=0, machine="", user="", dp="", compte="",
                 limit=20, offset=0):
    """Flux d'activite pagine (l'onglet "Activite recente" du tableau de bord).

    Sorti de get_stats pour ne pas rejouer tout le flux a chaque poll de 30 s
    et permettre la pagination cote serveur.
    """
    cutoff = _cutoff_ms(days)
    limit = _clamp_limit(limit)
    offset = max(0, int(offset or 0))

    where, params = _identity_filters(machine, user, dp, compte)
    where.insert(0, "ts_ms>=?")
    params.insert(0, cutoff)
    clause = " AND ".join(where)

    with _db_lock:
        (total,) = _conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {clause}", params).fetchone()
        rows = _conn.execute(
            f"""
            SELECT ts_ms, machine, ip, user, dp, compte, name, model, cost_usd,
                   input_tokens, output_tokens, tool_name, decision
            FROM events WHERE {clause} ORDER BY ts_ms DESC LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

    activite = [
        {
            "time": _fmt_ts(r[0]),
            "machine": r[1],
            "ip": r[2] or "",
            "user": r[3] or "",
            "dp": r[4] or "",
            "compte": r[5] or "",
            "event": r[6],
            "model": r[7] or "",
            "cost_usd": round(float(r[8] or 0), 4),
            "tokens": int((r[9] or 0) + (r[10] or 0)),
            "tool": r[11] or "",
            "decision": r[12] or "",
        }
        for r in rows
    ]
    return {"count": int(total), "limit": limit, "offset": offset,
            "activite": activite}


def _fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_prompts(days=0, machine="", user="", dp="", compte="",
                query="", limit=200, offset=0):
    """Journal des prompts saisis (necessite OTEL_LOG_USER_PROMPTS=1 cote machine).

    Pagine : `count` est le total correspondant aux filtres, pas la taille de
    la page renvoyee.
    """
    cutoff = _cutoff_ms(days)
    limit = _clamp_limit(limit, default=200)
    offset = max(0, int(offset or 0))

    where, params = _identity_filters(machine, user, dp, compte)
    where[:0] = ["prompt<>''", "ts_ms>=?"]
    params.insert(0, cutoff)
    if query:
        # La recherche est litterale : on neutralise les jokers LIKE (% et _).
        escaped = (query.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_"))
        where.append("prompt LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    clause = " AND ".join(where)

    with _db_lock:
        (total,) = _conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {clause}", params).fetchone()
        rows = _conn.execute(
            f"SELECT ts_ms, machine, ip, user, dp, compte, session_id, model,"
            f" prompt, prompt_length"
            f" FROM events WHERE {clause} ORDER BY ts_ms DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    prompts = [
        {
            "time": _fmt_ts(r[0]),
            "machine": r[1],
            "ip": r[2] or "",
            "user": r[3] or "",
            "dp": r[4] or "",
            "compte": r[5] or "",
            "session_id": r[6] or "",
            "model": r[7] or "",
            "text": r[8] or "",
            "length": int(r[9] or 0),
        }
        for r in rows
    ]
    return {"count": int(total), "limit": limit, "offset": offset,
            "prompts": prompts}


# --------------------------------------------------------------------------
# Export CSV / XLS
# --------------------------------------------------------------------------
EXPORT_HEADERS = ("heure_utc", "machine", "ip", "utilisateur", "dp", "compte",
                  "evenement", "modele", "session", "tokens_entree",
                  "tokens_sortie", "cache_lecture", "cache_creation",
                  "cout_usd", "outil", "decision", "prompt")


def get_export_rows(days=0, user="", machine="", dp="", compte=""):
    """Evenements bruts d'un utilisateur (ou de tous), du plus recent au plus ancien."""
    cutoff = _cutoff_ms(days)
    where, params = _identity_filters(machine, user, dp, compte)
    where.insert(0, "ts_ms>=?")
    params.insert(0, cutoff)

    with _db_lock:
        rows = _conn.execute(
            f"""
            SELECT ts_ms, machine, ip, user, dp, compte, name, model, session_id,
                   input_tokens, output_tokens, cache_read, cache_creation,
                   cost_usd, tool_name, decision, prompt
            FROM events WHERE {' AND '.join(where)} ORDER BY ts_ms DESC
            """,
            params,
        ).fetchall()

    return [
        (_fmt_ts(r[0]), r[1] or "", r[2] or "", r[3] or "", r[4] or "", r[5] or "",
         r[6] or "", r[7] or "", r[8] or "",
         int(r[9] or 0), int(r[10] or 0), int(r[11] or 0), int(r[12] or 0),
         round(float(r[13] or 0), 6), r[14] or "", r[15] or "", r[16] or "")
        for r in rows
    ]


def export_csv(rows):
    """CSV pour Excel francais : BOM UTF-8 + point-virgule comme separateur."""
    def cell(v):
        s = str(v)
        if any(c in s for c in ';"\n\r'):
            s = '"' + s.replace('"', '""') + '"'
        return s

    lines = [";".join(EXPORT_HEADERS)]
    lines += [";".join(cell(v) for v in row) for row in rows]
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


def export_xls(rows):
    """Excel 2003 SpreadsheetML : du XML que Excel/LibreOffice ouvrent en .xls."""
    def esc(v):
        return (str(v).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def xml_row(values, types):
        cells = "".join(
            f'<Cell><Data ss:Type="{t}">{esc(v)}</Data></Cell>'
            for v, t in zip(values, types))
        return f"<Row>{cells}</Row>"

    header = xml_row(EXPORT_HEADERS, ["String"] * len(EXPORT_HEADERS))
    types = (["String"] * 9) + (["Number"] * 5) + (["String"] * 3)
    body = "".join(xml_row(row, types) for row in rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
        ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
        '<Worksheet ss:Name="Moniteur"><Table>'
        f"{header}{body}"
        "</Table></Worksheet></Workbook>"
    )


# --------------------------------------------------------------------------
# Sessions d'authentification (dashboard + API ; jamais l'ingestion OTLP)
# --------------------------------------------------------------------------
def create_session():
    """Ouvre une session et renvoie son jeton (a poser en cookie)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        # purge des sessions expirees au passage
        for t in [t for t, exp in _sessions.items() if exp < now]:
            del _sessions[t]
        _sessions[token] = now + SESSION_TTL
    return token


def session_valid(token):
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            del _sessions[token]
            return False
        return True


def destroy_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


def check_password(candidate):
    """Comparaison en temps constant (hmac) pour ne pas fuiter par timing."""
    if AUTH_PASSWORD is None:
        return True
    return hmac.compare_digest(str(candidate or ""), AUTH_PASSWORD)


# --------------------------------------------------------------------------
# Serveur HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencieux : on affiche nos propres logs

    def client_ip(self):
        """IP de la machine emettrice (X-Forwarded-For si passage par un proxy)."""
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _read_chunked(self):
        """Lit un corps HTTP en Transfer-Encoding: chunked.

        L'exporteur OTLP de Claude Code (OTel-JS) streame le corps sans
        Content-Length ; BaseHTTPRequestHandler ne le decode pas seul.
        """
        chunks = []
        while True:
            size_line = self.rfile.readline().strip()
            # taille en hexa, eventuelles extensions apres ';'
            size = int(size_line.split(b";", 1)[0] or b"0", 16)
            if size == 0:
                # consomme les trailers eventuels jusqu'a la ligne vide
                while self.rfile.readline().strip():
                    pass
                break
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)  # CRLF de fin de chunk
        return b"".join(chunks)

    def _read_body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raw = self._read_chunked()
        else:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
        if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return raw

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, payload):
        self._send(200, json.dumps(payload).encode("utf-8"))

    def _redirect(self, location, set_cookie=None):
        headers = {"Location": location}
        if set_cookie is not None:
            headers["Set-Cookie"] = set_cookie
        self._send(302, b"", "text/plain", headers=headers)

    def _session_token(self):
        """Jeton de session lu dans l'en-tete Cookie (chaine vide si absent)."""
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _authorized(self):
        return AUTH_PASSWORD is None or session_valid(self._session_token())

    def _serve_file(self, path, missing_label):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(500, f"{missing_label} introuvable".encode(), "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/login":
            self._handle_login()
            return

        if path not in ("/v1/metrics", "/v1/logs", "/v1/traces"):
            self._send(404, b'{"error":"not found"}')
            return

        ctype = (self.headers.get("Content-Type") or "").lower()
        raw = self._read_body()

        if path == "/v1/traces":
            self._send(200, b"{}")
            return

        if "json" not in ctype:
            print(f"[warn] {path}: Content-Type={ctype!r} non supporte "
                  f"(configurez OTEL_EXPORTER_OTLP_PROTOCOL=http/json)")
            self._send(415, b'{"error":"expected application/json"}')
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            print(f"[warn] JSON invalide sur {path}: {e}")
            self._send(400, b'{"error":"invalid json"}')
            return

        ip = self.client_ip()
        try:
            if path == "/v1/metrics":
                n = ingest_metrics(payload, ip)
                print(f"[ok] {n} point(s) de metrique recu(s) de {ip}")
            else:
                n, n_prompts = ingest_logs(payload, ip)
                extra = f", dont {n_prompts} prompt(s)" if n_prompts else ""
                print(f"[ok] {n} evenement(s) recu(s) de {ip}{extra}")
        except Exception as e:  # noqa: BLE001
            print(f"[error] ingestion {path}: {e}")
            self._send(500, b'{"error":"ingest failed"}')
            return

        self._send(200, b"{}")

    def _handle_login(self):
        """POST /login : verifie le mot de passe, pose le cookie de session."""
        if AUTH_PASSWORD is None:
            self._redirect("/")
            return
        form = parse_qs(self._read_body().decode("utf-8", errors="replace"))
        if not check_password(form.get("mot_de_passe", [""])[0]):
            self._redirect("/login?erreur=1")
            return
        token = create_session()
        cookie = (f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL}; "
                  "HttpOnly; SameSite=Lax")
        self._redirect("/", set_cookie=cookie)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        def qs_int(key, default=0):
            try:
                return int(q.get(key, [str(default)])[0])
            except (TypeError, ValueError):
                return default

        def qs_str(key):
            return q.get(key, [""])[0]

        if path == "/health":
            self._send(200, b'{"status":"ok"}')
            return

        if path == "/login":
            if AUTH_PASSWORD is None or self._authorized():
                self._redirect("/")
            else:
                self._serve_file(LOGIN_PATH, "login.html")
            return

        if path == "/logout":
            destroy_session(self._session_token())
            self._redirect("/login" if AUTH_PASSWORD is not None else "/",
                           set_cookie=f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
            return

        # Tout le reste (tableau de bord + API) exige une session si un mot de
        # passe est configure.
        if not self._authorized():
            if path.startswith("/api/"):
                self._send(401, b'{"error":"authentification requise"}')
            else:
                self._redirect("/login")
            return

        if path == "/api/stats":
            self._send_json(get_stats(qs_int("days")))
            return

        if path == "/api/prompts":
            self._send_json(get_prompts(
                days=qs_int("days"),
                machine=qs_str("machine"),
                user=qs_str("user"),
                dp=qs_str("dp"),
                compte=qs_str("compte"),
                query=qs_str("q"),
                limit=qs_int("limit", 200),
                offset=qs_int("offset", 0),
            ))
            return

        if path == "/api/activite":
            self._send_json(get_activite(
                days=qs_int("days"),
                machine=qs_str("machine"),
                user=qs_str("user"),
                dp=qs_str("dp"),
                compte=qs_str("compte"),
                limit=qs_int("limit", 20),
                offset=qs_int("offset", 0),
            ))
            return

        if path == "/api/export":
            user = qs_str("user")
            fmt = qs_str("format") or "csv"
            if fmt not in ("csv", "xls"):
                self._send(400, b'{"error":"format inconnu (csv ou xls)"}')
                return
            rows = get_export_rows(days=qs_int("days"), user=user,
                                   machine=qs_str("machine"),
                                   dp=qs_str("dp"), compte=qs_str("compte"))
            days = qs_int("days")
            stem = (f"moniteur_{user or 'tous'}_"
                    f"{f'{days}j' if days else 'tout'}")
            if fmt == "csv":
                body = export_csv(rows).encode("utf-8")
                ctype = "text/csv; charset=utf-8"
            else:
                body = export_xls(rows).encode("utf-8")
                ctype = "application/vnd.ms-excel"
            disposition = f"attachment; filename*=UTF-8''{quote(stem)}.{fmt}"
            self._send(200, body, ctype,
                       headers={"Content-Disposition": disposition})
            return

        if path in ("/", "/index.html"):
            self._serve_file(DASHBOARD_PATH, "dashboard.html")
            return

        self._send(404, b"not found", "text/plain")


def main():
    global AUTH_PASSWORD
    ap = argparse.ArgumentParser(description="Moniteur Claude Code")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--mot-de-passe", dest="mot_de_passe",
                    default=os.environ.get("MONITEUR_MOT_DE_PASSE") or None,
                    help="Protege le tableau de bord et l'API par mot de passe "
                         "(defaut : variable MONITEUR_MOT_DE_PASSE ; absent = "
                         "acces libre). L'ingestion OTLP reste toujours ouverte.")
    args = ap.parse_args()
    AUTH_PASSWORD = args.mot_de_passe

    init_db()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 60)
    print(" Moniteur Claude Code demarre")
    print(f" Tableau de bord : http://localhost:{args.port}/")
    print(f" Endpoint OTLP   : http://<IP-de-cette-machine>:{args.port}")
    print(f" Base de donnees : {DB_PATH}")
    print(f" Authentification: {'activee' if AUTH_PASSWORD else 'desactivee'}"
          + ("" if AUTH_PASSWORD else " (--mot-de-passe pour proteger)"))
    print("=" * 60)
    print(" En attente de telemetrie... (Ctrl+C pour arreter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")
        server.shutdown()


if __name__ == "__main__":
    main()
