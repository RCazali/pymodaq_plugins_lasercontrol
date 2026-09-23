# Laser Control — extension PyMoDAQ

Extension de dashboard PyMoDAQ pour stabiliser un laser thin-disk à verrouillage de modes avec
génération d'XUV intracavité. Elle remplace les anciennes extensions (LaserController, XY Stage
Controller…) par **une seule extension** (décision D2 du cahier des charges), organisée en couches :
un cœur qui arbitre tout accès au matériel, des engines en logique pure, et une interface explicite.

- **Barre d'état toujours visible** : état global, un bouton par automatisme, fraîcheur des données
  de l'ordi B, intensité par rapport au seuil, ML ON / OFF / Instable, Note, Shake, REC, ARRÊT D'URGENCE.
- **Onglets** : Vue d'ensemble (stabilisation en un clic, une carte par moteur avec propriétaire et
  enveloppe), Alignement (jog clavier, historique), XY, PID (plusieurs PID), Optimisation,
  Scan 1D, Enregistrement, Presets, Configuration.
- **Sécurité** : les invariants S1–S12 sont appliqués par le cœur, pas par l'interface, et chacun a
  son test automatique.
- **Enregistrement** HDF5 (snapshots périodiques, rafales autour du shake, notes, état ML).
- **Presets** versionnés, écrits avec des **rôles** (OC_H, Cam1_x…) : un même preset se charge au
  bureau et au labo, seul le fichier de rôles de la machine change.
- **Simulateur LaserSim** (plugins moteur et détecteur 0D) pour travailler au bureau.

![Vue d'ensemble avec le simulateur : stabilisation active sur deux PID](docs/images/overview.png)

## Compatibilité

- PyMoDAQ **5.1.x** (développé sur 5.1.10) et **5.2.x**. Testé avec pymodaq 5.1.10 (pymodaq_gui 5.0.32
  et 5.1.12), pymodaq 5.2.11, Python 3.11, PyQt5. L'interface passe par qtpy (PyQt6 et PySide6
  devraient fonctionner, non testés).
- ⚠ pymodaq 5.1.10 ne démarre pas avec pymodaq_gui / pymodaq_data / pymodaq_utils 5.2
  (ImportError). Ne mettez pas ces paquets à jour séparément de pymodaq.

## Installation

Dans l'environnement PyMoDAQ :

```bash
cd pymodaq_plugins_lasercontrol
pip install -e .
```

La version vient des tags git (`hatch-vcs`, 0.1.0 sans dépôt git). L'extension apparaît dans le
menu *Extensions* du dashboard sous le nom **Laser Control**. Les plugins `LaserSim` (moteur et
détecteur 0D) apparaissent dans les listes de plugins.

Tests (≈ 1 min, Qt hors écran) : `pip install pytest` puis `pytest`. Les tests du dashboard écrivent
puis suppriment un preset `lc_pytest_dashboard.xml` dans le dossier de presets de PyMoDAQ.

## Démarrage au bureau (simulateur)

```bash
python -m pymodaq_plugins_lasercontrol.tools.sim_preset
python -m pymodaq_plugins_lasercontrol.extensions.laser_control laser_sim
```

La première commande écrit le preset de dashboard `laser_sim` (une *experiment* en PyMoDAQ 5.2) avec six
moteurs simulés et le détecteur `SimB`, qui joue l'ordi B. Elle écrit aussi le fichier de rôles du
simulateur si cette machine n'en a pas encore, avec les mêmes noms de rôles que le modèle du labo
(`docs/exemples/roles_lab.toml`). La seconde ouvre le dashboard avec ce preset et l'extension. On peut
aussi ouvrir le dashboard comme d'habitude, charger `laser_sim`, puis *Extensions → Laser Control*.

Ensuite :

1. Barre d'état : **▶ Acquisition** lance l'acquisition continue de `SimB`.
2. **Configuration → Sécurité** : pour chaque moteur piloté automatiquement, renseigner *Pas max*,
   *Excursion max* et *Débit max* (obligatoires), et la garde intensité (rôle `Laser_power`).
3. **PID → Ajouter un PID** : signal `Cam1_x`, moteur `OC_H`, gains (voir `docs/reglage_pid.md`).
4. **Vue d'ensemble → Activer la stabilisation**.
5. **Presets → Enregistrer sous…**

Pour simuler des pannes, utilisez les réglages du viewer `SimB` dans le dashboard : bruit, dérive,
chutes d'intensité, *muted* (ordi B muet) et *frozen* (valeurs figées).

## Mise en service au labo (ordi A)

Ordre de démarrage (T2.4) : coordinateur LECO → ordi B (caméras + extension 0D) → dashboard unique de
l'ordi A → extension Laser Control.

1. **`config_lasercontrol.toml`** : il est créé au premier lancement, à côté de `config_pymodaq.toml`,
   et reste propre à chaque machine, hors git. Y régler `[machine] name = "lab"`, les dossiers,
   l'adresse du Red Pitaya (`[shaker]`) et le dossier partagé avec l'ordi B (`event_trigger_folder`, O8).
2. **Rôles** : dans **Configuration → Rôles**, cliquer « Créer les rôles manquants », puis donner aux
   rôles les mêmes noms qu'au bureau (modèle : `docs/exemples/roles_lab.toml`), avec l'unité et le sens
   de chaque moteur. Sous « Compteurs de trames », choisir pour l'ordi B son canal compteur de trames (O9).
3. **Anciens profils** : **Presets → Importer un ancien profil…** accepte les sessions LaserController,
   les profils PID et optimiseur, et les profils XY. Les moteurs et canaux deviennent des rôles. Les
   limites `max_step`, `max_excursion` et débit ne sont pas inventées : elles restent à renseigner.
4. **Presets → Vérifier sur cette machine**, essai en CW, puis **Marquer comme validé…**

Script de lancement unique : `python -m pymodaq_plugins_lasercontrol.extensions.laser_control <preset>`.

## Où sont les fichiers

| Fichier | Emplacement par défaut | Contenu |
|---|---|---|
| `config_lasercontrol.toml` | dossier de configuration PyMoDAQ | réglages de la machine (nom, dossiers, shaker…) |
| `roles_<machine>.toml` | `~/.pymodaq/laser_control/` | rôles ↔ modules / canaux de cette machine |
| `presets/*.json` | `~/.pymodaq/laser_control/presets/` | presets (PID, optimiseur, sécurité, alignement, XY, scan, enregistrement) |
| `recordings/AAAA-MM-JJ/session_*.h5` | `~/.pymodaq/laser_control/recordings/` | enregistrements HDF5 |
| `session_<machine>.json` | `~/.pymodaq/laser_control/` | dernier état de l'interface, restauré sans rien démarrer (T6.6) |
| `alignment_history_<machine>.json` | `~/.pymodaq/laser_control/` | historique des positions de l'onglet Alignement |
| `logs/laser_control.log` | `~/.pymodaq/laser_control/logs/` | journal technique (rotatif, messages dédoublonnés) |

Le dossier `presets` peut pointer vers un dossier partagé ou versionné, pour utiliser les mêmes
presets sur les deux machines (`[paths] presets_folder`). Lecture d'une session :
`from pymodaq_plugins_lasercontrol.recording import load_session`.

## Organisation du code

```
src/pymodaq_plugins_lasercontrol/
├── core/          broker (seul accès au matériel), ownership, signals (fraîcheur, heartbeat),
│                  safety (S1–S5), states, roles, events, coordinator (LaserCore), automation
├── engines/       pid, scan (1D, serpentin XY), optimizer (interface enfichable + 4 algorithmes)
├── hardware/      ports PyMoDAQ (DAQ_Move / DAQ_Viewer), simulateur, shaker Red Pitaya
├── recording/     snapshots, écriture HDF5 dans un thread, rafales, fichiers déclencheurs (O8), lecture
├── presets/       schéma versionné, stockage, validation, import des anciens profils
├── ui/            barre d'état, onglets, journal
├── extensions/    laser_control.py : la classe d'extension PyMoDAQ
├── daq_move_plugins/, daq_viewer_plugins/plugins_0D/   plugins LaserSim
└── tools/         sim_preset.py : preset du simulateur
```

Le cœur et les engines ne dépendent ni de Qt ni de PyMoDAQ. Ils sont testés avec une horloge et du
matériel simulés (`core/fakes.py`), et l'interface ne passe jamais par `modules_manager`. Détails
dans `docs/conception.md`.

## Documentation

- `docs/guide_utilisateur.md` : démarrer, charger un preset, activer la stabilisation, que faire si c'est rouge (T11.1).
- `docs/fiche_incident.md` : plantage, ordi B muet, intensité à 0, arrêt d'urgence (T11.2).
- `docs/conception.md` : architecture, invariants et tests associés, ajouter un moteur, un détecteur ou un engine (T11.3).
- `docs/reglage_pid.md` : signes, gains, poids, enveloppes (T11.4).
- `CHANGELOG.md`.

## Avancement par rapport au cahier des charges

| Lot | État |
|---|---|
| L0 | Remplacé par la refonte : chaque correctif est couvert par le nouveau cœur (arrêt d'urgence par `stop_motion`, pause et reprise des PID, NaN au lieu de 0.0, garde intensité, plus de reconnexion des détecteurs, rien de bloquant). |
| L1 | Fait : structure, rôles, simulateur, 68 tests, version et hash git dans l'UI et dans les enregistrements, journal rotatif. À faire de votre côté : branches et tags (T1.5), NTP (T1.7). Le simulateur est à recaler sur des mesures réelles (T9.1). |
| L2 | Alignement et XY sont dans l'extension, derrière la propriété des moteurs (T2.3). L'inventaire du matériel, le preset unique du labo et le retrait de l'ancien dashboard restent à faire au labo. |
| L3, L4 | Faits : broker, fraîcheur et heartbeat, propriété (D4 / D5 / O3-A), superviseur S1–S5, machine d'états, bus d'événements, arrêt d'urgence ; PID multiples, moniteur passif, scan 1D, interface optimiseur avec les 4 modes historiques. |
| L5 | Fait. La lisibilité à distance et sur un petit écran reste à vérifier sur place (T5.9, T8.2). |
| L6 | Fait, sauf les différences entre presets (T6.5, P3). |
| L7 | Fait côté ordi A. Côté ordi B (T7.6) : lire les fichiers déclencheurs et sauvegarder les images. |
| L9 | Chargeur de sessions (T9.2). Le reste est à faire. |
| L10 | Seule l'interface est prête (`register_algorithm`). |
| L11 | Guide, fiche incident, conception, réglage PID. |
| L12 | Invariants, arbitrages et une partie des pannes testés en simulation. Endurance, CW, ML et test « utilisateur non-dev » : au labo. |

## Points à vérifier au labo

- **Heartbeat (O9)** : sans canal compteur de trames, la fraîcheur repose sur l'heure de réception
  (option O9-B). Une valeur figée mais toujours émise n'est alors pas détectée. Il faut ajouter le
  compteur côté ordi B.
- **Unités des moteurs** : les limites de sécurité sont dans l'unité du module PyMoDAQ (pas de
  picomoteur, mm…). Renseigner aussi la *Résolution* (plus petit pas significatif).
- **Mode de jog `limit`** (Alignement) : un `move_abs` vers Min/Max peut continuer sur le contrôleur si
  PyMoDAQ plante (S10). Préférez `step`, ou `native` si le plugin du moteur implémente `jog()`.
- **Pics isolés** : aucun filtre de valeurs aberrantes. Un pic produit au plus un pas borné (S3), dans
  l'enveloppe (S4). Une bande morte et une période de 0.5 s en limitent l'effet.
- **Shake** : le script Red Pitaya `redpitaya_scpi.py` doit être importable, ou copié dans
  `~/.pymodaq/laser_control/`.
- **PyMoDAQ 5.2** : la liste des modules se relit quand l'expérience change. Arrêtez les automatismes avant.

Licence : MIT (voir `LICENSE`).
