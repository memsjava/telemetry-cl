// Logique de configuration d'une machine pour le Moniteur Claude Code.
// Portage 1:1 de configurer_machine.py : memes cles, memes comportements,
// pour que les deux installeurs (npx et python) produisent un settings.json
// identique. Aucune dependance externe (stdlib Node uniquement).

import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULT_PORT = 4318;

// IP fixe du serveur central : evite de la retaper sur chacune des machines suivies.
export const DEFAULT_COLLECTEUR = "185.185.82.139";

export const DEFAULT_SETTINGS_PATH = path.join(os.homedir(), ".claude", "settings.json");

// Cles posees par ce module : ce sont exactement celles que removeEnv() enleve.
export const MANAGED_KEYS = [
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
];

/** Nom de la session systeme (Windows/Mac/Linux) courante. */
export function detectOsUser() {
  try {
    return os.userInfo().username;
  } catch {
    return "inconnu";
  }
}

/**
 * Encode une valeur pour OTEL_RESOURCE_ATTRIBUTES (format cle=valeur,cle=valeur).
 * Le "=" et la "," delimitent la syntaxe : s'ils apparaissent dans une valeur
 * (nom d'utilisateur ou de machine inhabituel), on les encode en pourcent pour
 * ne pas casser le parsing des attributs suivants.
 */
export function encodeResourceValue(value) {
  return value.replace(/%/g, "%25").replace(/,/g, "%2C").replace(/=/g, "%3D");
}

/** Variables a poser pour que Claude Code emette vers le Moniteur. */
export function buildEnv(collecteur, machine, options = {}) {
  const { port = DEFAULT_PORT, logPrompts = true, osUser } = options;
  const user = osUser ?? detectOsUser();
  const resourceAttrs =
    `machine=${encodeResourceValue(machine)},user=${encodeResourceValue(user)}`;
  return {
    CLAUDE_CODE_ENABLE_TELEMETRY: "1",
    OTEL_METRICS_EXPORTER: "otlp",
    OTEL_LOGS_EXPORTER: "otlp",
    // http/json est obligatoire : le serveur ne lit pas le protobuf.
    OTEL_EXPORTER_OTLP_PROTOCOL: "http/json",
    OTEL_EXPORTER_OTLP_ENDPOINT: `http://${collecteur}:${port}`,
    OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE: "delta",
    OTEL_METRIC_EXPORT_INTERVAL: "10000",
    OTEL_LOGS_EXPORT_INTERVAL: "5000",
    OTEL_RESOURCE_ATTRIBUTES: resourceAttrs,
    OTEL_LOG_USER_PROMPTS: logPrompts ? "1" : "0",
    // Les reponses de Claude ne nous interessent pas : volume + confidentialite.
    OTEL_LOG_ASSISTANT_RESPONSES: "0",
  };
}

/** Lit settings.json. Un fichier absent ou vide donne un reglage vide. */
export async function loadSettings(filePath) {
  let raw;
  try {
    raw = await fs.readFile(filePath, "utf8");
  } catch (err) {
    if (err.code === "ENOENT") return {};
    throw err;
  }
  const trimmed = raw.trim();
  if (!trimmed) return {};
  const settings = JSON.parse(trimmed);
  if (typeof settings !== "object" || settings === null || Array.isArray(settings)) {
    throw new Error(`${filePath} ne contient pas un objet JSON`);
  }
  return settings;
}

/** Fusionne env dans settings.env sans toucher au reste du fichier. */
export function mergeEnv(settings, env) {
  const merged = { ...settings };
  const current = merged.env;
  if (current !== undefined
      && (typeof current !== "object" || current === null || Array.isArray(current))) {
    throw new Error('la cle "env" de settings.json n\'est pas un objet');
  }
  merged.env = { ...(current ?? {}), ...env };
  return merged;
}

/** Retire les cles posees par ce module, et "env" s'il devient vide. */
export function removeEnv(settings) {
  const merged = { ...settings };
  const current = merged.env;
  if (typeof current !== "object" || current === null || Array.isArray(current)) {
    return merged;
  }
  const kept = Object.fromEntries(
    Object.entries(current).filter(([key]) => !MANAGED_KEYS.includes(key)),
  );
  if (Object.keys(kept).length > 0) {
    merged.env = kept;
  } else {
    delete merged.env;
  }
  return merged;
}

/** Ecrit settings.json apres avoir sauvegarde l'existant. Renvoie le chemin du .bak. */
export async function writeSettings(filePath, settings) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  let backupPath = null;
  try {
    await fs.access(filePath);
    backupPath = `${filePath}.bak`;
    await fs.copyFile(filePath, backupPath);
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }
  await fs.writeFile(filePath, JSON.stringify(settings, null, 2) + "\n", "utf8");
  return backupPath;
}
