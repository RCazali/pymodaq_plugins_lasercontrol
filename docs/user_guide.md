# User guide — Laser Control

## 1. Start

1. LECO coordinator, then computer B (cameras and 0D extension acquiring).
2. Computer A: PyMoDAQ dashboard with the lab preset, then *Extensions → Laser Control*
   (or `python -m pymodaq_plugins_lasercontrol.extensions.laser_control <preset>`).
3. Nothing starts by itself (S9). The top bar shows **Idle**.
4. If the **▶ Acquisition** button appears in the status bar, a detector used by the roles is not
   acquiring: click it.

## 2. Reading the status bar (always visible)

| Item | Meaning |
|---|---|
| Large badge on the left | **Idle** (nothing engaged) · **Running** · **Safety hold** (an automation is held) · **EMERGENCY STOP** · **Error** |
| Automation buttons | one per **engaged** automation (PID, optimization, routine, scan). Symbol + color = state (▶ active, ‖ paused, ■ held, ✖ error). Held ones and routines first; "+n" = more (tooltip). A click opens the matching tab. |
| Detector light (e.g. `LECO_B 0.3 s`) | age of the last frame: green = fresh, orange = late, red = stale (the automations are held). |
| `I …` | intensity (S2 guard) and % of the reference: green = above threshold, red = below threshold (hold), gray = unknown. "guard DISABLED" in orange if the guard is switched off in the configuration. |
| ML ON / ML OFF / Unstable | mode-lock state, **to be clicked by you**: it is timestamped and recorded. |
| 📝 Note | free timestamped text (log + recording). |
| 🎢 Shake | fires the piezo shaker, with a recording burst if REC is active. |
| ● REC | HDF5 recording running or not. |
| EMERGENCY STOP (F12 or Pause) | stops every automation, then every motor. Then **Re-arm**. |

The **📜 Log** tab lists every event (starts, preemptions, safety…). Alarms appear in red.

## 3. Loading a preset

**Presets**: choose a preset (**validated** presets come first), then **Load**. Loading starts nothing.
If a role or a channel is missing on this machine, a warning says so and the matching automation
cannot be activated. **Check on this machine** runs the check again.

## 4. Working from the Alignment tab

**Settings and execution are separate.** The Alignment tab only *runs* things: keyboard jog, go to, the
buttons of the PIDs, optimizations and routines. Everything is *set* in its own tab, as a PyMoDAQ
parameter tree: **🕹 Axes** (axes, keys, jog steps, − / + button steps), **⚙️ PID**, **📈 Optimization**,
**🧭 Routines**.

The tab is laid out **from the bottom up** (in the lab only the lower half of the screen is visible
through the setup): the last line holds the keyboard state and **■ STOP ALIGNMENT**, just above it the
motor cards (left) and the active axis (right); the automation buttons and the history plot fill the
top.

Above the motors, **one button per chosen automation** (routines first, band SEQ; see 4b): in the PID,
Optimization or Routines tab, give a short
**Display name** ("Optim HA", "Stab. OC_X"…) and tick **Button in Alignment**. For an optimization,
**Button action**: *one pass* (stops by itself), *periodic* (one pass every N minutes until stopped) or
*full optimization*.

| Button | One click | Color |
|---|---|---|
| Stopped | starts, **without confirmation** | gray |
| Active, arming, paused by another automation, error | stops (the motors stay where they are) | green, blue, orange, purple |
| Paused by a routine | resumes | orange |
| Held (safety) | resumes, if the cause is gone | red |

- Right click: start, resume, stop, **Open settings**.
- The line of the button gives the state: PID error, pass iteration, next pass, reason of the hold.
  The tooltip gives the details (signal, motors, max step).
- If the start is refused (missing safety limits, missing signal, emergency stop…), the reason is
  written in red above the buttons and in the log (**📜 Log** tab; its title shows the number of
  warnings not read yet, e.g. "📜 Log (2 ⚠)"). No dialog box.
- The settings stay in their tab; the button starts with the displayed settings, even unsaved.
- The keyboard stays active: a click on a button does not deactivate it. Moving a driven motor with the
  keyboard stops the automation that drives it (§ 5).
- Two automations on the same motor: the last one started takes over, the other one pauses and
  resumes by itself afterwards (for example a PID during an optimization pass).
- **PID setpoints as axes**: in the Axes tab, an axis channel can be `SP:<PID id>` (the setpoint of
  that PID; `SP:<PID id>/2` for the second signal of a coupled PID) instead of a motor. Selected with its key (1…9, 0), it moves with `a`/`d` and
  `s`/`w` like a motor (Slow / Fast = setpoint step; use the `step` or `auto` jog mode). The PID keeps
  running and moves its motors to follow: once the PIDs are well tuned, optimizing on the setpoints is
  more reliable than moving the motors by hand.
- The height of the button area is adjusted with the handle between the motors and the buttons.

## 4b. Routines (🧭 Routines tab): programs of actions

A **routine** chains high-level actions like a small program: *start the PIDs, wait until each one is
within its tolerance, then for each position ramp the setpoints slowly; every 30 s pause the PIDs and
check the hard aperture…*. It is programmed once in the **Routines** tab and started from its button in
Alignment (band **SEQ**, shown first) or with **▶ Run**. A routine only **executes**: it never changes a
setting (gains, decoupling are set in the PID tab); the positions of a FOR EACH / ramp are its own data.

The tab shows the routine as a **diagram**: the **main sequence**, then the blocks that run **in the
background**. Click a block to edit it in the tree on the right. The toolbar adds a block into the selected
REPEAT / FOR EACH / EVERY, after the selected step, or at the end; ▲ ▼ ⧉ ✖ move, duplicate, delete it.

| Block | Does |
|---|---|
| ▪ Step | one **action**, then **wait until** one of its conditions is true (first true wins; none: go on when the action is done); a **timeout** is the last resort (→ fail by default) |
| ⟲ REPEAT | its blocks again and again (forever or n×), until a condition says *leave the loop*, *END* or *FAIL* |
| ⟲ FOR EACH position | its blocks once per position of a table (setpoints of the chosen PIDs, one value per signal) |
| ↗ Ramp | moves the PID setpoints slowly (units / s) to the position of the FOR EACH (or its own target) and **waits while a PID is out of its tolerance**: the beam is never left behind |
| ⏱ EVERY N s | in the background: when due, it **interrupts the main sequence at any moment** (its ramp and timers are frozen), runs, then gives it back |

| Action of a step | Does |
|---|---|
| Start / Stop | a PID or an optimization (one pass, periodic, full) |
| Pause / Resume | nothing moves while paused, and a paused automation **does not resume by itself**: a Resume step (or its button: one click = resume) restarts it. A PID *held* by a safety rule is never resumed by a routine. |
| Confirm at maximum | an optimization in "confirm maximum" mode (e.g. the hard aperture, PIDs paused). Not confirmed → by default the PIDs are resumed and hold the beam and **the operator is asked**: ▶ Continue / ↻ Check again / ■ Stop (banner in Alignment, buttons in the Routines tab) |
| Move motor | relative or absolute, in bounded steps with the safety limits; a PID using this motor pauses during the move and resumes by itself |
| Hold | stays there until the routine is stopped (e.g. keep the final position) |
| Note / Wait | writes in the log / only waits |

| Condition | True when |
|---|---|
| Time (s) | the delay has elapsed since the end of the action |
| PID converged | each listed PID within **its own tolerance** (every signal) for *Hold* s |
| Signal | signal `<`, `>`, `\|x\|<`, `\|x\|>` value for *Hold* s |
| Motor travel | the motor moved by ≥ value since the step began (e.g. "too far: hand over to the other motor") |
| Finished | the automation ended by itself (one optimization pass) |
| Step count | the step was entered ≥ n times (guard for loops) |

Then: **next**, **repeat the loop**, **leave the loop**, **END** (success), **FAIL**.

- What a routine **starts** is watched: held, error or stopped by somebody else → the routine **fails**. On a
  failure or a stop, what it started is stopped (option *On failure / stop: stop what it started*); what
  it paused stays paused, the log says so.
- A routine ends with its last block: what it started keeps running (e.g. PIDs locked).
- Editing is locked while it runs (a run uses the routine as it was at its start). In the diagram the
  running block has a thick frame and **▶**, a main-sequence block interrupted by a background block a
  dashed frame and **‖**, the selected block a dashed white frame and **◆**; the run history is on the right.
- Routines saved by older versions ("go to" steps) are converted when loaded: a backward "go to" becomes a
  REPEAT.

Simulator examples ("Simulator demo" preset): *HA check (PIDs paused)*, *Keep aligned (HA every 2 min)*
(hold + background check), *Beam transport P1 → Final* (FOR EACH + ramp, HA check every 30 s), *Beam walk x,
one mirror at a time* (travel limit + loop guard).

## 5. Moving a motor by hand

**Moving a motor by hand** (Alignment keyboard, − / + buttons, Go) **first stops** the automation that
owns it. The log says so: "Cam1 X: ACTIVE → OFF (manual action on OC_H (alignment))". A move started
from the PyMoDAQ module itself, outside the extension, is detected and stops the automation too
("external move detected").

## 6. What to do when it is red

**Resume** = one click on the red button of the automation in Alignment (or right click → resume).

| What I see | Likely cause | What to do |
|---|---|---|
| **Safety hold**, PID *Held (safety)*, reason "stale data" | computer B silent, LECO down, acquisition stopped | check computer B and the detector light (▶ Acquisition if needed). When it is green: **Resume**. |
| reason "… frame counter frozen" | computer B still sends, but its counter does not advance anymore | restart the acquisition or the extension of computer B, then **Resume**. |
| reason "intensity below threshold" or "unknown intensity" | laser off, mode-lock lost, pump reduced, power measurement missing | restore the laser or the measurement. **Resume** is only possible above the threshold plus the hysteresis. |
| reason "envelope reached on …" | the PID pushes a motor to the edge of its excursion: drift too large, wrong sign or wrong gain | do not resume blindly. Re-center by hand if needed, check the sign (see `pid_tuning.md`), then **New envelope reference** or restart. |
| reason "rate limit reached on …" | too much cumulated displacement over the window (60 s by default) | same: find out why the PID pushes so much. |
| reason "timeout on …" | motor or controller not answering anymore | check the controller in the dashboard. |
| reason "safety limits not defined for …" | motor limits missing from the preset | Configuration: the motor is marked ⚠. Fill in its max step, max excursion and max travel per window, save the preset. |
| automation in **Error** | exception in the computation (S8) | read the log, then restart. If it happens again, report the message. |
| **EMERGENCY STOP** | you (or F12 / Pause) | when the situation is safe: **Re-arm**, then restart what is needed. |

When in doubt, **stop the automation** (its button): nothing moves.

## 7. Recording and annotating

- **● REC** starts an HDF5 session: a periodic snapshot and bursts around events. Not tested on the laser
  yet: its settings tab is hidden for now, the settings of the preset are used.
- Click **ML ON / ML OFF / Unstable** at each change of the mode-lock and add **Notes**: this is what
  makes the recordings usable.

## 8. Shortcuts

| Where | Keys |
|---|---|
| everywhere | **F12** or **Pause**: emergency stop |
| Alignment (click on "Keyboard") | `a`/`d` horizontal, `s`/`w` vertical, **Shift** fast, `1`…`9`, `0` axis choice, **Space** shake |