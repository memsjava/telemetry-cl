# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Moniteur Claude Code" — a self-hosted, single-machine collector + dashboard for Claude Code's own OpenTelemetry
telemetry, aggregated **per machine** across a fleet (the user runs ~7 PCs on one shared Claude Pro account and
wants to know which machine — and which person — burns what). Tracks tokens/cost, sessions, tool acceptance,
commits/PRs/LOC, plus each machine's **IP address**, **OS username**, and the **text of its prompts**. Each
tracked machine just points OTLP at this collector; there is no auth token to manage.

## Running

```bash
python server.py                        # listens on 0.0.0.0:4318
python server.py --port 4319 --host 127.0.0.1

python -m unittest discover -p "test_*.py"             # full suite (97 tests), run from repo root
python -m unittest tests.test_server.TestPrompts -v            # one class
python -m unittest tests.test_server.TestPrompts.test_search   # one test
```

No build step for the server, no package manager, no `requirements.txt` — `server.py` and `tests/test_server.py`
are stdlib-only (Python 3.8+) by design (see `server.py`'s module docstring). Do not add external Python
dependencies (e.g. `flask`, `fastapi`, `requests`, `pytest`); that would break the "zero-dependency" property that
half of this project is built around. `tests/` has an `__init__.py` (so the dotted `tests.test_server...` form
above works) but is otherwise a plain folder — always invoke from the **repo root**, since `tests/test_server.py`
does `import server` (top-level module, repo root), `tests/test_configurer.py` does
`from cli import configurer_machine` (needs `cli/` importable as a package, hence `cli/__init__.py`), and that
same file reads `claude-settings.template.json` via a path relative to CWD, not to the test file.

The Node package (`lib/`, `bin/`, `test/` at repo root — see below) is the other half and follows the same ethos:
zero npm dependencies, `node --test` instead of Jest/Mocha. Note the deliberate naming split: Python's tests live
in `tests/` (plural, with `__init__.py`), Node's in `test/` (singular, no package concept) — matching each
ecosystem's own convention rather than picking one name for both.

```bash
node --test                              # Node suite (33 tests) — auto-discovers test/*.test.js
```

There is no linter or CI config. The repo lives at `git@github.com:memsjava/telemetry-cl.git` — confirm with the
user before force-pushing, rewriting history, or changing repo visibility; those remain real, hard-to-reverse
actions even though the repo itself now exists.

## Configuring a tracked machine

Two installers exist, kept in lockstep (same env vars, same CLI flags, same `settings.json` output — verified by
tests on both sides, e.g. `TestTemplateStaysInSync` / `"le template reste synchronise"` both assert against
`claude-settings.template.json`):

- **`cli/configurer_machine.py`** (Python, stdlib-only) — run directly if Python's already on the machine. Lives
  in `cli/` (with an `__init__.py`) purely for tidiness; unlike the Node package below it has no constraint
  forcing repo-root placement, since it's invoked directly (`python cli/configurer_machine.py`), never via `npx`.
- **`bin/configurer-machine.js`** + **`lib/config.js`** (Node, zero npm deps) — meant to run via
  `npx github:memsjava/telemetry-cl`, so a tracked PC needs no local clone, no Python. This is why the Node
  **package.json lives at the repo root** rather than in a subfolder: `npm`/`npx` only reliably honours a
  `#path:subdir` fragment for git-based installs since npm ≥ 11.10.0 (Feb 2026) — putting the package below repo
  root would silently break on any older npm, which is likely across 7 different machines. Don't move it into a
  subdirectory without re-checking that constraint.

Both **merge** the OTEL vars into the `env` key of `~/.claude/settings.json` (Claude Code applies that to every
session). They preserve existing settings and write a `.bak`; `--retirer` cleanly reverses it.
`claude-settings.template.json` is the same block for hand-copying, shared by both installers (and asserted
identical to `buildEnv()`/`build_env()`'s output keys by both test suites).

Both auto-detect the OS username (`detect_os_user()` / `detectOsUser()`, wrapping `getpass.getuser()` /
`os.userInfo().username`) and pack it into `OTEL_RESOURCE_ATTRIBUTES` alongside `machine=`, e.g.
`machine=pc-bureau,user=eric,dp=jean,compte=GroupeAI1` — override with `--utilisateur`. Claude Code does **not**
expose the OS login name natively (only `user.email`, identical for everyone on a shared Pro/Max account), so this
is a value *we* inject, not something OTel/Claude Code provides. Same story for `dp=` (directeur de projet) and
`compte=` (shared Claude account name): when run in a real terminal both installers prompt interactively for
user/dp/compte (Enter keeps the proposed default — the already-configured value, parsed back out of the existing
`OTEL_RESOURCE_ATTRIBUTES` by `parse_resource_attrs` / `parseResourceAttributes`), `--dp`/`--compte` skip the
corresponding question, and with no TTY (scripted runs, the test suites) nothing is asked and existing values are
preserved (`resoudre_identite` / `resoudreIdentite` holds that precedence logic on both sides). Empty `dp`/`compte`
are omitted from the attrs string, not written as `dp=`. Values are percent-encoded (`_encode_resource_value` /
`encodeResourceValue`) since `,`/`=` are the `OTEL_RESOURCE_ATTRIBUTES` delimiters.

A third, older approach (shell-profile / Windows user env vars instead of `settings.json`) existed as
`configurer-machine.sh` / `configurer-machine.ps1` / `2-configurer-cette-machine.bat` and was removed — fully
superseded by the two installers above, and it could conflict with them if reintroduced. Don't recreate it.

**A global hook cannot replace the OTel path**: no hook payload carries `input_tokens`, `output_tokens`, or
`cost_usd` — token and cost data exist only in OpenTelemetry. This has been asked once already; don't re-propose
hooks as a telemetry substitute.

Other Windows entry points live in `windows/` (double-click wrappers, no logic of their own):
`0-DEMARRER-TOUT.bat` (server + browser), `1-demarrer-moniteur.bat` (server alone — `cd`s up one level to reach
`server.py` at repo root, since it lives in `windows/`), `2-ouvrir-tableau-de-bord.bat`, `autoriser-pare-feu.bat`
(opens TCP 4318 inbound, needs admin). If you add another `.bat` here that shells out to a repo-root file, remember
it needs the same `cd /d "%~dp0.."` — `%~dp0` alone now points at `windows/`, not the repo root.

## Architecture

Everything funnels through **one Python file**, `server.py`, split into clear stages:

1. **Ingestion** (`ingest_metrics`, `ingest_logs`) — receives OTLP/HTTP **JSON** (not protobuf — this is
   enforced; the client-side scripts all set `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` specifically because this
   server only parses JSON) at `POST /v1/metrics` and `POST /v1/logs`. `POST /v1/traces` is accepted and
   discarded (compat only — Claude Code's telemetry doesn't need traces here).
   - OTLP payloads are deeply nested (`resourceMetrics[].scopeMetrics[].metrics[].sum|gauge.dataPoints[]` for
     metrics; `resourceLogs[].scopeLogs[].logRecords[]` for logs/events). `attrs_to_dict` /
     `_attr_value` flatten OTLP's typed `AnyValue` attribute lists into plain dicts before use.
   - `resolve_machine()` is the important bit of business logic here: it decides which machine a data point
     belongs to, checking point-level then resource-level attributes (`machine` → `host.name` →
     `service.instance.id`), falling back to `user.email`/`terminal.type` if no explicit label was set. Any
     change to how machines are identified/labeled goes through this function.
   - Rows land in two SQLite tables: `events` (per-occurrence records like `api_request`, `tool_decision`,
     `user_prompt`, with tokens/cost/session id/prompt text) and `metrics` (cumulative counters like lines of
     code, commits, PRs, session count, active time). This split mirrors OTel's own logs-vs-metrics distinction
     and is why aggregation queries below touch both tables separately.
   - **IP is not an OTel attribute** — it's read server-side off the TCP connection (`Handler.client_ip()`,
     honouring `X-Forwarded-For`) and threaded into `ingest_metrics`/`ingest_logs` as a parameter.
   - **OS username is a `user` resource attribute we made up** — `resolve_user()` reads it the same way
     `resolve_machine()` reads `machine`, but with no fallback chain (empty string if absent). It only appears
     because `cli/configurer_machine.py` (or the Node installer) puts it there; nothing upstream (Claude Code,
     OTel) provides it.
   - **Prompt text only arrives if the machine sets `OTEL_LOG_USER_PROMPTS=1`**; it lands in the `prompt`
     attribute of the `user_prompt` event. Without it you get a prompt *count* but no text — the dashboard
     detects exactly this case (`prompts > 0 && prompts_logged == 0`) and tells the user which var to set.
   - `_migrate_columns()` runs on every `init_db()` and `ALTER TABLE`s in any missing column, so a pre-existing
     `data/telemetry.db` survives schema additions. Add new columns there, not just in the `CREATE TABLE`.
     `init_db()` also `os.makedirs`'s `data/` first, since a fresh clone won't have it (it's `.gitignore`d).

2. **Aggregation** (`get_stats`) — the single function backing `GET /api/stats?days=N`. It queries `events` and
   `metrics` independently and merges results into a per-machine dict (plus a synthesized `__TOTAL__` row
   summed across machines), covering: token/cost totals, tool accept/reject counts, per-model breakdown,
   commits/PRs/LOC, IPs and OS users seen per machine (both `ip`/`user` = most recent, `ips`/`users` = full
   list — same pattern, copy one to add the other), a daily cost/token timeline, and a recent-activity feed
   (last 60 events). If you add a new stat, it almost certainly belongs as another `cur.execute(...)` block
   here, following the existing `machine → accumulate into machines[name]` pattern, then folded into `totals`
   in the finalization loop at the bottom (which also has to `.pop()` any new machine-only key).

   `get_prompts` backs `GET /api/prompts?days=&machine=&q=&limit=` — the prompt log, kept out of `/api/stats`
   so the dashboard's 30 s poll doesn't ship every prompt body each time. Its `q` search escapes `%`/`_` so
   LIKE metacharacters stay literal.

3. **HTTP layer** (`Handler(BaseHTTPRequestHandler)` + `ThreadingHTTPServer`) — stdlib `http.server`, no
   framework. Routes are hand-dispatched by exact path string in `do_GET`/`do_POST`. `web/dashboard.html` is
   served as a static file at `/` and `/index.html` (path in `DASHBOARD_PATH`); it's a single-file vanilla-JS +
   Chart.js (via CDN) frontend that polls `/api/stats` and renders cards/tables/charts entirely client-side —
   there's no server-side templating.

All DB access goes through one shared `sqlite3` connection (`_conn`) guarded by a single `threading.Lock`
(`_db_lock`), since `ThreadingHTTPServer` handles requests concurrently on separate threads. Keep any new
DB-touching code inside that lock.

## Data model notes

- `data/telemetry.db` (SQLite, WAL mode) is a local, `.gitignore`d data file — treat it as generated state, not
  source. `telemetry.db-shm`/`-wal` (same folder) are WAL side files. It contains real prompt text and IPs:
  never remove it from `.gitignore` or otherwise let it land in a commit, especially once this repo is pushed to
  GitHub. `.gitignore` matches it by bare filename (no `data/` prefix), so it still applies after the move.
- Timestamps are stored as Unix ms (`ts_ms`) converted from OTLP's nanosecond `timeUnixNano`; `day` is a
  precomputed `YYYY-MM-DD` (UTC) string used for the dashboard's day-range filters (24h/7d/30d/all → `?days=N`).
- Token/decision/tool-name fields are read straight from OTel attributes and are best-effort — client-side
  Claude Code telemetry schema changes could add/rename attributes, so ingestion code favors permissive
  `.get()` lookups with defaults over strict schema validation.
