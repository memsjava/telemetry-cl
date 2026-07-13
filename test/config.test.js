// Tests du configurateur de machine (fusion dans ~/.claude/settings.json).
//
// Lancement : node --test test/   (ou npm test)

import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test, describe } from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  DEFAULT_COLLECTEUR,
  DEFAULT_PORT,
  MANAGED_KEYS,
  buildEnv,
  detectOsUser,
  encodeResourceValue,
  loadSettings,
  mergeEnv,
  removeEnv,
  writeSettings,
} from "../lib/config.js";

const execFileAsync = promisify(execFile);
// fileURLToPath (pas .pathname) : le depot peut vivre sous un chemin avec des
// espaces, que .pathname percent-encoderait (%20) en un chemin invalide.
const BIN_PATH = fileURLToPath(new URL("../bin/configurer-machine.js", import.meta.url));

describe("buildEnv", () => {
  test("l'endpoint est construit a partir du collecteur et du port", () => {
    const env = buildEnv("192.168.1.20", "pc-bureau", { port: 4318, osUser: "eric" });
    assert.equal(env.OTEL_EXPORTER_OTLP_ENDPOINT, "http://192.168.1.20:4318");
  });

  test("port personnalise", () => {
    const env = buildEnv("localhost", "pc", { port: 9999, osUser: "eric" });
    assert.equal(env.OTEL_EXPORTER_OTLP_ENDPOINT, "http://localhost:9999");
  });

  test("machine et utilisateur atterrissent dans OTEL_RESOURCE_ATTRIBUTES", () => {
    const env = buildEnv("localhost", "mac-maison", { osUser: "eric" });
    assert.equal(env.OTEL_RESOURCE_ATTRIBUTES, "machine=mac-maison,user=eric");
  });

  test("l'utilisateur systeme est detecte par defaut si non fourni", () => {
    const env = buildEnv("localhost", "pc");
    assert.match(env.OTEL_RESOURCE_ATTRIBUTES, new RegExp(`user=${detectOsUser()}$`));
  });

  test("les caracteres reserves dans les valeurs sont encodes en pourcent", () => {
    // "=" et "," delimitent la syntaxe : une valeur inhabituelle ne doit pas
    // casser le parsing des attributs suivants.
    const env = buildEnv("localhost", "pc", { osUser: "a,b=c" });
    assert.equal(env.OTEL_RESOURCE_ATTRIBUTES, "machine=pc,user=a%2Cb%3Dc");
  });

  test("le protocole est http/json car le serveur ne lit pas le protobuf", () => {
    assert.equal(buildEnv("h", "m", { osUser: "e" }).OTEL_EXPORTER_OTLP_PROTOCOL, "http/json");
  });

  test("les prompts sont enregistres par defaut", () => {
    assert.equal(buildEnv("h", "m", { osUser: "e" }).OTEL_LOG_USER_PROMPTS, "1");
  });

  test("les prompts peuvent etre desactives", () => {
    const env = buildEnv("h", "m", { osUser: "e", logPrompts: false });
    assert.equal(env.OTEL_LOG_USER_PROMPTS, "0");
  });

  test("les reponses de l'assistant ne sont jamais enregistrees", () => {
    for (const logPrompts of [true, false]) {
      const env = buildEnv("h", "m", { osUser: "e", logPrompts });
      assert.equal(env.OTEL_LOG_ASSISTANT_RESPONSES, "0");
    }
  });

  test("chaque cle geree est produite", () => {
    const env = buildEnv("h", "m", { osUser: "e" });
    assert.deepEqual(Object.keys(env).sort(), [...MANAGED_KEYS].sort());
  });
});

describe("encodeResourceValue", () => {
  test("une valeur normale n'est pas modifiee", () => {
    assert.equal(encodeResourceValue("eric"), "eric");
  });

  test("virgule et egal sont encodes en pourcent", () => {
    assert.equal(encodeResourceValue("a,b"), "a%2Cb");
    assert.equal(encodeResourceValue("a=b"), "a%3Db");
  });

  test("le signe pourcent est encode en premier pour rester reversible", () => {
    assert.equal(encodeResourceValue("100%"), "100%25");
  });
});

describe("detectOsUser", () => {
  test("renvoie une chaine non vide", () => {
    // os.userInfo() depend de l'environnement d'execution : on verifie juste
    // le contrat (chaine non vide), pas une valeur precise.
    assert.ok(detectOsUser());
  });
});

describe("mergeEnv", () => {
  test("cree env quand les reglages sont vides", () => {
    assert.deepEqual(mergeEnv({}, { A: "1" }), { env: { A: "1" } });
  });

  test("conserve les reglages de premier niveau sans rapport", () => {
    const existing = {
      permissions: { allow: ["Bash(git status)"] },
      model: "claude-sonnet-5",
    };
    const got = mergeEnv(existing, { A: "1" });
    assert.deepEqual(got.permissions, { allow: ["Bash(git status)"] });
    assert.equal(got.model, "claude-sonnet-5");
    assert.deepEqual(got.env, { A: "1" });
  });

  test("conserve les variables d'environnement sans rapport", () => {
    const existing = { env: { MON_VAR: "garde-moi" } };
    const got = mergeEnv(existing, { A: "1" });
    assert.deepEqual(got.env, { MON_VAR: "garde-moi", A: "1" });
  });

  test("une reconfiguration ecrase les anciennes valeurs", () => {
    const existing = { env: { OTEL_RESOURCE_ATTRIBUTES: "machine=ancien" } };
    const got = mergeEnv(existing, { OTEL_RESOURCE_ATTRIBUTES: "machine=nouveau" });
    assert.equal(got.env.OTEL_RESOURCE_ATTRIBUTES, "machine=nouveau");
  });

  test("ne modifie pas l'entree", () => {
    const existing = { env: { A: "1" } };
    mergeEnv(existing, { B: "2" });
    assert.deepEqual(existing, { env: { A: "1" } });
  });

  test("rejette un env qui n'est pas un objet", () => {
    assert.throws(() => mergeEnv({ env: "pas-un-objet" }, { A: "1" }));
  });
});

describe("removeEnv", () => {
  test("ne retire que les cles gerees", () => {
    const settings = { env: { OTEL_LOGS_EXPORTER: "otlp", MON_VAR: "garde-moi" } };
    const got = removeEnv(settings);
    assert.deepEqual(got.env, { MON_VAR: "garde-moi" });
  });

  test("supprime entierement env si plus rien n'y reste", () => {
    const settings = { model: "x", env: buildEnv("h", "m", { osUser: "e" }) };
    const got = removeEnv(settings);
    assert.ok(!("env" in got));
    assert.equal(got.model, "x");
  });

  test("ne fait rien si jamais configure", () => {
    assert.deepEqual(removeEnv({ model: "x" }), { model: "x" });
  });

  test("l'aller-retour restaure les reglages d'origine", () => {
    const original = { permissions: { allow: [] }, env: { MON_VAR: "v" } };
    const configured = mergeEnv(original, buildEnv("h", "m", { osUser: "e" }));
    assert.deepEqual(removeEnv(configured), original);
  });
});

describe("settings.json (fichier)", () => {
  let tmpDir;
  let settingsPath;

  test.beforeEach(async () => {
    tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "moniteur-test-"));
    settingsPath = path.join(tmpDir, ".claude", "settings.json");
  });

  test.afterEach(async () => {
    await fs.rm(tmpDir, { recursive: true, force: true });
  });

  test("un fichier absent donne un reglage vide", async () => {
    assert.deepEqual(await loadSettings(settingsPath), {});
  });

  test("un fichier vide donne un reglage vide", async () => {
    await fs.mkdir(path.dirname(settingsPath), { recursive: true });
    await fs.writeFile(settingsPath, "");
    assert.deepEqual(await loadSettings(settingsPath), {});
  });

  test("rejette un tableau JSON", async () => {
    await fs.mkdir(path.dirname(settingsPath), { recursive: true });
    await fs.writeFile(settingsPath, "[1, 2]");
    await assert.rejects(() => loadSettings(settingsPath));
  });

  test("leve une erreur sur du JSON invalide", async () => {
    await fs.mkdir(path.dirname(settingsPath), { recursive: true });
    await fs.writeFile(settingsPath, "{pas du json");
    await assert.rejects(() => loadSettings(settingsPath));
  });

  test("l'ecriture cree le dossier parent", async () => {
    await assert.rejects(() => fs.access(path.dirname(settingsPath)));
    await writeSettings(settingsPath, { env: { A: "1" } });
    assert.deepEqual(JSON.parse(await fs.readFile(settingsPath, "utf8")), { env: { A: "1" } });
  });

  test("l'ecriture sauvegarde le fichier existant", async () => {
    await fs.mkdir(path.dirname(settingsPath), { recursive: true });
    await fs.writeFile(settingsPath, '{"model": "ancien"}');
    const backup = await writeSettings(settingsPath, { model: "nouveau" });
    assert.ok(backup);
    assert.deepEqual(JSON.parse(await fs.readFile(backup, "utf8")), { model: "ancien" });
    assert.deepEqual(JSON.parse(await fs.readFile(settingsPath, "utf8")), { model: "nouveau" });
  });

  test("aucune sauvegarde n'est signalee pour une premiere installation", async () => {
    assert.equal(await writeSettings(settingsPath, { env: {} }), null);
  });

  test("cycle complet : preserve un vrai fichier de reglages", async () => {
    await fs.mkdir(path.dirname(settingsPath), { recursive: true });
    await fs.writeFile(settingsPath, JSON.stringify({
      permissions: { allow: ["Bash(python server.py)"] },
      model: "claude-sonnet-5",
      env: { MON_VAR: "garde-moi" },
    }));
    const before = await loadSettings(settingsPath);

    await writeSettings(settingsPath, mergeEnv(
      before, buildEnv("192.168.1.20", "pc-bureau", { osUser: "eric" }),
    ));
    const after = await loadSettings(settingsPath);

    assert.deepEqual(after.permissions, { allow: ["Bash(python server.py)"] });
    assert.equal(after.model, "claude-sonnet-5");
    assert.equal(after.env.MON_VAR, "garde-moi");
    assert.equal(after.env.OTEL_RESOURCE_ATTRIBUTES, "machine=pc-bureau,user=eric");

    await writeSettings(settingsPath, removeEnv(after));
    assert.deepEqual(await loadSettings(settingsPath), before);
  });
});

describe("le template reste synchronise", () => {
  test("les cles du template correspondent a buildEnv", async () => {
    const templateUrl = new URL("../claude-settings.template.json", import.meta.url);
    const template = JSON.parse(await fs.readFile(templateUrl, "utf8"));
    const env = buildEnv("h", "m", { osUser: "e" });
    assert.deepEqual(Object.keys(template.env).sort(), Object.keys(env).sort());
  });

  test("l'endpoint du template correspond au collecteur par defaut", async () => {
    // Le template est copie a la main : s'il derive du vrai defaut, on ne le
    // saurait pas sans ce test.
    const templateUrl = new URL("../claude-settings.template.json", import.meta.url);
    const template = JSON.parse(await fs.readFile(templateUrl, "utf8"));
    assert.equal(
      template.env.OTEL_EXPORTER_OTLP_ENDPOINT,
      `http://${DEFAULT_COLLECTEUR}:${DEFAULT_PORT}`,
    );
  });
});

describe("valeurs par defaut du CLI (sous-processus)", () => {
  let tmpDir;
  let settingsPath;

  test.beforeEach(async () => {
    tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "moniteur-cli-test-"));
    settingsPath = path.join(tmpDir, "settings.json");
  });

  test.afterEach(async () => {
    await fs.rm(tmpDir, { recursive: true, force: true });
  });

  async function runCli(...args) {
    const { stdout } = await execFileAsync(
      process.execPath,
      [BIN_PATH, "--settings", settingsPath, "--simuler", ...args],
    );
    // Note : split(sep, limit) en JS tronque le TABLEAU a `limit` elements
    // (contrairement a Python) - ne surtout pas passer de limite ici.
    return JSON.parse(stdout.split("deviendrait :")[1]);
  }

  test("le collecteur prend la valeur par defaut sans etre passe", async () => {
    const updated = await runCli();
    assert.equal(
      updated.env.OTEL_EXPORTER_OTLP_ENDPOINT,
      `http://${DEFAULT_COLLECTEUR}:${DEFAULT_PORT}`,
    );
  });

  test("la machine prend le nom d'hote courant sans etre passee", async () => {
    const updated = await runCli();
    const hostname = os.hostname().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    assert.match(updated.env.OTEL_RESOURCE_ATTRIBUTES, new RegExp(`machine=${hostname}(,|$)`));
  });

  test("un collecteur explicite ecrase la valeur par defaut", async () => {
    const updated = await runCli("--collecteur", "localhost");
    assert.equal(updated.env.OTEL_EXPORTER_OTLP_ENDPOINT, `http://localhost:${DEFAULT_PORT}`);
  });
});
