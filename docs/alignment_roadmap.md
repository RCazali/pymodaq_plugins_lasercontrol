# Alignment cockpit roadmap — one tab for daily use, keyboard-first, extremum seeking

Status: **proposal, not implemented**. Complements [pid_roadmap.md](pid_roadmap.md) (calibration, loop
state, coupled PID), which stays valid and is reused here.

Goal: everything is configured once in the detailed tabs; afterwards a whole alignment session is run
from the **Alignment tab alone**, mostly with the keyboard:

1. move motors with the keyboard (exists), with **user-defined keys** (default `1 2 3 4 5 6 7 8 9 0`);
2. **PIDs are driven like motors**: select one, jog its setpoint;
3. an **extremum-seeking optimizer with step dither** (continuous or every x minutes) for small
   corrections on a slow photodiode;
4. a layout readable at **1300 × 700**, in the **bottom half of the screen**, through **1 µm goggles**.

Hard constraints from the lab: the Zaber motor runs **under vacuum** (limited number of orders per
second, minimum wear, no heat build-up); the operator sees mostly the bottom half of the screen; the
goggles tint everything green.

---

## 1. Layout (1300 × 700, bottom-weighted)

Rule: **what is used every day lives in the bottom ~55 %; what is set once lives at the top and can be
collapsed** (one click / `F12` collapses it, the state is remembered).

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ TOP (setup, collapsible, ~35 %)  motor cards · history plot · config buttons │
├──────────────────────────────────────────────────────────────────────────────┤
│ COCKPIT (always visible, ~55 %)                                              │
│ ┌ ACTIVE ITEM (big) ─────────────┐ ┌ LIVE PLOTS ────────────────────────────┐│
│ │ [3] Gas jet xy   ↔ 12.3450 mm  │ │ signal / PID error / optimizer cost    ││
│ │                  ↕  4.0100 mm  │ │ (one plot, tabs by what is running)    ││
│ │ step  slow 0.10  ▸FAST 2.00    │ │                                        ││
│ └────────────────────────────────┘ └────────────────────────────────────────┘│
│ ┌ MOTOR STRIP: 1 2 3 4 5 6 7 8 9 0 ┐ ┌ LOOPS: F1 F2 F3 … (PID + optimizer) ─┐│
│ │ compact cards, active one framed │ │ ● LOCKED  ▲ CONVERGING  ■ HELD  …    ││
│ └──────────────────────────────────┘ └──────────────────────────────────────┘│
│ [⌨ ACTIVE] key: 3   slow/FAST   wear: 412 moves/h   [■ STOP ALIGNMENT]       │
└──────────────────────────────────────────────────────────────────────────────┘
```

- Motor cards shrink to one line each in the strip (key badge, name, value); the **active item is shown
  large once** (the current detail panel, moved to the cockpit) instead of every card being big.
- Loops (PIDs, optimizer) get the same kind of compact card with a **state badge** (§ 2 of the PID
  roadmap) and their own key.
- Live plots: a single plot area that shows the trace of what is running (PID error, optimizer cost,
  dither demodulation), so the operator does not switch tabs to know whether it works.
- The status bar (keyboard state, STOP, wear counter) stays on the very bottom line: always visible.
- Minimum widths chosen so that 3 columns of strip cards + plots fit in 1300 px; nothing needs a
  vertical scrollbar in the cockpit with ≤ 10 motors and ≤ 4 loops.

### Visibility through 1 µm goggles (everything looks green)
The goggles shift colour perception: green stays distinguishable, **red becomes dark / hard to tell
from black**, and a green-tinted "OK" is easily confused with the general tint.

- Never encode a state by hue alone: **always shape + text + luminance** (`● LOCKED`, `▲ CONVERGING`,
  `◆ OSCILLATING`, `■ HELD`, `✖ DIVERGING`).
- Palette: dark neutral background, **bright, high-luminance** foregrounds (white, warm yellow,
  cyan); alerts = **bright yellow on dark + blinking border**, not red. Keep red only as a secondary
  cue. Avoid mid-tone greens as "OK" (they vanish in the tint): "OK" = white/cyan with a check mark.
- Large bold fonts (values ≥ 16 pt in the cockpit, badges ≥ 12 pt), thick frames (3 px) for the active
  item, no thin grey text for anything operational.
- A **"Goggles" theme switch** (Config tab, stored in the preset) so the normal theme stays available
  for people without goggles; contrast checked on the simulated tint (multiply R and B by ~0.5) in a
  small unit test of the palette (luminance contrast ≥ 7:1 for operational text).

---

## 2. Configurable keyboard

### Bindings
- A **Key bindings** table in the Alignment › Configuration sub-tab: one row per item (motor axis, PID,
  optimizer, shaker, fast/slow, collapse setup), column *Key* with a **"press a key" capture** button.
- Defaults: axes `1…9, 0` (as today), loops `F1…F10`, `Space` shaker, `Shift` fast, `w/s/a/d` jog.
- Stored in the preset (`alignment['keys']`); the current behaviour is the default when absent.
- Validation: no duplicate key, reserved keys (`w a s d Shift Ctrl Alt Esc`) refused, warning when a
  key would type into a focused field. Bindings work only while the keyboard is ACTIVE (as today).

### PIDs as motors
- Selecting a PID (`F1…`) makes it the **active item**: `a/d` (or `w/s`) jog its **setpoint** with the
  same slow/fast steps as a motor, using the existing `SP:<pid id>` virtual actuators (moving a
  setpoint never stops the PID; 100 ms per virtual step).
- Extra keys on a selected loop: `Enter` = start/resume, `Backspace` = hold/stop, `R` = reset the
  integral (postponed items of the PID work can be added here later). Nothing starts by itself (S9):
  `Enter` is a human action.
- Two-signal PIDs (H/V setpoints): `a/d` = horizontal setpoint, `w/s` = vertical, like a 2-axis motor.
- Setpoint step sizes come from the calibration (`σ`, sensitivity): default slow = 2σ, fast = 10σ,
  editable per PID.

---

## 3. Extremum seeking with step dither ("Dither" optimizer mode)

### Principle for a stepper
A sinusoidal dither is wrong for a stepper (each cycle = many tiny orders, high rate, wear). Use a
**square-wave / step dither**: the motor is at rest on plateaus; a plateau costs **one order**.

Single motor, centre `c`, amplitude `a`, cost `J` measured on a plateau (settle + average):

```
c → c+a : J₊      c+a → c−a : J₋      c−a → c+a : J₊'     c+a → c : (back)
gradient  ĝ = (mean(J₊,J₊') − J₋) / 2a      (order + − + cancels a linear drift)
update    c ← c + k · ĝ   (limited to ±max_update, clamped to max_step and the envelope)
```

Several motors at once: each motor follows an **orthogonal ±1 (Walsh/Hadamard) sequence** instead of
different frequencies. With K steps (K ≥ p + 1) the correlation of `J` with each sequence gives the
gradient of every motor from the same K plateaus (fewer orders than probing motors one by one, and
orthogonality separates them). Linear drift is cancelled by a drift term in the fit or by symmetric
sequence ordering. This is exactly the estimator of the PID roadmap (Hadamard design), used as a
gradient instead of a Jacobian — the code is shared.

### "Filter the effect based on the step"
Demodulation = correlation of the measured plateaus with the known step sequence (lock-in in the
discrete domain), so everything not correlated with the steps (noise, slow drift, other loops) is
rejected. Shown live in the cockpit plot: cost per plateau, the demodulated gradient with its
uncertainty, the SNR = |ĝ| / stderr(ĝ), and the centre position. The step size is chosen from the noise
(σ from calibration or measured at rest): `a` such that the expected signal change `½·curvature·a²`
or `|g|·a` is ≥ 3σ_plateau, never below a few resolutions of the motor.

### Slow photodiode timing
`settle` = 5 τ of the photodiode (or measured), then average M frames; the **cycle rate is set by the
photodiode, not by the software**. Target: an order of magnitude faster than the PIDs (which is
seconds → dither plateau ≈ 0.3–1 s). Timing parameters are explicit and shown as "≈ x moves/min".

### Two modes
| Mode | Behaviour |
|---|---|
| **Continuous** | dither cycles back-to-back, the centre follows the gradient; the amplitude shrinks when SNR is high and the gradient ≈ 0 (converged), grows when SNR is low (up to a limit) |
| **Periodic (every x min)** | sleeps, then runs a short burst (n cycles) and stops as soon as `|ĝ|` is below noise (converged) → most bursts cost few orders. The interval reuses the existing `interval_min` of the optimizer |

Implementation: a new generator algorithm `engines/optimizer/dither.py` registered next to `joint /
sequential / nelder-mead / gradient` (`Measure` / `MoveTo` / `Report` protocol already there), so
the periodic scheduling, "Optimize now", safety and reporting of the optimizer are reused. Continuous
mode = the pass has no stall exit, only a user stop or a violation.

### Protecting the Zaber under vacuum
- **Hard order budget**: `rate_max` (S5) enforced by the core as today, *plus* a dither-specific
  **minimum dwell** (default ≥ 0.5 s per order) and a **moves-per-hour cap** per motor; when the cap
  is reached the dither pauses ("wear budget reached") instead of degrading silently.
- **Wear counters** persisted per motor (orders, cumulative travel, since when) in a small JSON next
  to the preset, shown in the status bar; a configurable warning threshold.
- **Dead-band and sleep**: when SNR is low and `|ĝ|` below noise for N cycles → the continuous mode
  falls back to "sleep" (no orders) and re-checks every x min. A well-aligned system moves almost
  never.
- Amplitude never below `max(2 × resolution, backlash)` so an order always produces a real move
  (no wasted orders that only rattle), never above `max_step / 2`.
- Keep the centre update **quantised to the motor resolution** and skip orders smaller than that.
- Thermal (vacuum: no convection): favour few large-enough orders over many tiny ones; check the
  Zaber driver settings (idle/hold current) with the vendor documentation, since a stepper holding
  current heats a stage in vacuum. To verify with the hardware, not assumed.
- Position polling is not part of the dither loop: the position is read once per order (the move
  result), never faster.

### Interaction with the PIDs
A PID and the dither must never fight for a motor: ownership (D5/S7) already forbids two owners on the
same motor. Two intended arrangements:
1. **Dither on motors not held by a PID** (independent axes) — simplest;
2. **Dither on PID setpoints** (`SP:<id>` are virtual actuators): the dither moves the target, the PID
   does the motion. The dither must then be **≥ 3–5× slower than the PID** (its plateau must contain
   the PID settling). This is the opposite of "10× faster than the PID" and must be a conscious
   choice.

---

## 4. Steps (each ends with green tests and a commit by the user)

1. **Finish the PID foundations** already specified: calibration UI (Calibrate / Apply, results in the
   preset — `engines/calibration.py` exists), then the loop state indicator (PID roadmap § 1–2).
   Needed for σ, sensitivity and the state badges used below.
2. **Configurable key bindings** (table, capture, validation, preset storage) with the current keys as
   default. Pure `KeyMap` class + tests, then the config UI.
3. **PIDs in the Alignment tab**: loop cards with badge, active-item selection by `F1…`, setpoint jog,
   start/hold keys.
4. **Cockpit layout + goggles theme**: collapsible setup area, compact strip, large active item, one
   live-plot area, bottom status bar; palette contrast test.
5. **Dither estimator (pure)**: sequence design (Walsh), demodulation, amplitude choice, wear
   budget — numpy only, tests against simulator benches with known gradient, noise and drift.
6. **Dither algorithm in the optimizer** (continuous + periodic), wear counters, sleep logic, UI in the
   optimizer tab and the cockpit; simulator scenarios (drifting optimum) as acceptance tests.
7. **Coupled PID** (PID roadmap § 3), which reuses the Hadamard estimator of step 5.

Order rationale: 1 gives the numbers everything else needs; 2–4 make the daily workflow good before
adding the new optimizer; 5–6 are the new capability; 7 is independent and last.

---

## 5. Additional recommendations
- **Dry-run / preview** for the dither: show the planned amplitude, dwell, orders per hour and the
  travel envelope before starting; nothing starts by itself (S9).
- **Undo last automatic correction** key (`Backspace` on the optimizer card): returns to the position
  before the last dither update.
- **Log every automatic order** (already in the event log) with the reason (`dither`, `pid`), and
  export the wear counters with the recording.
- **Audible/visual cue** when a loop changes state (short flash of the badge border) — no sound by
  default in a shared lab.
- **Focus safety**: a clearly visible "keyboard inactive" state (exists) and auto-deactivation when the
  window loses focus (exists: jogs released); keep it for new keys.
- **Tab order / mouse-free**: the whole cockpit reachable without the mouse except STOP (mouse only by
  design).

## 6. Decisions (answered by the user)
1. **Dither acts on the motors directly** (option 1). It is meant for metrics that cannot be turned into
   a PID (extremum of a signal, no setpoint). Option 2 (dither on setpoints) is dropped.
2. Photodiode: 1 kHz analog, **acquisition at ~10 Hz**. Plateau = settle + average of ~3–10 frames at
   10 Hz, so a plateau is ≈ 0.5–1.5 s; defaults are derived from this.
3. Zaber: standard 25 mm-travel stage under vacuum, wear cap unknown → conservative default
   (e.g. ≤ 600 orders/h/motor, editable) until the user decides.
4. Goggles: red is not unreadable, the problem is **telling colours apart**. Consequence: distinct
   states must differ by shape, text and luminance, never by hue alone (already the rule of § 1); red
   may stay as a secondary cue.
5. `engines/calibration.py` was interrupted and is **not finished**: step 1 starts by auditing it
   (fit + automation exist; UI, preset storage, Apply and tests to verify or complete).
