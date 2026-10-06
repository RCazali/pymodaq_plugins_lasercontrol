# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: git tags (`hatch-vcs`).

## [Unreleased]

### Removed
- **Overview tab** and its grouped "Activate / Deactivate stabilization" (with the `Stabilization` flag of the
  PIDs): the PIDs are started from their buttons in Alignment, from the PID tab or by a routine.
- **XY tab** (groups, map, saved positions, XY snake scan). The motors are moved from the Alignment tab. Old
  presets load unchanged: their `xy_groups` / `overview` entries are dropped.
- **Recording tab hidden** (not tested on the laser yet): REC in the status bar still records with the
  settings of the preset.
- Repository: design prototype, working documents (roadmap, design, incident sheet) and the screenshots
  not used by the README.

### Changed
- **Axes tab**: Min / Max / Speed parameter are only shown in the `limit` jog mode, the only one using them.
  The travel of a motor is only bounded by the abs limits of its role (Configuration → Safety).

### Added
- **Routine block editor** (🧭 Routines tab, layout "D"): the routine is shown as a program — main sequence,
  then the ⏱ background blocks — and edited block by block: toolbar Step / ⟲ Repeat / ⟲ For each / ↗ Ramp /
  ⏱ Every… (into the selected block, after the selected step, else at the end), ▲ ▼ ⧉ ✖; the selected block
  in a parameter tree (conditions with one tolerance per PID, positions table per PID and signal, ramp speed
  and tolerances, "if not confirmed"). While a routine runs its blocks are marked ▶ running / ‖ interrupted
  (thick or dashed frame + symbol), the editor is locked, and the operator answers are also offered there.
  Routines are saved as programs (`main`, `background`); older step routines are converted when loaded.
- Simulator demo: routines rewritten as programs — "HA check (PIDs paused)", "Keep aligned" (HA check every
  2 min in the background), new **"Beam transport P1 → Final"** (ramp along positions, HA check every 30 s),
  "Beam walk x" (REPEAT).
- **Confirm maximum (robust extremum seeking)**: new optimisation mode (`engines/optimizer/confirm.py`) whose
  only goal is to confirm that a signal is at its maximum, never comparing its absolute value. Per motor:
  plateaus `0 +δ −δ −δ +δ 0` (repeated), fit gradient + curvature + slow drift, error = the larger of the
  frame noise and the residual scatter. At the maximum = curvature of a top and gradient not significant, or
  estimated loss ≤ tolerance (1 %: the top itself moves). Otherwise one step towards the top (Newton) and probe
  again (5 tries); a flat response enlarges the probe. Settings in the Optimization tab ("Confirm maximum"
  group). Checked on the simulated hard aperture with the pointing locked then paused: confirmed 30 / 30
  (10 seeds × at the top / 3 µm off on each side), without moving at the top.
- **Routines run on the structured engine**; new step "confirm at maximum" (an optimisation in that mode).
  Not confirmed → by default the routine **asks the operator**: what it paused (the PIDs) is resumed and
  holds the beam, the routine waits; a banner in the Alignment tab (⚠ + text + bright frame) offers
  ▶ Continue / ↻ Check again (pause again and check again) / ■ Stop.
- **Structured routines — engine** (`engines/program.py`): nested blocks
  instead of "go to" — steps, `repeat` (forever or n×), `foreach` position (setpoints of the PIDs, per signal),
  `ramp` of the setpoints at a given speed that waits while a PID is out of its own tolerance, and
  background `every N s` blocks that interrupt the main sequence at any moment (its ramp and its timers are
  frozen meanwhile). Outcomes next / repeat / break / end / fail; "PID converged" with one tolerance per
  PID; `hold` until stopped. Step routines are converted automatically (backward "go to" → `repeat`).
  Checked on the simulator with the "beam transport" program (lock, ramp P1 → Final interrupted by the HA
  optimisation every 30 s, PIDs paused during it, lock on each position), on 10 random seeds.
- Optimization tab: its parameter tree is filled when first shown (preset loading ≈ 0.3 s faster).
- **Settings separated from execution**: the Alignment tab only runs things (keyboard, go to, buttons);
  everything is set in its own tab as a PyMoDAQ parameter tree — ⚙️ PID, 🧭 Routines, 📈 Optimization, and
  the new **🕹 Axes** tab (the former Alignment › Configuration sub-tab, with the step of the − / +
  buttons, which is no longer editable in Alignment; "Hide unassigned" moved there too).
- **Coupled PID**: a PID regulates one or several signals ("loops": setpoint, sign, Kp, Ki, Kd, deadband
  and motor weights each) with the motors ticked in its settings; common max step / period / settle. At
  each period the contributions of the loops are summed, the step vector is scaled (direction kept) and
  one order per motor is sent. Its decoupling is part of its settings (*Calibration & decoupling*):
  🎯 Measure (every motor on every signal of the PID), matrix ± errors, κ, coupling now → after, ✔ Apply
  (weights = columns of J⁺, Kp = 0.5 / loop gain, deadband, settle; refused when the errors of the
  measurement allow more remaining coupling than the threshold). One Alignment setpoint axis per signal
  (`SP:<pid>`, `SP:<pid>/2`).
- **Routines** (new "🧭 Routines" tab, `engines/routine.py`), execution only: start / stop / pause / resume
  a PID or an optimization, move a motor, note, wait — each followed by conditions (time, PID converged
  for a hold time — every signal of the PID —, signal test, motor travel since the step began,
  automation finished, step count) whose first true one decides the next step (next, go to n, end,
  fail), plus a timeout. Runs as an automation `RTN:<id>` in the core tick; what it starts is watched
  (held / error / stopped by somebody else = failure) and stopped on failure (option); it never resumes a
  safety hold. Stored in the preset (`routines`), buttons in the Alignment tab (band SEQ, listed first).
- **Deliberate pause** (`LaserCore.pause`): nothing moves, motors released, no automatic resume (unlike a
  preemption); one click on the button, the Resume button or a routine step resumes it.
- Simulator: **hard aperture** (`HA_X`, `HA_Y` → `HA_flux`) on the beam of the beam walk: the flux
  depends on the aperture position and on the beam angle, and moving the aperture shifts the near field.
  "Simulator demo": coupled PIDs "Pointing x / y" (near + far field), optimization `Optim_HA`, and three
  demo routines (HA with the PIDs paused, periodic HA, automated beam walk with a travel limit). Run
  `tools.sim_preset --force` to get them (it also works around the PyMoDAQ 5.1 naming bug above 10
  actuators).
- `docs/roadmap.md` replaces `docs/pid_roadmap.md` and `docs/alignment_roadmap.md` (only their display
  constraints are kept).
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
- **Sensitivity matrix**: the calibration measures each motor on several signals at once
  (`CalibrationAutomation(extra_signals=…)`); `engines/decoupling.py` gives the combination of motors
  that moves only one signal (column of J⁺, largest weight ±1), the condition number, the coupling now →
  after and the coupling still allowed by the errors of J. A motor with no clear effect on a signal no
  longer blocks the Kp proposal of a combination: the significance is checked on the total loop gain.
- **Log in its own tab** ("📜 Log", last tab) instead of a dock under the tabs; the tab title counts the
  warnings / alarms not read yet. The window now fits 1300 × 700 (minimum height 775 → 663 px).
- **Alignment tab laid out from the bottom up** (only the lower half of the screen is visible in the
  lab): keyboard state and STOP ALIGNMENT on the last line, motor cards and active axis just above, the
  automation buttons and the history plot at the top. Inner tab headers stay at the top.
- **PID setpoints in the Alignment tab**: each PID setpoint is a virtual axis `SP:<pid id>` that can be
  assigned to an axis channel (h / v) like a motor, and moved with the keyboard (step mode, 10 steps/s,
  Slow / Fast = setpoint step). Moving a setpoint does not stop the PID (it is not a motor); the value
  is applied live and kept in sync with the PID tab. Demo: axis "Beam setpoint" (Beam H / Beam V).
- Simulator: two coupled benches with known matrices — coupled mount (`Cpl_A/B` → `Cpl_x/y`, moderate
  coupling) and beam walk (`BW1_H/V`, `BW2_H/V` → `Near_x/y`, `Far_x/y`, strong coupling), driven in the
  demo preset by independent PIDs. Run `tools.sim_preset --force` to get them.

### Changed
- Status bar: the chips of the engaged automations share the free width (the window stays at 1300 px),
  show their state by a symbol too (▶ ‖ ■ ✖), held ones and routines first, "+n" for the rest.
- **PID tab redesigned**: overview table of every PID (state, signals → motors, errors, RMS errors,
  worst envelope use, start / stop / resume button) with the selected PID below: its settings in one
  parameter tree (General, Motors, Signals, Calibration & decoupling, Timing & limits; a single **loop
  sign** per signal) and two linked plots for the chosen signal: measurement + setpoint with move
  markers, error + P / I / D terms; time window and statistics (RMS error, σ of the measurement,
  iterations, moves). Preset schema 3 (PIDs with loops); schema 2 PIDs and PID groups are converted.
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
