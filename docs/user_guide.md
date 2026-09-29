# User guide — Laser Control (T11.1)

## 1. Start

1. LECO coordinator, then computer B (cameras and 0D extension acquiring).
2. Computer A: PyMoDAQ dashboard with the lab preset, then *Extensions → Laser Control*
   (or `python -m pymodaq_plugins_lasercontrol.extensions.laser_control <preset>`).
3. Nothing starts by itself (S9). The top bar shows **Idle**.
4. If the **▶ Acquisition** button appears in the status bar, a detector used by the roles is not
   acquiring: click it.

## 2. Reading the status bar (always visible)

![Overview, stabilization active (simulator)](images/overview.png)

| Item | Meaning |
|---|---|
| Large badge on the left | **Idle** (nothing engaged) · **Running** · **Safety hold** (an automation is held) · **EMERGENCY STOP** · **Error** |
| Automation buttons | one per **engaged** automation (PID, optimization, scan). Color = state. A click opens the matching tab. |
| Detector light (e.g. `LECO_B 0.3 s`) | age of the last frame: green = fresh, orange = late, red = stale (the automations are held). |
| `I …` | intensity (S2 guard) and % of the reference: green = above threshold, red = below threshold (hold), gray = unknown. "guard DISABLED" in orange if the guard is switched off in the configuration. |
| ML ON / ML OFF / Unstable | mode-lock state, **to be clicked by you**: it is timestamped and recorded. |
| 📝 Note | free timestamped text (log + recording). |
| 🎢 Shake | fires the piezo shaker, with a recording burst if REC is active. |
| ● REC | HDF5 recording running or not. |
| EMERGENCY STOP (F12 or Pause) | stops every automation, then every motor. Then **Re-arm**. |

The **log**, at the bottom, lists every event (starts, preemptions, safety…). Alarms appear in red.

## 3. Loading a preset

**Presets**: choose a preset (**validated** presets come first), then **Load**. Loading starts nothing.
If a role or a channel is missing on this machine, a warning says so and the matching automation
cannot be activated. **Check on this machine** runs the check again.

## 4. Working from the Alignment tab

![Alignment tab: axes, automation buttons under the motors, history](images/alignment.png)

Under the motors, **one button per chosen automation**: in the PID or Optimization tab, give a short
**Display name** ("Optim HA", "Stab. OC_X"…) and tick **Button in Alignment**. For an optimization,
**Button action**: *one pass* (stops by itself), *periodic* (one pass every N minutes until stopped) or
*full optimization*.

| Button | One click | Color |
|---|---|---|
| Stopped | starts, **without confirmation** | gray |
| Active, arming, paused, error | stops (the motors stay where they are) | green, blue, orange, purple |
| Held (safety) | resumes, if the cause is gone | red |

- Right click: start, resume, stop, **Open settings**.
- The line of the button gives the state: PID error, pass iteration, next pass, reason of the hold.
  The tooltip gives the details (signal, motors, max step).
- If the start is refused (missing safety limits, missing signal, emergency stop…), the reason is
  written in red above the buttons and in the log. No dialog box.
- The settings stay in their tab; the button starts with the displayed settings, even unsaved.
- The keyboard stays active: a click on a button does not deactivate it. Moving a driven motor with the
  keyboard stops the automation that drives it (§ 5).
- Two automations on the same motor: the last one started takes over, the other one pauses and
  resumes by itself afterwards (for example a PID during an optimization pass).
- **PID setpoints as axes**: in Alignment → Configuration, an axis channel can be `SP:<PID id>` (the
  setpoint of that PID) instead of a motor. Selected with its key (1…9, 0), it moves with `a`/`d` and
  `s`/`w` like a motor (Slow / Fast = setpoint step; use the `step` or `auto` jog mode). The PID keeps
  running and moves its motors to follow: once the PIDs are well tuned, optimizing on the setpoints is
  more reliable than moving the motors by hand.
- The height of the button area is adjusted with the handle between the motors and the buttons.

## 5. Activating / deactivating the stabilization (Overview)

**Overview**:

- the summary lists the stabilization PIDs, the tracked signal and the driven motors;
- **Activate stabilization**: a summary of what will move (motors and limits) is shown, then you
  confirm. It is **all or nothing**: if one PID cannot start, none starts, and the reason is shown in
  red under the summary;
- **Deactivate**: stops the stabilization PIDs. The motors stay where they are;
- **Resume**: restarts the PIDs held by a safety rule, once the cause is gone (see 6).

The motor cards show the position, the **owner** (Free, Human, PID name, Optimizer, Scan) and the
**envelope** gauge (green, then orange, then red near the edge).

**Moving a motor by hand** (Alignment, XY, Go, saved positions) **first stops** the automation that
owns it. The log says so: "Cam1 X: ACTIVE → OFF (manual action on OC_H (alignment))". A move started
from the PyMoDAQ module itself, outside the extension, is detected and stops the automation too
("external move detected").

## 6. What to do when it is red

![Safety hold: computer B does not answer anymore, both PIDs are held](images/overview_hold.png)

| What I see | Likely cause | What to do |
|---|---|---|
| **Safety hold**, PID *Held (safety)*, reason "stale data" | computer B silent, LECO down, acquisition stopped | check computer B and the detector light (▶ Acquisition if needed). When it is green: **Resume** (or one click on the red button in Alignment). |
| reason "… frame counter frozen" | computer B still sends, but its counter does not advance anymore | restart the acquisition or the extension of computer B, then **Resume**. |
| reason "intensity below threshold" or "unknown intensity" | laser off, mode-lock lost, pump reduced, power measurement missing | restore the laser or the measurement. **Resume** is only possible above the threshold plus the hysteresis. |
| reason "envelope reached on …" | the PID pushes a motor to the edge of its excursion: drift too large, wrong sign or wrong gain | do not resume blindly. Re-center by hand if needed, check the sign (see `pid_tuning.md`), then **New envelope reference** or restart. |
| reason "rate limit reached on …" | too much cumulated displacement over the window (60 s by default) | same: find out why the PID pushes so much. |
| reason "timeout on …" | motor or controller not answering anymore | check the controller in the dashboard. |
| reason "safety limits not defined for …" | motor limits missing from the preset | Configuration → Safety: fill them in, save the preset. |
| automation in **Error** | exception in the computation (S8) | read the log, then restart. If it happens again, report the message. |
| **EMERGENCY STOP** | you (or F12 / Pause) | when the situation is safe: **Re-arm**, then restart what is needed. |

When in doubt, **Deactivate**: nothing moves. For any serious incident, see `incident_sheet.md`.

## 7. Recording and annotating

- **● REC** starts an HDF5 session: a periodic snapshot and bursts around the events chosen in the
  Recording tab.
- Click **ML ON / ML OFF / Unstable** at each change of the mode-lock and add **Notes**: this is what
  makes the recordings usable.

## 8. Shortcuts

| Where | Keys |
|---|---|
| everywhere | **F12** or **Pause**: emergency stop |
| Alignment (click on "Keyboard") | `a`/`d` horizontal, `s`/`w` vertical, **Shift** fast, `1`…`9`, `0` axis choice, **Space** shake |
| XY | `a`/`d` X, `s`/`w` Y, **Shift** ×10, **Space** shake, `T` / `Shift+T` next / previous group |
