# Changelog

Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/). Versions : tags git (`hatch-vcs`).

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
