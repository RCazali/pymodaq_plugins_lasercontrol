# Roadmap — automated laser alignment with routines

Replaces `pid_roadmap.md` and `alignment_roadmap.md` (earlier iterations, removed; see the git history).
Only their **display constraints** are kept (§ 4).

## 1. Goal

Control a large part of the laser automatically, precisely and without operator error:

1. **Settings and execution are separate.** The Alignment tab only runs things (keyboard, go to, buttons
   of the PIDs, optimisations, routines); everything is set in its own tab — **intuitive, parameters easy
   to follow, in the PyMoDAQ style** (parameter trees).
2. **A PID with two motors (or more) and two measured quantities** (typically near field and far field),
   whose **decoupling is part of the PID settings**: measured and applied there, easily and
   automatically.
3. **Regular optimisation of the hard aperture (HA)** of the laser — it raises the flux but shifts the
   position (not the angle) of the beam a little — with the PIDs **paused** during it.
4. A **Routines tab** to program sequences of high-level **execution** actions — "activate A, then B,
   then C" — with a **delay** or a **condition on a measurement** (e.g. convergence of a PID) between
   them, and logic such as "**if the motor travelled more than x, go on with the next motor**". No setting
   is changed by a routine.
5. The routines are stored in the preset and shown as **buttons in the Alignment tab**, like the PIDs.

## 2. Current version (implemented)

| Need | Where |
|---|---|
| Settings as parameter trees | ⚙️ PID (`ui/pid_tab.py`), 🧭 Routines (`ui/routines_tab.py`), 🕹 Axes (settings of the Alignment axes, moved out of the Alignment tab), 📈 Optimization (already a tree) |
| Coupled PID | `PIDAutomation` with several loops (`engines/pid.py`): one per signal (setpoint, sign, Kp, Ki, Kd, deadband, motor weights), common max step / period / settle; one order per motor per period (contributions summed, step vector scaled, direction kept) |
| Decoupling in the PID settings | *Calibration & decoupling* group: 🎯 Measure (every motor on every signal of the PID, `CAL:<pid>`), matrix ± errors and κ, ✔ Apply (`engines/decoupling.py › decoupling_proposal`: J⁺ columns, Kp = 0.5 / loop gain, refused if the errors allow more remaining coupling than the threshold) |
| Pause without automatic resume | `LaserCore.pause()` (`pause_kind = 'manual'`, motors released), resumed by `resume()` / a Resume step / one click on its button |
| Routines | structured programs `engines/program.py` (blocks, validation, runner `ProgramAutomation` = `RTN:<id>`), execution only; edited as a block diagram in the Routines tab |
| Buttons in Alignment | band **SEQ**, routines first; state line "▶ Step n/N: …", "✔ Done", "✖ Failed · step n: why" |
| Preset (schema 3) | `pids[]: {id, label, motors: [...], loops: [{signal, setpoint, sign, kp, ki, kd, deadband, weights}], settings, calibration: {signal: {motor: result}}}`; schema 2 PIDs and "groups" are converted (a group becomes one coupled PID); `routines: [...]` |

### Routine model (structured blocks, § 3)
- A step = **action** (start, stop, pause, resume, move motor, note, wait only, hold, confirm at maximum)
  + **conditions** (time, PIDs locked — each within its own tolerance —, signal test with hold time, motor
  travel since the step began, automation finished, step count) + **timeout**. The first true condition
  gives the outcome: next / repeat the loop / leave the loop / end / fail.
- Blocks: ⟲ REPEAT, ⟲ FOR EACH position, ↗ ramp of the setpoints, ⏱ EVERY N s in the background.
- Routines saved by older versions ("go to" steps) are converted when loaded (backward "go to" →
  REPEAT); forward "go to" cannot be converted and is reported.

### Rules (safety)
- A routine starts only from a human action (S9) and runs in the core tick (no thread, never blocks).
- It watches what it starts or resumes: HELD, ERROR or stopped by somebody else = failure. On failure or
  stop, what it started is stopped (option); what it paused stays paused (nothing moves) and the log
  says so.
- It never resumes an automation held by a safety rule (O10-A): it fails instead.
- Its own moves go through `request_moves` like any automation (ownership, max step, envelope, rate);
  the PIDs of that motor are preempted during the move and resume by themselves.
- A running PID: only gains / setpoints / common settings are applied live; signals, motors and weights
  are locked. A running routine: its tree is read-only.
- E-STOP and a human action on a motor behave as for every automation.

### Simulator (office test bench)
`hardware/sim.py`: beam walk (two mirrors → near / far field, strong coupling) and a **hard aperture**
on that beam (`HA_X`, `HA_Y` → `HA_flux`: the flux depends on the aperture position and on the beam
angle; moving the aperture shifts the near field by 0.2 µm/µm, not the far field). "Simulator demo"
preset (`python -m pymodaq_plugins_lasercontrol.tools.sim_preset --force`): coupled PIDs "Pointing x / y"
(near + far, already decoupled; measure and apply again in the PID tab to see it), optimisation
`Optim_HA`, and four routines:

| Routine | Shows |
|---|---|
| HA check (PIDs paused) | lock, pause both PIDs, confirm the HA at its maximum (operator asked if not), resume, re-lock |
| Keep aligned (HA every 2 min) | lock and hold; ⏱ every 2 min the HA check interrupts the hold |
| Beam transport P1 → Final | FOR EACH position: slow ramp of the near / far setpoints, never leaving the beam behind; ⏱ every 30 s the HA check interrupts the ramp |
| Beam walk x, one mirror at a time | manual beam walk automated: travel limit hands over to the other mirror, REPEAT until both are centred, step-count guard |

The HA optimum drifts slowly (thermal: ±2 µm over minutes, 6 µm wide).

Acceptance tests: `tests/test_pid_group.py` (coupled PID, schema migration), `tests/test_routine.py`
(engine on the fake rig), `tests/test_routine_sim.py` (demo routines on the simulator model, offline),
`tests/test_program.py` (structured programs, beam transport), `tests/test_confirm.py` (confirm at maximum),
`tests/test_extension_ui.py` (PID tree, coupled PID decoupling, routine block editor + Alignment button).

## 3. Structured routines (implemented 2026-10-02)

The routine steps are shown and edited as **structured blocks** ("D" of `prototypes/routine_layouts.py`,
chosen 2026-10-01: reads like a program). The target program is tab **E** of the prototype ("beam
transport"): run `python prototypes/routine_layouts.py`, ▶ Simulate a run.

### Blocks
| Block | Meaning |
|---|---|
| Step | start / stop / pause / resume a PID or an optimisation, move a motor, note, wait until… (as today) |
| ⟲ REPEAT forever / while … (at most n×) | loops, instead of "go to" (current routines converted automatically) |
| ⟲ FOR EACH position in a table | positions = setpoints of the PIDs (near / far, x / y), **execution data** of the routine (agreed) |
| Ramp setpoints to a position | slow (e.g. 0.2 µm/s); waits while a PID is out of **its own** tolerance |
| ⏱ EVERY N s (background) | runs while the main sequence runs and **interrupts it at any moment** (ramp and timers frozen), then gives it back |
| ⛰ CONFIRM at maximum | robust extremum seeking whose only goal is to confirm a maximum (below) |

### Convergence
"Locked" = each PID within **its own tolerance** (e.g. Pointing x |err| < 0.5, Pointing y < 0.8) for a hold
time — not one global threshold.

### HA (hard aperture): confirm it is at its maximum
No threshold on the flux (it changes for other physical reasons). Every 30 s, PIDs paused:
probe X then Y with plateaus `+δ −δ −δ +δ` around the centre (mean of N frames each; the order cancels a
linear drift), gradient ± its error and curvature. Not significant and curved down → ✔ at maximum (8 small
moves). Significant → one step uphill and probe again, at most 5 times. δ from the noise of the flux, never
below 2 motor resolutions. **Not confirmed after 5 → the PIDs are resumed and hold the pointing, the main
sequence stays frozen, and the operator decides** (alert in Alignment: Continue / Check again / Stop).

### Flux with a 3rd mirror
Optional EVERY 5 min: optimiser on a **3rd mirror** with the PIDs **active** on the two pointing mirrors;
each flux measurement waits until every PID is back within its tolerance (otherwise it measures the PID
correction).

### Implementation steps (each: green tests, commit by the user)
1. **Done** — engine: nested blocks (step, repeat, for each, ramp, every with interruption), conversion of
   the current routines, tests on the simulator (beam transport program): `engines/program.py`,
   `tests/test_program.py`.
2. **Done** — "Confirm at maximum" algorithm in the optimiser (`engines/optimizer/confirm.py`, tests with
   noise, drift and a moving optimum: `tests/test_confirm.py`); "confirm" step of the programs, operator
   question (Continue / Check again / Stop) as a banner in Alignment; routines run on the structured
   engine. Learned on the simulator: the criterion is "within a tolerated loss (1 %)" because the top
   moves during the check, the probes must never be truncated by the step bound (false gradient), and
   the check must run with the pointing locked then paused (the flux depends on it).
3. **Done** — block editor in the Routines tab (`ui/routines_tab.py`): routines on the left, the program
   as a diagram in the middle (main sequence, then the ⏱ background blocks; ◆ selected, ▶ running,
   ‖ interrupted — shape + symbol, never colour alone), toolbar Step / Repeat / For each / Ramp / Every…
   (into the selected block, after the selected step, else at the end) and ▲ ▼ ⧉ ✖; the selected block in a
   parameter tree on the right (conditions with one tolerance per PID, positions table per PID and signal,
   ramp speed and tolerances, confirm "if not confirmed"), run controls, operator answers, history.
   Locked while the routine runs. The demo routines are native programs. Learned on the simulator: an HA
   optimum drifting as fast as the pointing (±7 µm in minutes) cannot be confirmed by any check; the
   simulator now drifts it slowly.

## 4. Display constraints (kept from the previous iterations)

### Screen
- Everything must work at **1300 × 700**; the tabs must never force a larger window (no minimum width or
  height above that, long texts elided with a tooltip, lists that scroll).
- In the lab only the **bottom half of the screen** is really seen: what is used every day lives at the
  bottom (Alignment tab laid out from the bottom up: keyboard state and STOP on the last line, motors and
  active axis just above, automation buttons and history plot at the top).
- The status bar (global state, engaged automations, E-STOP) stays always visible.

### Laser goggles (1 µm): colours are hard to tell apart
- **Never encode a state by hue alone**: always shape / symbol + text (+ luminance): `▶` active,
  `‖` paused, `■` held / stopped, `✖` error / failed, `✔` done, `⚠` problem.
- Prefer bright, high-luminance foregrounds on dark backgrounds; red may stay as a secondary cue only.
- Large bold fonts for operational values, thick frames for the active item, no thin grey text for
  anything operational.
