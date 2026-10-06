# PID tuning guide — Laser Control (T11.4)

A PID regulates **one 0D signal** (e.g. `Cam1_x`, a centroid in pixels) — or several at once, see § 3 —
by moving **a combination of motors** (e.g. `OC_H` with a weight of 1). The PID tab is for its
**settings** (a PyMoDAQ parameter tree); it is **run** from its button in the Alignment tab (or by a
routine). At each period (0.5 s = 2 Hz):

```
error = setpoint − measurement
output = Kp·error + Ki·∫error·dt + Kd·d(measurement)/dt          (each term with its + / − sign)
step of motor i = output × weight_i; the whole step vector is scaled down so that no motor exceeds
"Max step / iteration" (its direction is kept), then ± max_step of the role (S3)
```

- A **fresh measurement** is needed at each iteration (S1). A measurement already used is not reused.
- If a motor is still moving, the iteration is **skipped**: no stacking of orders.
- **Deadband**: if |error| is smaller, nothing moves and the integral is frozen.
- **Anti-windup**: |Ki·integral| stays lower than or equal to the max step. The integral is reset at
  each resume (pause, hold).
- The D term acts on the measurement and not on the error: changing the setpoint does not cause a kick.

## 1. Finding the sign (the most important)

A wrong sign makes the loop **diverge**: the motor always goes the wrong way. The envelope (S4) and
the rate (S5) limit the damage, then hold the PID.

1. In the **Alignment** tab, make a small `+` step on the motor and watch the signal: does it increase
   or decrease?
2. If a `+` step **increases** the signal, then to correct a positive error (measurement too low) a
   positive step is needed: **Loop sign = +** and positive weight.
3. If a `+` step **decreases** the signal: **Loop sign = −** (applied live), or a negative weight.
   Choose one or the other, not both. Simpler: **Calibration › 🎯 Measure**, then **✔ Apply** sets the
   sign, Kp, deadband and settle time from the measured response.
4. The loop sign applies to the P, I and D terms together. A preset with different Kp / Ki / Kd signs
   (older versions) is unified to the Kp sign when loaded, with a warning in the log.
5. Write the direction in the role note (Configuration → Roles), for example "+ = beam to the right
   on Cam1".

## 2. Typical gains

Start with **P only**, low, with **Confirm each move** ticked: each step is proposed before being
sent, and the PID is paused during the question.

| Parameter | Starting point | Remark |
|---|---|---|
| Period | 0.5 s | shorter than the response time of the camera is useless |
| Kp | 0.05 – 0.5 (motor units / signal unit) | start from ~ 0.3 × (1 / sensitivity), the sensitivity being the change of the signal for a step of 1 |
| Ki | **0** | the motor already integrates the steps: P alone cancels a constant error. A small Ki (≈ Kp / 50) only reduces the lag on a fast drift, and after a large error the integral takes a long time to empty (residual offset) |
| Kd | 0 | with a noisy signal at 2 Hz, D mostly amplifies the noise |
| Deadband | ~ 2 × the standard deviation of the signal noise | avoids "chasing" the noise |
| Max step / iteration | a few motor steps | bounds each correction |
| Settle after move | 0 to 0.5 s | if the camera still integrates the old position |

Procedure: P only with confirmation, then P without confirmation. Increase Kp up to a slight
oscillation, and divide it by two (Kp ≈ 1 / sensitivity does the way in one step). If the measurement
lags behind the drift, add a small Ki, and check that no offset persists after a large misalignment.
With the simulator, the "Beam H / V" PIDs of the demo (Kp 0.6, Ki 0) show the expected behavior.
In the PID tab, the table at the top shows every PID (state, error, RMS error over 2 min, envelope
used, start / stop). Below, for the selected PID: measurement and setpoint with a marker at each move,
then the error with the P, I, D contributions, over a chosen window (30 s, 2 min, 10 min, all), with
the RMS error and the standard deviation of the measurement: use them to compare two settings.
When the PID is not active (stopped, paused, held), the plots freeze on the last iteration. The
tree has five groups: **General** (name, button in Alignment), **Motors** (tick the
motors the PID may move), **Signals** (per signal: setpoint, sign, Kp, Ki, Kd, deadband, motor
weights), **Calibration** and **Timing & limits** (max step, period, settle, confirmation). While the PID
runs, the gains and setpoints stay editable (applied live), not the signals, motors or weights.

## 3. Weights (several motors for one signal)

- The PID output is multiplied by the weight of each ticked motor. With `OC_H × 1` and `EM_H × −0.5`,
  a step of +1 on OC_H comes with −0.5 on EM_H.
- The max step applies **after** weighting (the step vector is scaled, its direction kept).
- Two PIDs must not drive the same motor at the same time (D4: the last activated one takes over, the
  other one pauses).

### Coupled PID (two signals, e.g. near field + far field)

When the same motors move two signals (beam walk: two mirrors move both the near field and the far
field), put both signals in **one PID**: **Signals › ➕ Add a signal (coupled PID)**, and tick at least
as many motors as signals. Then **Calibration & decoupling › 🎯 Measure**: every motor is moved by small
bipolar steps (the others still, back to the start position) and every signal is recorded, which gives
the sensitivity matrix. The result shows the matrix ± its errors, the condition number κ (< 10 good,
> 100 refused: two motors do the same thing), and for each signal the combination of motors that moves
**only that signal** (coupling now → after, and the coupling still allowed by the measurement errors).
**✔ Apply** writes these weights and, for each signal, the sign, Kp (0.5 / loop gain), deadband and the
settle time — refused if the errors allow more remaining coupling than *Max remaining coupling* (then
measure again with more cycles per motor). The PID then corrects both signals at once, one order per
motor per period; each signal keeps its own setpoint (also as an Alignment axis `SP:<pid>` and
`SP:<pid>/2`), gains and plot (choose it above the plots).

## 4. Envelopes and limits (Configuration → Safety)

| Limit | Role | Advice |
|---|---|---|
| Max step (S3) | largest step of an automatic order | a few steps: a correction, not a realignment |
| Max excursion (S4) | ± around the position at engagement | a bit more than the drift expected over a session. Too large: a wrong sign goes far. Too small: frequent holds. |
| Abs min / max | absolute limits, also for manual moves if ticked | mechanical stops or forbidden zones |
| Max rate (S5) over the window | max cumulated displacement over 60 s by default | 2 to 5 times the normal drift over the window |
| Resolution | smallest meaningful step | smaller steps are not sent |

Intensity guard (S2): relative threshold (fraction of the reference taken at engagement) or absolute,
with hysteresis. A relative threshold of 0.5 with a hysteresis of 0.1 holds below 50 % and only allows
the resume above 60 %.

When a PID is held on "envelope reached", the motor is at the edge of what was allowed. If the new
position is good, **New envelope reference** re-centers the envelope on the current positions.

## 5. Setpoint

- **fixed**: typed value. **Setpoint = current value** fills it with the last measurement.
- **capture**: the setpoint takes the measured value at each start (handy to "keep the beam where it is").

## 6. Check with the simulator before the lab

At the office (`python -m pymodaq_plugins_lasercontrol.tools.sim_preset`), the simulator roles
`Cam1_x` / `OC_H` and `Cam1_y` / `OC_V` are tuned as in the lab: drift of about one minute, noise,
couplings between axes. A preset that converges with the simulator is not necessarily good in the lab,
but a preset that diverges with the simulator almost certainly has a sign or gain problem.
