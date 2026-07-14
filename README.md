# Moniteur Claude Code

Petite application sur-mesure pour suivre votre usage de **Claude Code** sur
plusieurs machines : tokens dépensés, coût estimé, sessions, tâches (outils,
commits, PR, lignes de code), **adresse IP**, **utilisateur système**,
**contenu des prompts**, date **et heure** — le tout **par machine**, en
quasi temps réel.

Aucun token d'authentification à saisir : chaque machine **émet** elle-même sa
télémétrie (standard OpenTelemetry, intégré nativement à Claude Code) vers ce
petit serveur, qui la stocke et l'affiche.

```
Machine A ─┐  (télémétrie activée, étiquette machine=A)
Machine B ─┤──►  server.py  ──►  data/telemetry.db  ──►  tableau de bord web
Machine C ─┘  (télémétrie activée, étiquette machine=C)
```

---

## Prérequis

- **Python 3.8+** sur la machine qui héberge le moniteur.
  Vérifier : `python --version` (ou `python3 --version`).
- Les machines à suivre doivent pouvoir joindre l'hébergeur sur le port **4318**
  — même réseau local, VPN, ou adresse publique (voir « Adresse du collecteur »
  plus bas si ce n'est pas le même réseau).

> ⚠️ Fonctionne pour Claude Code utilisé via l'**API Anthropic** ou un
> **abonnement** (Pro/Max) — la télémétrie est côté machine, indépendante du
> mode d'authentification. En revanche, l'usage via Bedrock / Vertex / Foundry
> n'est pas couvert.

---

## 0. Récupérer le projet (sur la machine qui héberge le serveur)

```bash
npx degit memsjava/telemetry-cl moniteur-claude-code
cd moniteur-claude-code
```

`degit` télécharge une copie propre des fichiers du dépôt (sans historique git,
sans clone) — nécessite seulement Node.js, pas `git`. Si `git` est disponible et
que vous préférez cette méthode : `git clone https://github.com/memsjava/telemetry-cl.git`.
Ni l'un ni l'autre : bouton **Code → Download ZIP** sur la page GitHub du
projet, puis décompressez et ouvrez un terminal dans le dossier obtenu.

> Remplacez `memsjava/telemetry-cl` par ce dépôt une fois poussé sur GitHub. La machine
> serveur n'a besoin que de Python pour tourner — `npx`/`degit` (ou `git`, ou le
> ZIP) ne servent qu'à récupérer le code une fois.

---

## 1. Démarrer le serveur (sur la machine « centrale »)

```bash
python server.py
```

Sous Windows, vous pouvez double-cliquer [`windows/0-DEMARRER-TOUT.bat`](windows/0-DEMARRER-TOUT.bat)
à la place (démarre le serveur **et** ouvre le tableau de bord). Les autres
scripts de `windows/` sont des raccourcis pour les étapes individuelles :
[`1-demarrer-moniteur.bat`](windows/1-demarrer-moniteur.bat) (serveur seul),
[`2-ouvrir-tableau-de-bord.bat`](windows/2-ouvrir-tableau-de-bord.bat),
[`autoriser-pare-feu.bat`](windows/autoriser-pare-feu.bat) (voir plus bas).

Vous verrez :

```
 Tableau de bord : http://localhost:4318/
 Endpoint OTLP   : http://<IP-de-cette-machine>:4318
```

Laissez cette fenêtre ouverte. Ouvrez le tableau de bord dans votre navigateur :
**http://localhost:4318/**

### Protéger le tableau de bord par mot de passe

```bash
python server.py --mot-de-passe votre-secret
# ou : MONITEUR_MOT_DE_PASSE=votre-secret python server.py
```

Le tableau de bord et l'API (`/api/*`) exigent alors une connexion (page
`/login`, session de 7 jours, lien « Se deconnecter » dans l'en-tête).
**L'ingestion OTLP (`/v1/*`) reste toujours ouverte** : les machines suivies
n'ont aucun identifiant à configurer. Sans l'option, l'accès reste libre comme
avant. Un redémarrage du serveur invalide les sessions (elles sont en mémoire).

> 💡 Notez l'**adresse IP locale** de cette machine (`ipconfig` sous Windows,
> `ifconfig`/`ip a` sous macOS/Linux) — les autres machines en auront besoin.
> Si vous n'utilisez qu'une seule machine, l'adresse est simplement `localhost`.

### Adresse du collecteur : LAN, VPN, ou serveur distant ?

Ce projet est configuré avec **`185.185.82.139`** (IP publique fixe du
serveur) comme collecteur par défaut — posé dans `cli/configurer_machine.py`,
`bin/configurer-machine.js` et `claude-settings.template.json`. La plupart des
machines n'ont donc rien à préciser.

La valeur donnée à `--collecteur` (ou à `OTEL_EXPORTER_OTLP_ENDPOINT`) reste
cependant *l'adresse à laquelle une machine suivie donnée peut joindre le
serveur* — rien n'empêche de la forcer différemment pour un cas particulier :

| Situation | Valeur à utiliser |
|---|---|
| Cas général (IP publique fixe) | rien à faire — c'est la valeur par défaut |
| Machine suivie = machine du serveur elle-même | `--collecteur localhost` |
| Même réseau local (LAN), pour éviter le détour par Internet | `--collecteur 192.168.x.x` (IP locale du serveur) |
| Accès via VPN maillé (Tailscale, ZeroTier…) plutôt que l'IP publique | `--collecteur <IP-VPN>` |

Si cette IP publique venait à changer (box réinitialisée, changement de FAI),
il faudra mettre à jour la constante dans les deux installeurs et le template,
puis reconfigurer les machines déjà en place.

> ⚠️ **Ce serveur n'a aucune authentification.** Avec une IP publique fixe
> comme collecteur, le port 4318 est joignable depuis **tout Internet** : qui
> que ce soit peut y lire tous les prompts et IP en clair (`/api/stats`,
> `/api/prompts`), ou y injecter de fausses métriques, s'il devine ou scanne
> cette adresse. Pour réduire ce risque : limitez 4318 par pare-feu aux IP des
> machines suivies si elles sont connues et fixes, ou placez un reverse proxy
> avec authentification devant ce port (non fourni par ce projet). Un VPN
> maillé (Tailscale, ZeroTier…) reste l'alternative la plus simple si vous
> pouvez basculer dessus.

---

## 2. Configurer chaque machine à suivre

Claude Code lit ses réglages dans `~/.claude/settings.json`, dont la clé `env`
est appliquée à **toutes** les sessions. C'est là qu'on déclare la télémétrie :
rien à installer, aucune variable d'environnement système, aucun terminal à
rouvrir.

**`--collecteur` et `--machine` ont tous les deux une valeur par défaut** —
inutile de les retaper sur chacun des postes suivis : le collecteur pointe par
défaut vers l'IP fixe du serveur (`185.185.82.139`), et l'étiquette de machine
est détectée automatiquement (nom d'hôte de la machine courante). La commande
la plus simple est donc, sur chaque poste :

### Méthode recommandée : npx (aucun clone, aucune dépendance à installer)

```bash
npx github:memsjava/telemetry-cl
```

> Remplacez `memsjava/telemetry-cl` par ce dépôt une fois poussé sur GitHub. Nécessite
> Node.js (déjà présent si Claude Code a été installé via npm ; sinon
> [nodejs.org](https://nodejs.org)).

### Alternative : Python

Le script est autonome (aucune dépendance, aucun autre fichier du dépôt) : pas
besoin de cloner tout le projet sur chaque machine suivie, un simple
téléchargement suffit.

```bash
curl -o configurer_machine.py https://raw.githubusercontent.com/memsjava/telemetry-cl/main/cli/configurer_machine.py
python configurer_machine.py
```

Sous Windows (PowerShell), remplacez la ligne `curl` par :
```powershell
Invoke-WebRequest https://raw.githubusercontent.com/memsjava/telemetry-cl/main/cli/configurer_machine.py -OutFile configurer_machine.py
```

Si le dépôt est déjà cloné sur cette machine (par exemple, c'est la machine
serveur elle-même), lancez-le directement depuis là :
```bash
python cli/configurer_machine.py
```

Les deux méthodes (npx et Python) sont strictement équivalentes (même logique,
mêmes options, même fichier produit) — utilisez celle disponible sur la machine.

Pour donner une étiquette explicite plutôt que le nom d'hôte détecté, ou pointer
vers un autre collecteur (une autre IP, `localhost`…) :
```bash
npx github:memsjava/telemetry-cl --machine pc-bureau --collecteur 192.168.1.20
```

Les deux **fusionnent** dans `~/.claude/settings.json` : vos réglages existants
(`model`, `permissions`, `hooks`…) sont conservés, et l'ancien fichier est
sauvegardé en `.bak`. Options utiles (identiques dans les deux versions) :

| Option | Effet |
|---|---|
| `--machine <nom>` | force l'étiquette affichée (sinon : nom d'hôte détecté automatiquement) |
| `--collecteur <hôte>` | force le serveur ciblé (sinon : `185.185.82.139`) |
| `--utilisateur <nom>` | force le nom d'utilisateur affiché (sinon détecté automatiquement) |
| `--sans-prompts` | ne pas enregistrer le texte des prompts (compteurs et coûts seulement) |
| `--simuler` | affiche le résultat sans rien écrire |
| `--retirer` | annule la configuration, sans toucher au reste du fichier |

Puis lancez `claude` : la télémétrie part dès la session suivante.

### Configuration manuelle (sans Python ni Node sur la machine)

Copiez le contenu de [`claude-settings.template.json`](claude-settings.template.json)
dans `~/.claude/settings.json` (sous Windows :
`C:\Users\<vous>\.claude\settings.json`), en adaptant les valeurs :
`OTEL_EXPORTER_OTLP_ENDPOINT`, `machine=…` et `user=…`. Si le fichier existe
déjà, fusionnez uniquement la clé `env` — n'écrasez pas le reste.

### Et pourquoi pas un *hook* global ?

Un hook Claude Code pourrait pousser lui-même les données vers le serveur, mais
**aucun payload de hook ne contient les tokens ni le coût** (`input_tokens`,
`output_tokens`, `cost_usd`) : ces valeurs n'existent que dans la télémétrie
OpenTelemetry. Un hook saurait remonter le prompt et la session, pas le
chiffrage — soit l'essentiel de ce moniteur. D'où le choix d'OTel, la clé `env`
supprimant de toute façon la corvée d'installation qui motivait l'idée.

### Variables posées (référence)

La clé `env` de `settings.json` reçoit :

| Variable | Valeur |
|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` |
| `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_LOGS_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/json` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://185.185.82.139:4318` (valeur par défaut) |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `delta` |
| `OTEL_RESOURCE_ATTRIBUTES` | `machine=<nom-de-la-machine>,user=<utilisateur-systeme>,dp=<directeur-de-projet>,compte=<compte-claude>` |
| `OTEL_LOG_USER_PROMPTS` | `1` (enregistre le **texte** des prompts ; `0` pour ne pas le faire) |
| `OTEL_LOG_ASSISTANT_RESPONSES` | `0` (ne pas enregistrer les réponses de Claude) |

> `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` est **obligatoire** : ce serveur ne
> lit que le JSON (pas le protobuf), pour rester sans dépendance.

> Sans `OTEL_LOG_USER_PROMPTS=1`, Claude Code remonte le *nombre* de prompts mais
> **pas leur contenu**. `OTEL_LOG_USER_PROMPTS` active aussi les réponses de
> l'assistant : `OTEL_LOG_ASSISTANT_RESPONSES=0` les désactive (volume inutile ici).

L'**adresse IP** n'a rien à configurer : le serveur la lit sur la connexion de
chaque machine qui lui envoie sa télémétrie.

Le **nom d'utilisateur** (session Windows/Mac/Linux), lui, n'est pas exposé
nativement par la télémétrie de Claude Code — seul `user.email` l'est, et il
est identique pour tout le monde quand un compte Pro/Max est partagé. C'est
pour ça que `cli/configurer_machine.py` (ou l'installeur Node) l'ajoute
lui-même dans `OTEL_RESOURCE_ATTRIBUTES`, aux côtés de `machine=`.

Même logique pour le **directeur de projet** (`dp=`) et le **nom du compte
Claude partagé** (`compte=`, ex. `GroupeAI1`) : lancé dans un terminal,
l'installeur demande interactivement l'utilisateur, le DP et le compte (Entrée
conserve la valeur proposée — celle déjà configurée, ou détectée). Les options
`--utilisateur`, `--dp` et `--compte` court-circuitent les questions ; hors
terminal (lancement scripté), rien n'est demandé et les valeurs déjà
configurées sont conservées. Ces champs sont facultatifs : absents, le tableau
de bord affiche simplement « — ».

---

## 3. Utiliser Claude Code… et regarder

Utilisez Claude Code comme d'habitude. Au bout de quelques secondes, les
données remontent (intervalle réglé à ~10 s). Le tableau de bord se rafraîchit
automatiquement toutes les 30 secondes.

Vous y trouverez, par machine et au total :
- **Coût estimé** (USD) et **tokens** (entrée / sortie / cache) ;
- **Adresse IP** et **utilisateur système** de chaque machine (et tous ceux
  vus, si plusieurs personnes ou réseaux se succèdent) ;
- **Directeur de projet** et **compte Claude partagé** déclarés à
  l'installation de chaque machine ;
- **Sessions**, requêtes API, prompts, erreurs ;
- le **contenu des prompts**, avec recherche plein texte et filtre par machine ;
- **Tâches** : commits, pull requests, lignes de code ajoutées/supprimées ;
- **Taux d'acceptation** des suggestions d'outils ;
- **Détail par modèle** et **courbe de coût journalier** ;
- un **flux d'activité récente** horodaté à la seconde.

Les tableaux **Prompts** et **Activité récente** sont paginés (20 lignes par
page, boutons Précédent/Suivant) — la recherche et les filtres s'appliquent à
l'ensemble, pas seulement à la page affichée.

Le panneau **Export** télécharge les événements d'un utilisateur (ou de tous)
sur la période affichée, en **CSV** (UTF-8 + point-virgule, s'ouvre
directement dans Excel) ou en **XLS** : horodatage, machine, IP, utilisateur,
DP, compte, tokens, coûts, outils et texte des prompts. Aussi accessible en
direct : `GET /api/export?user=eric&days=7&format=csv`.

---

## Confidentialité

Tout reste **chez vous** : les données ne sortent jamais de votre réseau, la base
`data/telemetry.db` est un simple fichier local.

Ce qui est enregistré dépend de la configuration :

| Donnée | Enregistrée ? |
|---|---|
| Compteurs (tokens, coût, sessions, outils, commits…) | toujours |
| Adresse IP de la machine émettrice | toujours (lue sur la connexion) |
| Utilisateur système (session Windows/Mac/Linux) | toujours (posé par le configurateur dans `OTEL_RESOURCE_ATTRIBUTES`) |
| Directeur de projet et compte Claude (`dp=`, `compte=`) | si renseignés à l'installation (questions interactives ou `--dp`/`--compte`) |
| **Texte des prompts** | **oui, si `OTEL_LOG_USER_PROMPTS=1`** (valeur par défaut posée par le configurateur) |
| Réponses de Claude | non (`OTEL_LOG_ASSISTANT_RESPONSES=0`) |

Le texte des prompts est stocké **en clair** dans `data/telemetry.db` et affiché
dans le tableau de bord, lui-même servi **sans authentification** à quiconque
atteint le port 4318. Si des tiers utilisent ces machines, prévenez-les — et
évitez d'exposer ce port au-delà de votre réseau local ou de votre VPN (voir
« Adresse du collecteur » plus haut si vous suivez des machines distantes).

Pour ne pas enregistrer les prompts sur une machine :
`python cli/configurer_machine.py --collecteur <IP> --sans-prompts`.

---

## Dépannage

| Symptôme | Piste |
|---|---|
| Rien n'apparaît | La config est-elle bien dans `~/.claude/settings.json` (clé `env`) ? Avez-vous relancé `claude` ? |
| Les prompts restent vides | `OTEL_LOG_USER_PROMPTS` doit valoir `1`. Le tableau de bord vous le signale explicitement si des prompts ont eu lieu sans texte reçu. |
| L'IP affichée est `127.0.0.1` | Normal si la machine héberge aussi le moniteur. |
| `Content-Type ... non supporte` dans la console serveur | La machine n'envoie pas en JSON → vérifiez `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`. |
| Une autre machine ne remonte pas | Pare-feu : autorisez le port **4318** en entrée sur la machine centrale (voir [`windows/autoriser-pare-feu.bat`](windows/autoriser-pare-feu.bat)). Testez `http://185.185.82.139:4318/health` depuis l'autre machine. |
| Une machine sur un autre réseau ne remonte pas | Vérifiez que l'adresse donnée à `--collecteur` est bien joignable *depuis cette machine précise* (VPN connecté ? redirection de port active ?) — voir « Adresse du collecteur » plus haut. |
| Les machines sont mélangées | Vérifiez que chaque machine a une étiquette `machine=` **différente**. |
| Compteurs qui semblent gonflés | Vérifiez `...TEMPORALITY_PREFERENCE=delta` sur les machines. |

Vérifier que le serveur répond : ouvrez **http://localhost:4318/health**
(doit afficher `{"status":"ok"}`).
