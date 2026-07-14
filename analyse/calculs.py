"""
Calculs pandas de la page /analyse (voir analyse/__init__.py pour le statut
de dependance optionnelle du package).

Toutes les fonctions recoivent la connexion sqlite3 partagee de server.py ;
l'appelant detient deja le verrou _db_lock.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict

import pandas as pd

JOURS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

_TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read", "cache_creation")


def _cutoff_ms(days: int) -> int:
    if days and days > 0:
        return int((time.time() - days * 86400) * 1000)
    return 0


def _records(df: pd.DataFrame) -> list:
    """DataFrame -> liste de dicts JSON-serialisables (via to_json : les types
    numpy et les NaN/inf ne passent pas dans json.dumps directement)."""
    cleaned = df.replace([float("inf"), float("-inf")], 0)
    return json.loads(cleaned.to_json(orient="records"))


def _label(serie: pd.Series, inconnu: str = "(inconnu)") -> pd.Series:
    return serie.replace("", inconnu)


def _lire_events(conn, cutoff: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT ts_ms, machine, user, dp, compte, name, model,"
        " input_tokens, output_tokens, cache_read, cache_creation,"
        " cost_usd, prompt_length"
        " FROM events WHERE ts_ms>=?",
        conn, params=(cutoff,))


def _lire_metrics(conn, cutoff: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT ts_ms, machine, user, name, type, value"
        " FROM metrics WHERE ts_ms>=?",
        conn, params=(cutoff,))


def _par_identite(events: pd.DataFrame, metrics: pd.DataFrame,
                  cle: str) -> list:
    """Agregats par utilisateur / dp / compte : couts, tokens, prompts,
    et productivite (commits, lignes) issue de la table metrics."""
    if events.empty:
        return []
    df = events.assign(**{cle: _label(events[cle])})
    grp = df.groupby(cle)
    agg = pd.DataFrame({
        "cout_usd": grp["cost_usd"].sum().round(4),
        "tokens": grp[list(_TOKEN_COLS)].sum().sum(axis=1).astype(int),
        "prompts": grp["name"].apply(lambda s: int((s == "user_prompt").sum())),
        "requetes_api": grp["name"].apply(lambda s: int((s == "api_request").sum())),
        "machines": grp["machine"].nunique(),
    })
    agg["cout_moyen_par_prompt"] = (
        (agg["cout_usd"] / agg["prompts"]).where(agg["prompts"] > 0, 0).round(4))

    if cle == "user" and not metrics.empty:
        m = metrics.assign(user=_label(metrics["user"]))
        commits = m[m["name"].str.contains("commit", na=False)] \
            .groupby("user")["value"].sum()
        lignes = m[m["name"].str.contains("lines_of_code", na=False)
                   & (m["type"] == "added")].groupby("user")["value"].sum()
        agg["commits"] = commits.reindex(agg.index).fillna(0).astype(int)
        agg["lignes_ajoutees"] = lignes.reindex(agg.index).fillna(0).astype(int)

    return _records(agg.sort_values("cout_usd", ascending=False).reset_index())


def _par_modele(events: pd.DataFrame) -> list:
    req = events[events["name"] == "api_request"]
    if req.empty:
        return []
    grp = req.groupby(req["model"].replace("", "(inconnu)"))
    agg = pd.DataFrame({
        "cout_usd": grp["cost_usd"].sum().round(4),
        "tokens": grp[list(_TOKEN_COLS)].sum().sum(axis=1).astype(int),
        "requetes": grp.size(),
    })
    total = agg["cout_usd"].sum()
    agg["part_cout_pct"] = ((agg["cout_usd"] / total * 100).round(1)
                            if total > 0 else 0.0)
    return _records(agg.sort_values("cout_usd", ascending=False)
                    .reset_index().rename(columns={"model": "modele"}))


def _par_heure(events: pd.DataFrame) -> list:
    req = events[events["name"] == "api_request"]
    if req.empty:
        return []
    heures = pd.to_datetime(req["ts_ms"], unit="ms", utc=True).dt.hour
    grp = req.groupby(heures)
    agg = pd.DataFrame({"cout_usd": grp["cost_usd"].sum().round(4),
                        "requetes": grp.size()})
    agg = agg.reindex(range(24), fill_value=0)
    agg.index.name = "heure"
    return _records(agg.reset_index())


def _par_jour_semaine(events: pd.DataFrame) -> list:
    req = events[events["name"] == "api_request"]
    if req.empty:
        return []
    jours = pd.to_datetime(req["ts_ms"], unit="ms", utc=True).dt.dayofweek
    grp = req.groupby(jours)
    agg = pd.DataFrame({"cout_usd": grp["cost_usd"].sum().round(4),
                        "requetes": grp.size()})
    agg = agg.reindex(range(7), fill_value=0)
    agg.insert(0, "jour", [JOURS_FR[i] for i in agg.index])
    return _records(agg.reset_index(drop=True))


def _tendance(events: pd.DataFrame) -> Dict[str, Any]:
    """Cout des 7 derniers jours vs les 7 precedents (independant de ?days=)."""
    now_ms = int(time.time() * 1000)
    j7 = now_ms - 7 * 86400_000
    j14 = now_ms - 14 * 86400_000
    req = events[events["name"] == "api_request"]
    cout_7j = float(req.loc[req["ts_ms"] >= j7, "cost_usd"].sum())
    cout_prec = float(req.loc[(req["ts_ms"] >= j14)
                              & (req["ts_ms"] < j7), "cost_usd"].sum())
    variation = (round((cout_7j - cout_prec) / cout_prec * 100, 1)
                 if cout_prec > 0 else None)
    return {"cout_7_derniers_jours": round(cout_7j, 4),
            "cout_7_jours_precedents": round(cout_prec, 4),
            "variation_pct": variation}


def _cache_par_machine(events: pd.DataFrame) -> list:
    req = events[events["name"] == "api_request"]
    if req.empty:
        return []
    grp = req.groupby("machine")
    agg = pd.DataFrame({
        "cache_lu": grp["cache_read"].sum().astype(int),
        "tokens_entree": grp["input_tokens"].sum().astype(int),
    })
    lus = agg["cache_lu"] + agg["tokens_entree"]
    agg["taux_cache_pct"] = ((agg["cache_lu"] / lus * 100)
                             .where(lus > 0, 0).round(1))
    return _records(agg.sort_values("taux_cache_pct", ascending=False)
                    .reset_index())


def _erreurs_par_machine(events: pd.DataFrame) -> list:
    df = events[events["name"].isin(["api_request", "api_error"])]
    if df.empty:
        return []
    grp = df.groupby("machine")["name"]
    agg = pd.DataFrame({
        "requetes_api": grp.apply(lambda s: int((s == "api_request").sum())),
        "erreurs_api": grp.apply(lambda s: int((s == "api_error").sum())),
    })
    total = agg["requetes_api"] + agg["erreurs_api"]
    agg["taux_erreur_pct"] = ((agg["erreurs_api"] / total * 100)
                              .where(total > 0, 0).round(1))
    return _records(agg.sort_values("taux_erreur_pct", ascending=False)
                    .reset_index())


def _longueur_prompts(events: pd.DataFrame) -> Dict[str, Any]:
    longueurs = events.loc[(events["name"] == "user_prompt")
                           & (events["prompt_length"] > 0), "prompt_length"]
    if longueurs.empty:
        return {"nombre": 0, "moyenne": 0, "mediane": 0, "p90": 0, "max": 0}
    return {"nombre": int(longueurs.count()),
            "moyenne": round(float(longueurs.mean()), 1),
            "mediane": round(float(longueurs.median()), 1),
            "p90": round(float(longueurs.quantile(0.9)), 1),
            "max": int(longueurs.max())}


def compute(conn, days: int = 0) -> Dict[str, Any]:
    """Toutes les analyses de la page /analyse, en un seul dict JSON-able."""
    cutoff = _cutoff_ms(days)
    events = _lire_events(conn, cutoff)
    metrics = _lire_metrics(conn, cutoff)

    return {
        "disponible": True,
        "pandas_version": pd.__version__,
        "periode_jours": days,
        "evenements_analyses": int(len(events)),
        "par_utilisateur": _par_identite(events, metrics, "user"),
        "par_dp": _par_identite(events, metrics, "dp"),
        "par_compte": _par_identite(events, metrics, "compte"),
        "par_modele": _par_modele(events),
        "par_heure": _par_heure(events),
        "par_jour_semaine": _par_jour_semaine(events),
        "tendance": _tendance(events),
        "cache_par_machine": _cache_par_machine(events),
        "erreurs_par_machine": _erreurs_par_machine(events),
        "longueur_prompts": _longueur_prompts(events),
    }
