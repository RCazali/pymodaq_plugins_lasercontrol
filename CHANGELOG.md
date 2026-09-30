# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: git tags (`hatch-vcs`).

## [Unreleased]

### Added
- **Automatic PID calibration** (`engines/calibration.py`, PID tab "🎯 Calibrate"): each motor of the PID
  is moved by small bipolar steps (the others still, back to the start position), a drift-robust least
  squares fit gives the sensitivity, its error, the noise and the settling time. Refused: a response
  that is not significant, or a significant curvature (quadratic term changing the slope by > 30 % over
  ±δ). Warnings only: asymmetric / backlash, settle > period, plateaus scattering much more than the
  frame noise (slow drift of the beam: the error of g already includes it). "✔ Apply proposal" sets the
  loop sign, Kp (half of the error corrected per iteration, for a combination of motors Kp = 0.5 / Σ
  weight·g), deadband (2σ) and settle time after confirmation. Results are kept in the preset
  (`pids[].calibration`). Checked on the simulator coupled benches: gains within a few % of the known
  matrices, single motors and combinations.
- **Decoupling of PIDs** (PID tab "🔀 Decouple PIDs…", `engines/decoupling.py`): the calibration now
  measures each motor on several signals at once (`CalibrationAutomation(extra_signals=…)`), which gives
  the sensitivity matrix J of a group of PIDs. Each PID is proposed the combination of motors that moves
  only its own signal (column of J⁺, largest weight ±1), with its condition number, the coupling now →
  after and the coupling still allowed by the errors of J; "✔ Apply weights" sets the weights, keeps the
  calibration in the preset and sets sign / Kp / deadband / settle of the new combination. Non-modal
  window (E-STOP reachable), cycles per motor adjustable. A motor with no clear effect on a signal no
  longer blocks the Kp proposal of a combination: the significance is checked on the total loop gain.
  **Limit:** decoupled PIDs share their motors, so they cannot run at the same time yet (single owner
  per motor, the last started pauses the other).
- **Log in its own tab** ("📜 Log", last tab) instead of a dock under the tabs; the tab title counts the
  warnings / alarms not read yet. The window now fits 1300 × 700 (minimum height 775 → 663 px).
- **Alignment tab laid out from the bottom up** (only the lower half of the screen is visible in the
  lab): keyboard state and STOP ALIGNMENT on the last line, motor cards and active axis just above, the
  automation buttons and the history plot at the top. Inner tab headers stay at the top.
- PID tab on small screens (1300 × 700): the "2. Settings" column and the calibration results scroll
  instead of forcing the window height (minimum height of the tab 561 → 340 px).
- `docs/alignment_roadmap.md`: plan for the alignment cockpit (layout, goggles palette, configurable keys,
  PIDs as jogged items, step-dither extremum seeking).
- **PID setpoints in the Alignment tab**: each PID setpoint is a virtual axis `SP:<pid id>` that can be
  assigned to an axis channel (h / v) like a motor, and moved with the keyboard (step mode, 10 steps/s,
  Slow / Fast = setpoint step). Moving a setpoint does not stop the PID (it is not a motor); the value
  is applied live and kept in sync with the PID tab. Demo: axis "Beam setpoint" (Beam H / Beam V).
- Simulator: two coupled benches with known matrices — coupled mount (`Cpl_A/B` → `Cpl_x/y`, moderate
  coupling) and beam walk (`BW1_H/V`, `BW2_H/V` → `Near_x/y`, `Far_x/y`, strong coupling), driven in the
  demo preset by independent PIDs. Run `tools.sim_preset --force` to get them.
- `docs/pid_roadmap.md`: design of the calibration, the loop state indicator and the coupled PID.

### Changed
- **PID tab redesigned**: overview table of every PID (state, signal → motors, error, RMS error,
  worst envelope use, start / stop / resume button) with the details of the selected PID below.
  Only the motors used by the PID are listed ("➕ Add motor", ✕ to remove). Settings in two levels
  (essential / Advanced) with a single **loop sign** and an "⇄ Invert sign" button. Two linked plots:
  measurement + setpoint with move markers, error + P / I / D terms; time window and statistics
  (RMS error, σ of the measurement, iterations, moves).
- PID engine: the trace keeps every iteration (`TracePoint`: error, P / I / D, output, moves sent);
  `error_stats()` helper.
- The whole plugin (user interface, messages, logs, docstrings, documentation) is now in English.
  Documentation files renamed: `docs/user_guide.md`, `docs/incident_sheet.md`, `docs/design.md`,
  `docs/pid_tuning.md`, `docs/examples/`.
- Simulator demo preset renamed "Simulator demo" (was "Démo simulateur"); its periodic optimization
  id is now `Track_PD` (was `Suivi_PD`). Run `tools.sim_preset --force` to rewrite it.

## [0.2.0] — 2026-09-23

The Alignment tab becomes the main workstation.

### Added
- **Automation buttons in the Alignment tab**, under the motors: one per PID or optimization marked
  "Button in Alignment". One click starts, another one stops, a click on a held automation resumes
  it, without confirmation. Right click: resume, stop, open settings. A refusal (missing limits,
  emergency stop…) is shown under the buttons and in the log, without a dialog box.
- **Several named optimizations** (Optimization tab: add, duplicate, rename, remove), each one with its
  short name and the action of its button (one pass, periodic, full).
- Simulator: two test benches, beam pointing (`Steer_H/V` → `Beam_x/y`) and photodiode
  (`PD_X/Y` → `PD_current`). `tools.sim_preset` also writes the Laser Control preset
  "Simulator demo" (PIDs and optimizations with a button, safety limits, alignment axes).

### Changed
- Presets: schema 2 (`optimizers`, list). A schema 1 preset is converted when loaded: its optimizer
  becomes the "Optimizer" optimization.
- The status bar only shows the engaged automations.
- `tools.sim_preset` completes the existing roles file (without changing the existing roles).
- Simulator: random intensity drops disabled by default (setting of the `SimB` viewer).
- PID guide: start from Ki = 0 (the motor already integrates the steps).

## [0.1.0] — 2026-09-23

First version: a single extension replaces LaserController, XY Stage Controller and their tabs,
according to the specification (D1–D9, options O2, O3, O4, O6, O8, O9 and O10 as A).

### Added
- Event-driven core in the GUI thread (O2-A): hardware broker, signal source with freshness and frame
  counter (O9-A), motor ownership (D4: the last activated one takes over, the other one pauses, O3-A;
  D5: human action = stop), S1–S5 safety supervisor, state machine, event bus, latched emergency
  stop (S11).
- Engines: several simultaneous PIDs (anti-windup, deadband, no derivative kick, move confirmation,
  captured setpoint), pluggable periodic optimizer with the 4 historical modes (Joint, Sequential,
  Nelder-Mead, Gradient/Newton-lite) and the full optimization, 1D scan, XY snake scan, passive monitor.
- User interface: always-visible status bar, Overview (one-click all-or-nothing stabilization, motor
  cards with owner and envelope), Alignment (step / native / limit keyboard jog, clickable history),
  XY (groups, saved positions, map), PID, Optimization, 1D Scan, Recording, Presets, Configuration
  (roles, heartbeat, safety, detectors, folders).
- Logical roles per machine (`roles_<machine>.toml`): presets are portable between office and lab.
- Versioned presets (draft / validated, content hash, validation per machine), session restored
  without starting anything, import of the legacy profiles (sessions, PID, optimizer, XY).
- HDF5 recording (O4-A): periodic snapshots of every role and automation, bursts on event with a
  pre-event buffer, notes, ML state (O6-A), rotation, `load_session` reader, trigger files for
  computer B (O8-A).
- LaserSim simulator (motor and 0D detector plugins: drift, noise, couplings, intensity drops,
  computer B silent or frozen) and `tools/sim_preset` tool (preset / experiment and roles of the simulator).
- Non-blocking Red Pitaya shaker.
- 68 tests: invariants S1–S12, engines, presets, recording, real PyMoDAQ modules, user interface,
  loading by the PyMoDAQ 5.1 and 5.2 dashboard.
- Documentation: user guide, incident sheet, design, PID tuning.
