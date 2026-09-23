# Changelog

Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/). Versions : tags git (`hatch-vcs`).

## [0.2.0] — 2026-09-23

L'onglet Alignement devient le poste de travail principal.

### Ajouté
- **Boutons d'automatismes dans l'onglet Alignement**, sous les moteurs : un par PID ou optimisation
  marqué « Bouton dans Alignement ». Un clic démarre, un autre arrête, un clic sur un automatisme gelé
  le reprend, sans confirmation. Clic droit : reprendre, arrêter, ouvrir les réglages. Un refus
  (limites manquantes, arrêt d'urgence…) s'affiche sous les boutons et dans le journal, sans boîte de
  dialogue.
- **Plusieurs optimisations nommées** (onglet Optimisation : ajouter, dupliquer, renommer, supprimer),
  chacune avec son nom court et l'action de son bouton (une passe, périodique, complète).
- Simulateur : deux bancs de test, pointé de faisceau (`Steer_H/V` → `Beam_x/y`) et photodiode
  (`PD_X/Y` → `PD_current`). `tools.sim_preset` écrit aussi le preset Laser Control
  « Démo simulateur » (PID et optimisations avec bouton, limites de sécurité, axes d'alignement).

### Modifié
- Presets : schéma 2 (`optimizers`, liste). Un preset du schéma 1 est converti au chargement : son
  optimiseur devient l'optimisation « Optimiseur ».
- La barre d'état ne montre plus que les automatismes engagés.
- `tools.sim_preset` complète le fichier de rôles existant (sans modifier les rôles présents).
- Simulateur : chutes d'intensité aléatoires désactivées par défaut (réglage du viewer `SimB`).
- Guide PID : partir de Ki = 0 (le moteur intègre déjà les pas).

## [0.1.0] — 2026-09-23

Première version : une seule extension remplace LaserController, XY Stage Controller et leurs
onglets, conformément au cahier des charges (D1–D9, options O2, O3, O4, O6, O8, O9 et O10 en A).

### Ajouté
- Cœur événementiel dans le thread GUI (O2-A) : broker matériel, source de signaux avec fraîcheur et
  compteur de trames (O9-A), propriété des moteurs (D4 : le dernier activé prend la main, l'autre en
  pause, O3-A ; D5 : action humaine = arrêt), superviseur de sécurité S1–S5, machine d'états, bus
  d'événements, arrêt d'urgence verrouillé (S11).
- Engines : plusieurs PID simultanés (anti-windup, bande morte, pas de coup de dérivée, confirmation
  des mouvements, consigne capturée), optimiseur périodique enfichable avec les 4 modes historiques
  (Joint, Sequential, Nelder-Mead, Gradient/Newton-lite) et l'optimisation complète, scan 1D, scan
  serpentin XY, moniteur passif.
- Interface : barre d'état toujours visible, Vue d'ensemble (stabilisation en un clic, tout ou rien,
  cartes moteurs avec propriétaire et enveloppe), Alignement (jog clavier step / native / limit,
  historique cliquable), XY (groupes, positions mémorisées, carte), PID, Optimisation, Scan 1D,
  Enregistrement, Presets, Configuration (rôles, heartbeat, sécurité, détecteurs, dossiers).
- Rôles logiques par machine (`roles_<machine>.toml`) : les presets sont portables entre bureau et labo.
- Presets versionnés (brouillon / validé, empreinte du contenu, validation par machine), session
  restaurée sans rien démarrer, import des anciens profils (sessions, PID, optimiseur, XY).
- Enregistrement HDF5 (O4-A) : snapshots périodiques de tous les rôles et automatismes, rafales sur
  événement avec tampon avant événement, notes, état ML (O6-A), rotation, lecture `load_session`,
  fichiers déclencheurs pour l'ordi B (O8-A).
- Simulateur LaserSim (plugins moteur et détecteur 0D : dérive, bruit, couplages, chutes d'intensité,
  ordi B muet ou figé) et outil `tools/sim_preset` (preset / experiment et rôles du simulateur).
- Shaker Red Pitaya non bloquant.
- 68 tests : invariants S1–S12, engines, presets, enregistrement, vrais modules PyMoDAQ, interface,
  chargement par le dashboard PyMoDAQ 5.1 et 5.2.
- Documentation : guide utilisateur, fiche incident, conception, réglage PID.
