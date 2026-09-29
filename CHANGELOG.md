# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: git tags (`hatch-vcs`).

## [Unreleased]

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
