# Document de conception — Laser Control (T11.3)

## 1. Vue d'ensemble

```
Dashboard PyMoDAQ (ordi A) : DAQ_Move de tous les moteurs, DAQ_Viewer LECO de l'ordi B, puissance…
        │  (lecture seule de modules_manager, une seule fois : hardware/pymodaq_ports.discover_ports)
        ▼
hardware/pymodaq_ports.py   DAQMovePort / DAQViewerPort : adaptateurs Qt ↔ ports
        │  ports (core/ports.py) : move_rel, move_abs, stop, position, frames {canal: valeur}
        ▼
core/ ─ LaserCore (coordinator.py) ─────────────────────────────────────────────────────────┐
  broker.py      seul accès au matériel : ordres, fin de mouvement, délais, mouvements externes  │
  signals.py     dernière valeur de chaque canal 0D, âge, compteur de trames (O9), NaN = invalide │
  ownership.py   un propriétaire par moteur (Humain, PID:x, OPT, SCAN…), préemption D4 / D5      │
  safety.py      S1–S5 : fraîcheur, garde intensité, pas bornés, enveloppe, débit, limites abs   │
  states.py      états par automatisme (OFF, ARMED, ACTIVE, PAUSED, HELD, ERROR) et état global  │
  roles.py       rôles logiques ↔ modules / canaux de la machine (TOML)                          │
  events.py      bus d'événements horodatés (journal, enregistrement, UI)                        │
  automation.py  classe de base des automatismes                                                 │
└───────────────────────────────────────────────────────────────────────────────────────────┘
        ▲ request_moves(), signals.value()          ▲ événements
engines/  PIDAutomation, OptimizerAutomation (+ algorithmes enfichables), Scan1D, XYScan : logique pure
recording/ Recorder (snapshots, rafales, tampon avant événement), H5Writer (thread), triggers (O8)
presets/  schéma versionné, store JSON, validation par machine, import des anciens profils
ui/       LaserControlApp + onglets ; les widgets ne parlent qu'au cœur et à ManualControl
extensions/laser_control.py   classe CustomExt (PyMoDAQ 5.1 et 5.2)
```

**Modèle d'exécution (O2-A)** : tout tourne dans le thread GUI. Un `QTimer` à 20 Hz appelle
`LaserCore.tick()` (délais, contrôles de sécurité, pas des automatismes). L'interface se rafraîchit
à 5 Hz au plus. Les ordres ne sont jamais bloquants : `move_rel` sur le module, puis fin de mouvement
par `move_done_signal`. Seule l'écriture HDF5 tourne dans un thread, derrière une file.

**Règle d'or** : les engines et l'UI ne touchent jamais `modules_manager`. Tout passe par le cœur.
Seuls `hardware/pymodaq_ports.py`, l'extension, les plugins LaserSim et l'outil `tools/sim_preset.py`
importent `pymodaq`. L'UI n'utilise que `pymodaq_gui` (ParameterTree, docks). Le cœur et les engines
ne dépendent ni de Qt ni de PyMoDAQ. Ils sont testés avec une horloge simulée
(`core/clock.FakeClock`) et des ports simulés (`core/fakes.py`).

## 2. Cycle de vie d'un automatisme

```
OFF ──start() : validation──► ARMED ──engagement──► ACTIVE ──stop()──► OFF
 ▲          │ refus (raison dans le journal)          │
 └──────────┘                                         ├── violation S1–S5 / délai ──► HELD ── resume() manuel (O10-A) ──► ACTIVE
                                                      ├── préemption D4 ──► PAUSED ── moteurs libérés : reprise auto (O3-A) ──► ACTIVE
                                                      └── exception ──► ERROR (S8)
```

- `start()` : validation (rôles résolus, modules présents et initialisés, positions connues, données
  fraîches, limites de sécurité complètes, garde intensité), puis mémorisation des positions
  d'engagement (enveloppe S4), capture éventuelle de la référence d'intensité, prise des moteurs.
- Préemption D4 : le dernier activé prend les moteurs communs. L'ancien passe en PAUSED
  (« preempted ») et reprend seul quand ils sont libérés. Son intégrale est alors remise à zéro.
- Action humaine (D5 / S7) : `human_preempt()` arrête (OFF) tout automatisme propriétaire **avant**
  l'envoi de l'ordre humain. Un mouvement lancé hors de l'extension est aussi détecté
  (`on_external_move`).
- HELD : aucun ordre n'est envoyé, l'intégrale est gelée, l'automatisme garde ses moteurs et sa
  référence d'enveloppe. La reprise est manuelle et revérifie données, intensité et positions.

## 3. Invariants et où ils sont garantis

| ID | Garanti par | Tests |
|---|---|---|
| S1 données fraîches | `SignalSource.freshness` et `all_fresh` (âge ≤ `max_data_age_s`, compteur de trames qui avance) ; vérifié à chaque tick et à chaque `request_moves` | `test_S1_stale_data_freezes`, `test_S1_frozen_heartbeat_is_stale`, `test_viewer_port_channels_and_mute` |
| S2 intensité | `IntensityGuard` (seuil relatif ou absolu, hystérésis ; référence = intensité à l'engagement, jamais nulle) | `test_S2_*` |
| S3 pas bornés | `SafetySupervisor.check_move` : `max_step` du rôle en plus du pas max de l'engine ; les automatismes n'ont que `move_rel` | `test_S3_only_bounded_relative_steps`, `test_pid_signs_and_clamp_after_weighting` |
| S4 enveloppe | `safety.engage` et `allowed_range` : ± `max_excursion` autour de l'engagement ∩ [`abs_min`, `abs_max`] ; au bord, pas écrêté puis gel | `test_S4_envelope_clamps_and_freezes`, `test_S4_absolute_limits` |
| S5 débit | `RateTracker` : déplacement cumulé sur une fenêtre glissante | `test_S5_rate_limit_freezes` |
| S6 un propriétaire | `Ownership` ; `request_moves` refuse un non-propriétaire | `test_S6_*` |
| S7 humain = préemption | `LaserCore.human_preempt` appelé par le broker avant tout ordre manuel | `test_S7_*`, `test_D5_external_move_detected`, `test_04_alignment_keyboard_preempts_pid` |
| S8 exception = gel | `tick()` capture l'exception : état ERROR, un seul événement | `test_S8_exception_freezes_once` |
| S9 rien ne démarre seul | aucun `start()` hors action explicite ; preset et session ne font que configurer | `test_S9_nothing_starts_by_itself`, `test_01_…`, `test_07_…` |
| S10 plantage = immobilité | pas de file d'ordres ; un moteur occupé n'en reçoit pas d'autre ; `limit` signalé dans l'UI | `test_S10_no_order_stacked_on_busy_motor` |
| S11 arrêt d'urgence | `emergency_stop` : automatismes arrêtés, puis `stop_motion` sur **tous** les modules du dashboard, état verrouillé | `test_S11_estop_stops_everything_and_latches`, `test_08_estop_and_close`, `test_roles_move_and_estop` |
| S12 pas de 0.0 trompeur | `extract_scalars` et `data_to_float` renvoient NaN ou None ; NaN = donnée invalide | `test_S12_*` |

Arbitrages (T12.3) : `test_S6_single_owner_and_O3_pause_resume`,
`test_optimizer_pass_pauses_pid_then_pid_resumes`, `test_xyscan_visits_pattern_and_stops_on_human_action`.
Délai de mouvement : `test_move_timeout_holds`. Les tests `test_pymodaq_ports.py`,
`test_extension_ui.py` et `test_dashboard_integration.py` utilisent de vrais DAQ_Move et DAQ_Viewer
(plugins LaserSim), jusqu'au chargement par le dashboard PyMoDAQ lui-même.

## 4. Rôles, presets, enregistrement

- **Rôles** (`core/roles.py`) : `roles_<machine>.toml`, avec des `[actuators.X] module=…`,
  `[signals.Y] channel="Détecteur/donnée/label"` et `[heartbeats] Détecteur = canal`. Les presets ne
  citent que les rôles. La validation d'un preset sur une machine liste les rôles manquants, et seul
  l'automatisme concerné est bloqué.
- **Presets** (`presets/schema.py`) : JSON avec `schema_version`, nom, description, statut
  (brouillon / validé, date, auteur, commentaire), conditions, PID, optimiseur, sécurité, alignement,
  XY, scan, enregistrement, courbes. `migrate()` fait monter les anciennes versions. L'empreinte du contenu
  est notée à la validation : un preset validé dont le contenu a changé repasse en brouillon.
- **Enregistrement** (`recording/`) : un fichier HDF5 par session (rotation par taille et par durée).
  Chaque snapshot contient **tous** les rôles, tous les moteurs (position lue et commandée), tous les
  canaux reçus (option), les variables internes de chaque automatisme (`Automation.variables()`),
  l'état de sécurité, l'état ML, le preset et son empreinte. Les événements sont dans une table
  séparée. Le tampon circulaire avant événement et les rafales sont rattachés à un `event_id`.
  Lecture : `recording.load_session()`.

## 5. Ajouter un moteur

![Configuration : rôles de la machine et compteur de trames](images/config.png)

Aucun code à écrire :

1. Ajouter le module (DAQ_Move) au preset du dashboard. Le plugin doit implémenter `move_rel`,
   `stop_motion` et `move_done`. `jog(direction, vitesse)` est optionnel et active le mode `native` de
   l'Alignement.
2. Dans l'extension, cliquer **Relire le matériel** (barre d'outils), ou relancer.
3. **Configuration → Rôles** : créer le rôle (nom, module, unité, note de sens), puis
   **Appliquer et enregistrer**.
4. **Configuration → Sécurité** : `max_step`, `max_excursion`, `rate_max`, et au besoin `abs_min` /
   `abs_max` et la résolution. Sans elles, aucun automatisme ne peut piloter ce moteur.
5. Le moteur apparaît dans la Vue d'ensemble, les listes des PID, de l'optimiseur et des scans, et
   dans l'enregistrement.

## 6. Ajouter un détecteur ou une grandeur 0D (O7)

1. Ajouter le DAQ_Viewer au dashboard (0D, ou tout viewer dont les données contiennent des scalaires).
2. **Relire le matériel**, lancer l'acquisition (**▶ Acquisition** ou Configuration → Détecteurs).
3. Configuration → Détecteurs → « Canaux 0D reçus » donne les clés `Détecteur/donnée/label`.
   Déclarer les rôles signal (Configuration → Rôles), et le compteur de trames s'il existe.
4. Le nouveau signal est enregistré automatiquement (T7.1) et devient utilisable par les PID,
   l'optimiseur, les scans et les courbes.

## 7. Ajouter un engine (automatisme)

Sous-classer `core.automation.Automation` dans `engines/` :

```python
class MonEngine(Automation):
    kind = 'mon_engine'

    def __init__(self, role_moteur, role_signal):
        super().__init__('MON:1', 'Mon engine')
        self.role_moteur, self.role_signal = role_moteur, role_signal
        self.period_s = 0.5

    def motor_roles(self):  return [self.role_moteur]       # propriété + sécurité
    def signal_roles(self): return [self.role_signal]       # fraîcheur exigée (S1)
    def policy(self):       return SafetyPolicy()           # S1, S2, S5 et enveloppe S4 par défaut

    def tick(self, core, now):                              # appelé toutes les period_s si ACTIVE
        v = core.signals.value(core.roles.channel_for(self.role_signal))
        if not math.isfinite(v):
            return                                          # S12 : jamais bouger sur NaN
        core.request_moves(self, {self.role_moteur: -0.1 * v})   # pas relatif, vérifié par le cœur

    def variables(self):                                    # enregistré dans chaque snapshot
        return {'derniere_valeur': ...}
```

- Ne jamais bloquer dans `tick()`, ne jamais appeler de module PyMoDAQ. `request_moves` renvoie les pas
  réellement envoyés : un moteur encore occupé n'en reçoit pas, et l'engine réessaie au tick suivant.
- Les hooks `validate`, `on_start` (message d'erreur = refus), `on_stop`, `on_pause`, `on_resume` et
  `on_hold` sont disponibles. `is_done()` signale la fin, et le cœur arrête alors l'automatisme.
- Le cœur ajoute l'automatisme avec `core.add_automation()`. Il apparaît dans la barre d'état et le
  journal, et il est enregistré.

### Ajouter un algorithme d'optimisation (L10)

Implémenter `start(ctx)`, `propose()` et `observe(result)` (`engines/optimizer/base.py`), ou écrire un
générateur qui `yield` des `Measure`, `MoveTo`, `Report` et `Status`. L'enregistrer avec
`register_algorithm('nom', 'Libellé', fabrique)`. Il apparaît dans la liste « Mode ». Le pilote
(`OptimizerAutomation`) convertit les décalages en pas bornés et vérifiés, et moyenne des trames
fraîches pour chaque mesure.

## 8. Compatibilité PyMoDAQ 5.1 / 5.2

- 5.1 : le dashboard crée l'extension avec `klass(area, dashboard=self)`. 5.2 : avec
  `create_extension(dashboard, klass)`, dans une fenêtre `SharedUI`, et exige l'action
  `show_dashboard` (ajoutée par `create_dashboard_toolbar`).
- Les deux API de `CustomApp` sont fournies (`setup_docks` et `setup_docks_and_widgets`,
  `setup_menu` et `setup_menus_and_toolbars`).
- Paramètres du module moteur : `move_settings` (5.1) ou `actuator_settings` (5.2).
- Un `move_abs` vers la position courante ne produit pas de `move_done` en 5.1. Le broker n'envoie
  donc pas ce mouvement nul (« déjà en position »).
- En 5.2, un changement d'expérience appelle `do_things_after_experiment_set`, qui relit le matériel.
