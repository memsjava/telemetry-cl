#!/usr/bin/env python3
"""
Configure CETTE machine pour qu'elle envoie sa telemetrie Claude Code au Moniteur.

Ecrit les variables OTEL dans la cle "env" de ~/.claude/settings.json, que Claude
Code applique a *toutes* ses sessions. Aucune variable d'environnement systeme,
aucun profil shell modifie, aucun terminal a rouvrir.

Les autres reglages presents dans settings.json (permissions, model, hooks...)
sont conserves : seules les cles OTEL_* / CLAUDE_CODE_ENABLE_TELEMETRY sont
ajoutees ou mises a jour. Une sauvegarde .bak est ecrite avant modification.

L'utilisateur systeme (session Windows/Mac/Linux) est detecte automatiquement
et pose dans OTEL_RESOURCE_ATTRIBUTES aux cotes de "machine=" : Claude Code
n'expose pas nativement le nom d'utilisateur OS (seul user.email, identique
pour tous quand un compte Pro/Max est partage).

Lance dans un terminal, le script demande interactivement l'utilisateur, le
directeur de projet (dp) et le nom du compte Claude partage (ex. GroupeAI1) ;
la valeur deja configuree (ou detectee) est proposee par defaut, Entree la
conserve. Les options --utilisateur/--dp/--compte court-circuitent la question
correspondante ; sans terminal (stdin non interactif), rien n'est demande et
les valeurs deja presentes dans settings.json sont conservees.

Le collecteur (--collecteur) a une valeur par defaut (DEFAULT_COLLECTEUR, IP fixe
du serveur central) : inutile de la retaper sur chaque machine suivie. --machine
a lui aussi une valeur par defaut (nom d'hote de la machine courante).

Utilisation :
    python cli/configurer_machine.py                     (machine = hostname, collecteur = defaut)
    python cli/configurer_machine.py --machine pc-bureau  (etiquette explicite)
    python cli/configurer_machine.py --collecteur localhost --machine pc-bureau  (autre collecteur)
    python cli/configurer_machine.py --utilisateur eric --dp jean --compte GroupeAI1
    python cli/configurer_machine.py --sans-prompts
    python cli/configurer_machine.py --retirer                         (annule la config)
    python cli/configurer_machine.py --simuler                         (aucune ecriture)
"""

from __future__ import annotations

import argparse
import getpass
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

DEFAULT_PORT = 4318
# IP fixe du serveur central : evite de la retaper sur chacune des machines suivies.
DEFAULT_COLLECTEUR = "185.185.82.139"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Cles posees par ce script : ce sont exactement celles que --retirer enleve.
MANAGED_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_LOGS_EXPORT_INTERVAL",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_ASSISTANT_RESPONSES",
)


def detect_os_user() -> str:
    """Nom de la session systeme (Windows/Mac/Linux) courante."""
    try:
        return getpass.getuser()
    except OSError:
        return "inconnu"


def _encode_resource_value(value: str) -> str:
    """Encode une valeur pour OTEL_RESOURCE_ATTRIBUTES (format cle=valeur,cle=valeur).

    Le "=" et la "," delimitent la syntaxe : s'ils apparaissent dans une valeur
    (nom d'utilisateur ou de machine inhabituel), on les encode en pourcent pour
    ne pas casser le parsing des attributs suivants.
    """
    return value.replace("%", "%25").replace(",", "%2C").replace("=", "%3D")


def _decode_resource_value(value: str) -> str:
    """Inverse de _encode_resource_value (ordre inverse des remplacements)."""
    return value.replace("%3D", "=").replace("%2C", ",").replace("%25", "%")


def parse_resource_attrs(raw: str) -> Dict[str, str]:
    """Decompose une chaine OTEL_RESOURCE_ATTRIBUTES en dict decode.

    Sert a re-proposer les valeurs deja configurees (user/dp/compte) comme
    defauts quand le script est relance sur une machine deja installee.
    """
    out: Dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = _decode_resource_value(value.strip())
    return out


def build_env(collecteur: str, machine: str, port: int = DEFAULT_PORT,
              log_prompts: bool = True, os_user: str | None = None,
              dp: str = "", compte: str = "") -> Dict[str, str]:
    """Variables a poser pour que Claude Code emette vers le Moniteur."""
    if os_user is None:
        os_user = detect_os_user()
    resource_attrs = (f"machine={_encode_resource_value(machine)}"
                      f",user={_encode_resource_value(os_user)}")
    if dp:
        resource_attrs += f",dp={_encode_resource_value(dp)}"
    if compte:
        resource_attrs += f",compte={_encode_resource_value(compte)}"
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        # http/json est obligatoire : le serveur ne lit pas le protobuf.
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{collecteur}:{port}",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "OTEL_LOGS_EXPORT_INTERVAL": "5000",
        "OTEL_RESOURCE_ATTRIBUTES": resource_attrs,
        "OTEL_LOG_USER_PROMPTS": "1" if log_prompts else "0",
        # Les reponses de Claude ne nous interessent pas : volume + confidentialite.
        "OTEL_LOG_ASSISTANT_RESPONSES": "0",
    }


def demander(label: str, defaut: str = "") -> str:
    """Pose une question dans le terminal ; Entree conserve la valeur par defaut."""
    suffixe = f" [{defaut}]" if defaut else ""
    try:
        reponse = input(f"{label}{suffixe} : ").strip()
    except EOFError:
        return defaut
    return reponse or defaut


def resoudre_identite(utilisateur: str | None, dp: str | None, compte: str | None,
                      existants: Dict[str, str], interactif: bool,
                      poser=demander) -> tuple[str, str, str]:
    """Complete utilisateur/dp/compte : option CLI > question interactive > existant.

    `existants` est le contenu decode de l'OTEL_RESOURCE_ATTRIBUTES deja en
    place (vide pour une premiere installation) : relancer le script ne perd
    jamais une valeur saisie precedemment.
    """
    if interactif:
        if utilisateur is None:
            utilisateur = poser("Utilisateur", existants.get("user") or detect_os_user())
        if dp is None:
            dp = poser("Directeur de projet (dp)", existants.get("dp", ""))
        if compte is None:
            compte = poser("Compte Claude (ex. GroupeAI1)", existants.get("compte", ""))
    if utilisateur is None:
        utilisateur = detect_os_user()
    if dp is None:
        dp = existants.get("dp", "")
    if compte is None:
        compte = existants.get("compte", "")
    return utilisateur, dp, compte


def load_settings(path: Path) -> Dict[str, Any]:
    """Lit settings.json. Un fichier absent donne un reglage vide."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    settings = json.loads(raw)
    if not isinstance(settings, dict):
        raise ValueError(f"{path} ne contient pas un objet JSON")
    return settings


def merge_env(settings: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    """Fusionne env dans settings['env'] sans toucher au reste du fichier."""
    merged = dict(settings)
    current = merged.get("env")
    if current is not None and not isinstance(current, dict):
        raise ValueError('la cle "env" de settings.json n\'est pas un objet')
    merged["env"] = {**(current or {}), **env}
    return merged


def remove_env(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Retire les cles posees par ce script, et "env" s'il devient vide."""
    merged = dict(settings)
    current = merged.get("env")
    if not isinstance(current, dict):
        return merged
    kept = {k: v for k, v in current.items() if k not in MANAGED_KEYS}
    if kept:
        merged["env"] = kept
    else:
        merged.pop("env", None)
    return merged


def write_settings(path: Path, settings: Dict[str, Any]) -> Path | None:
    """Ecrit settings.json apres avoir sauvegarde l'existant. Renvoie le .bak."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return backup


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Configure cette machine pour le Moniteur Claude Code.")
    ap.add_argument("--collecteur", "-c", default=DEFAULT_COLLECTEUR,
                    help="IP ou nom de la machine qui heberge server.py "
                         f"(defaut : {DEFAULT_COLLECTEUR})")
    ap.add_argument("--machine", "-m", default=platform.node(),
                    help="Etiquette de CETTE machine dans le tableau de bord "
                         "(defaut : nom d'hote)")
    ap.add_argument("--port", "-p", type=int, default=DEFAULT_PORT)
    ap.add_argument("--utilisateur", "-u", default=None,
                    help="Nom d'utilisateur affiche dans le tableau de bord "
                         "(defaut : utilisateur systeme courant)")
    ap.add_argument("--dp", default=None,
                    help="Directeur de projet responsable de cette machine")
    ap.add_argument("--compte", default=None,
                    help="Nom du compte Claude partage (ex. GroupeAI1)")
    ap.add_argument("--sans-prompts", action="store_true", dest="sans_prompts",
                    help="Ne pas enregistrer le texte des prompts "
                         "(compteurs et couts uniquement)")
    ap.add_argument("--retirer", action="store_true",
                    help="Retire la configuration du Moniteur de settings.json")
    ap.add_argument("--simuler", action="store_true",
                    help="Affiche le resultat sans rien ecrire")
    ap.add_argument("--settings", type=Path, default=SETTINGS_PATH,
                    help=f"Chemin de settings.json (defaut : {SETTINGS_PATH})")
    args = ap.parse_args()

    if not args.retirer and not args.collecteur:
        ap.error("--collecteur est requis (ou utilisez --retirer)")

    try:
        settings = load_settings(args.settings)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Erreur : {args.settings} est illisible ({e})", file=sys.stderr)
        print("Corrigez ou supprimez ce fichier, puis relancez.", file=sys.stderr)
        return 1

    if args.retirer:
        updated = remove_env(settings)
        action = "retiree de"
        details = ""
    else:
        env_actuel = settings.get("env")
        existants = parse_resource_attrs(
            env_actuel.get("OTEL_RESOURCE_ATTRIBUTES", "")
            if isinstance(env_actuel, dict) else "")
        os_user, dp, compte = resoudre_identite(
            args.utilisateur, args.dp, args.compte, existants,
            interactif=sys.stdin.isatty())
        env = build_env(args.collecteur, args.machine, args.port,
                        log_prompts=not args.sans_prompts, os_user=os_user,
                        dp=dp, compte=compte)
        updated = merge_env(settings, env)
        action = "ecrite dans"
        details = (f"  machine  : {args.machine}\n"
                   f"  utilisateur : {os_user}\n"
                   f"  directeur de projet : {dp or '-'}\n"
                   f"  compte   : {compte or '-'}\n"
                   f"  endpoint : {env['OTEL_EXPORTER_OTLP_ENDPOINT']}\n"
                   f"  prompts  : {'enregistres' if not args.sans_prompts else 'non enregistres'}\n")

    if args.simuler:
        print(f"[simulation] {args.settings} deviendrait :\n")
        print(json.dumps(updated, indent=2, ensure_ascii=False))
        return 0

    try:
        backup = write_settings(args.settings, updated)
    except OSError as e:
        print(f"Erreur d'ecriture sur {args.settings} : {e}", file=sys.stderr)
        return 1

    print(f"OK - configuration {action} {args.settings}")
    if details:
        print(details, end="")
    if backup:
        print(f"  (sauvegarde de l'ancien fichier : {backup})")
    if not args.retirer:
        preserved = [k for k in settings if k != "env"]
        if preserved:
            print(f"  reglages conserves : {', '.join(preserved)}")
        print("\nLancez 'claude' : la telemetrie part des la prochaine session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
