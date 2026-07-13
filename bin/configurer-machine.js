#!/usr/bin/env node
// Configure CETTE machine pour qu'elle envoie sa telemetrie Claude Code au Moniteur.
//
// Ecrit les variables OTEL dans la cle "env" de ~/.claude/settings.json, que Claude
// Code applique a *toutes* ses sessions. Aucune variable d'environnement systeme,
// aucun profil shell modifie, aucun terminal a rouvrir.
//
// Le collecteur (--collecteur) a une valeur par defaut (DEFAULT_COLLECTEUR,
// IP fixe du serveur central) : inutile de la retaper sur chaque machine
// suivie. --machine a lui aussi une valeur par defaut (nom d'hote courant).
//
// Utilisation :
//   npx github:memsjava/telemetry-cl                     (machine = hostname, collecteur = defaut)
//   npx github:memsjava/telemetry-cl --machine pc-bureau  (etiquette explicite)
//   npx github:memsjava/telemetry-cl --collecteur localhost --machine pc-bureau  (autre collecteur)
//   npx github:memsjava/telemetry-cl --utilisateur eric
//   npx github:memsjava/telemetry-cl --sans-prompts
//   npx github:memsjava/telemetry-cl --retirer                         (annule la config)
//   npx github:memsjava/telemetry-cl --simuler                         (aucune ecriture)

import os from "node:os";

import {
  DEFAULT_COLLECTEUR,
  DEFAULT_PORT,
  DEFAULT_SETTINGS_PATH,
  buildEnv,
  detectOsUser,
  loadSettings,
  mergeEnv,
  removeEnv,
  writeSettings,
} from "../lib/config.js";

const HELP = `Configure cette machine pour le Moniteur Claude Code.

Options :
  --collecteur, -c <hote>    IP ou nom de la machine qui heberge server.py
                             (defaut : ${DEFAULT_COLLECTEUR})
  --machine, -m <nom>        Etiquette de cette machine dans le tableau de
                             bord (defaut : nom d'hote)
  --utilisateur, -u <nom>    Nom d'utilisateur affiche (defaut : utilisateur
                             systeme courant)
  --port, -p <port>          Port du collecteur (defaut : ${DEFAULT_PORT})
  --sans-prompts             Ne pas enregistrer le texte des prompts
  --retirer                  Retire la configuration du Moniteur de settings.json
  --simuler                  Affiche le resultat sans rien ecrire
  --settings <chemin>        Chemin de settings.json (defaut : ${DEFAULT_SETTINGS_PATH})
  --help, -h                 Affiche cette aide
`;

function parseArgs(argv) {
  const args = {
    collecteur: DEFAULT_COLLECTEUR,
    machine: os.hostname(),
    utilisateur: null,
    port: DEFAULT_PORT,
    sansPrompts: false,
    retirer: false,
    simuler: false,
    settings: DEFAULT_SETTINGS_PATH,
    help: false,
  };

  const takeValue = (name, value) => {
    if (value === undefined) {
      throw new Error(`${name} attend une valeur`);
    }
    return value;
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    switch (arg) {
      case "--collecteur":
      case "-c":
        args.collecteur = takeValue(arg, argv[++i]);
        break;
      case "--machine":
      case "-m":
        args.machine = takeValue(arg, argv[++i]);
        break;
      case "--utilisateur":
      case "-u":
        args.utilisateur = takeValue(arg, argv[++i]);
        break;
      case "--port":
      case "-p":
        args.port = Number.parseInt(takeValue(arg, argv[++i]), 10);
        break;
      case "--sans-prompts":
        args.sansPrompts = true;
        break;
      case "--retirer":
        args.retirer = true;
        break;
      case "--simuler":
        args.simuler = true;
        break;
      case "--settings":
        args.settings = takeValue(arg, argv[++i]);
        break;
      case "--help":
      case "-h":
        args.help = true;
        break;
      default:
        throw new Error(`Option inconnue : ${arg}`);
    }
  }
  return args;
}

async function main(argv) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (err) {
    console.error(`Erreur : ${err.message}\n`);
    console.error(HELP);
    return 1;
  }

  if (args.help) {
    console.log(HELP);
    return 0;
  }

  if (!args.retirer && !args.collecteur) {
    console.error("Erreur : --collecteur est requis (ou utilisez --retirer)\n");
    console.error(HELP);
    return 1;
  }

  let settings;
  try {
    settings = await loadSettings(args.settings);
  } catch (err) {
    console.error(`Erreur : ${args.settings} est illisible (${err.message})`);
    console.error("Corrigez ou supprimez ce fichier, puis relancez.");
    return 1;
  }

  let updated;
  let action;
  let details = "";

  if (args.retirer) {
    updated = removeEnv(settings);
    action = "retiree de";
  } else {
    const osUser = args.utilisateur ?? detectOsUser();
    const env = buildEnv(args.collecteur, args.machine, {
      port: args.port,
      logPrompts: !args.sansPrompts,
      osUser,
    });
    updated = mergeEnv(settings, env);
    action = "ecrite dans";
    details = `  machine     : ${args.machine}\n`
      + `  utilisateur : ${osUser}\n`
      + `  endpoint    : ${env.OTEL_EXPORTER_OTLP_ENDPOINT}\n`
      + `  prompts     : ${args.sansPrompts ? "non enregistres" : "enregistres"}\n`;
  }

  if (args.simuler) {
    console.log(`[simulation] ${args.settings} deviendrait :\n`);
    console.log(JSON.stringify(updated, null, 2));
    return 0;
  }

  let backup;
  try {
    backup = await writeSettings(args.settings, updated);
  } catch (err) {
    console.error(`Erreur d'ecriture sur ${args.settings} : ${err.message}`);
    return 1;
  }

  console.log(`OK - configuration ${action} ${args.settings}`);
  if (details) process.stdout.write(details);
  if (backup) console.log(`  (sauvegarde de l'ancien fichier : ${backup})`);
  if (!args.retirer) {
    const preserved = Object.keys(settings).filter((k) => k !== "env");
    if (preserved.length) console.log(`  reglages conserves : ${preserved.join(", ")}`);
    console.log("\nLancez 'claude' : la telemetrie part des la prochaine session.");
  }
  return 0;
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (err) => {
    console.error(`Erreur inattendue : ${err.stack || err}`);
    process.exit(1);
  },
);
