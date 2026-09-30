# PID roadmap — calibration, loop state, coupled (MIMO) control

Status: **§ 1 implemented** (`engines/calibration.py`, PID tab), § 2 and § 3 are proposals. Three features for the PID module, in the order they should
be built: each one reuses the previous one.

1. [Automatic calibration](#1-automatic-calibration) — sign, sensitivity, noise, suggested settings.
2. [Loop state indicator](#2-loop-state-indicator) — Locked / Converging / Oscillating / Diverging.
3. [Coupled PID](#3-coupled-pid-mimo) — several signals, several motors, robust identification of the
   sensitivity matrix, iterative refinement.

All three run inside the existing core: every move goes through `request_moves` (S3–S6), stale data
and low intensity hold them (S1, S2), a human action stops them (S7), nothing starts by itself (S9).

---

## 1. Automatic calibration

### Goal
One button per PID, **Calibrate**, which measures for each motor of the PID:

| Result | Use |
|---|---|
| sign and **sensitivity** `g` = Δsignal / Δmotor (signal units per motor unit) | loop sign, Kp |
| noise σ of the signal (standard deviation at rest) | deadband, tolerance of the loop state |
| response time (settling after a move) | "Settle after move", period |
| asymmetry + / − and linearity | warning (backlash of picomotors, non-linear zone) |
| confidence of `g` (standard error) | refuse to apply a bad calibration |

and proposes: **loop sign** = sign(g), **Kp = α / |g|** with α = 0.5 (half the error corrected per
iteration, robust to a ±50 % error on g), **deadband = 2σ**, **settle** = measured settling time.
The user sees the proposal and clicks **Apply** (nothing is changed silently).

### Procedure (per motor, the others still)
1. **Noise**: N₀ fresh frames at rest (default 20) → mean s₀, σ.
2. **Step amplitude δ**: start from `max(resolution, max_step / 4)`; after the first bipolar cycle, if
   |Δs| < 5σ increase δ (×2) up to `max_step`; if the signal leaves a linear-looking range, reduce δ.
   δ always stays inside the envelope (explicit range = start ± 2δ, like the scans).
3. **Bipolar, symmetric pattern** repeated R times (default 3):
   `0 → +δ → 0 → −δ → 0`. At each position: wait for `move_done`, then the settle time, then average
   M fresh frames (default 5). The pattern starts and ends at the start position.
4. **Estimation**: linear least squares on all the plateaus
   `s_k = s₀ + g·m_k + d·t_k (+ ε)` where `d·t` is a linear drift term. The symmetric pattern makes g
   insensitive to a linear drift. Outputs g, its standard error, the residual σ.
5. **Checks** (each one gives a warning, the first two refuse the result):
   - |g| / stderr(g) < 5 → "response not significant: increase δ or check the motor";
   - significant quadratic term (fit `s₀ + g·m + c·m² + d·t`) with |c|·δ / |g| > 30 % → "non-linear
     response" (refused);
   - residual σ ≫ noise σ / √M → warning only: on a real beam it is usually slow correlated noise
     (drift), already included in the standard error of g (a residual-based refusal rejected good
     calibrations on the simulator beam-walk bench);
   - |g₊ − g₋| / |g| > 30 % (g fitted separately on + and − steps) → "asymmetric: backlash?";
   - settling time > period → "increase Settle after move".
6. **Return** to the start position (absolute move, checked like any other move).

### Implementation notes
- `engines/calibration.py`: `CalibrationAutomation(Automation)` driven by the core tick, sampling like
  `Scan1DAutomation` (new frames only, `seq` numbers), explicit envelope policy. Pure functions
  `fit_sensitivity(plateaus) -> CalibrationResult` testable without Qt.
- Results stored in the preset (`pid['calibration'] = {role: {g, stderr, sigma, settle_s, date}}`):
  they are also the starting point of the coupled PID (§ 3).
- The same automation calibrates several motors in sequence (one column of the matrix each).

### Tests
Simulator with known gain and noise → g within 5 %, sign right; linear drift → g unaffected;
backlash added in a fake actuator → asymmetry warning; human action during calibration → stopped
(S7); return to the start position.

---

## 2. Loop state indicator

A state computed from the trace (`TracePoint` list) at each iteration, shown in the overview table,
on the Alignment button and in the detail panel. Tolerance `tol = max(deadband, 3σ)` (σ from the
calibration, otherwise estimated from the error while Locked).

| State | Rule (last N iterations, N = 10 by default) | Display |
|---|---|---|
| **Locked** | \|error\| < tol during the last T_lock (default 10 iterations) | green |
| **Converging** | \|error\| decreasing: slope of \|error\| < 0 and not Locked | blue |
| **Oscillating** | error changes sign in > 60 % of consecutive iterations **and** RMS(error) > tol | orange, hint "lower Kp" |
| **Saturated** | output clipped at max step, same sign, for ≥ 5 iterations | orange, hint "drift faster than max step" |
| **Diverging** | \|error\| grows by > 50 % over N iterations while the motors move in the direction commanded | red |

**Diverging → automatic hold** (new violation code `diverging`, same path as S4/S5), reason
"error increases while correcting: sign probably wrong". Manual resume only (O10-A). This catches a
wrong sign in a few iterations, long before the envelope.

Implementation: `engines/pid_health.py`, pure function `loop_state(points, tol, max_step) ->
(state, hint)`, unit tests on synthetic traces (exponential convergence, oscillation, ramp beyond max
step, divergence). The thresholds go in the Advanced settings.

---

## 3. Coupled PID (MIMO)

### Why
When one motor moves several signals (e.g. OC_H moves the beam on Cam1 **and** Cam2, or H and V are
crossed by a rotated mount), independent PIDs fight each other: each one sees the other's moves as a
disturbance, which gives slow convergence or oscillations. A coupled PID corrects all the signals at
once with a model of the couplings.

### Model
For small moves, `Δs = J · Δm` where `s` are the n signals, `m` the p motors and `J` the n × p
**sensitivity matrix** (Jacobian). Column j of J = response of every signal to a unit step of motor j.

### Control law
- error vector `e = s* − s` (setpoints s*), integral in **signal space**;
- signal-space PI: `u = Kp·e + Ki·∫e`;
- motor step `Δm = J⁺_λ · u` with a **regularized pseudo-inverse** (SVD `J = U Σ Vᵀ`):
  `J⁺_λ = V diag(σᵢ / (σᵢ² + λ²)) Uᵀ`, λ = a small fraction of σ_max (default 5 %). Directions with a
  tiny singular value (motors that almost do the same thing) are not amplified.
- **step limit by scaling, not by clipping**: if a component of Δm exceeds its max step, the whole
  vector is scaled down, so the correction keeps its direction (clipping each motor would change the
  direction and break the decoupling). The S3 clamp of the core stays as a last guard.
- n > p (more signals than motors): least squares correction; n < p: minimum-norm motor move.

Kp = 0.5 again means "half of the error corrected per iteration" in every direction, whatever the
couplings: tuning is the same as for a single PID.

### Identification of J (robust)
Three levels, each one usable alone:

**a. Column by column** (§ 1 run on each motor, the others still): simple and easy to check,
p × R bipolar cycles. Good default.

**b. Simultaneous design** (faster, more robust to drift): all motors move at each step following a
**Hadamard / ±1 design** (orthogonal pattern: each motor ±δⱼ, balanced so that the mean position is
the start position). With K steps (K ≥ p + 2, multiple of 4) the least squares fit
`S = s₀ + J·ΔM + d·t` estimates every column at once, the linear drift term d is fitted too, and the
orthogonality makes the columns independent of each other. **Huber (robust) regression** limits the
effect of isolated spikes. Output: J, the standard error of each coefficient, the residual.

**c. Online refinement (iterative)** while the loop runs: each iteration provides a pair
(Δm actually sent, Δs measured after settling). The estimate is updated with **recursive least
squares with forgetting factor** (RLS, λ_f ≈ 0.98, one RLS per signal row), which:
- only corrects the directions actually excited (no drift of unobserved directions, unlike a
  plain Broyden update);
- follows slow changes of the system (alignment, temperature);
- is guarded: update only if |Δm| > 3 × resolution and |Δs| > 3σ; each update bounded (relative
  change of a column ≤ 20 %); a covariance reset if the estimate becomes inconsistent.

**Protection against spurious correlations (checkpoints).** The loop moves the motors *because* of
the error, and the error also contains drift and other disturbances: a naive online fit can "learn"
a coincidence (a drift that happened to follow a move) and slowly corrupt J. Hence:

- **Learning is optional** (setting *Online learning*: Off / Suggest / Auto, default **Off**). With
  *Suggest*, the learned matrix is only shown next to the accepted one; nothing changes until the user
  accepts it.
- **Accepted matrix = checkpoint.** The loop always runs on the last *accepted* J (the checkpoint,
  stored in the preset with its date, κ and validation score). The learned estimate lives beside it
  and is **never used directly** by the loop.
- **Promotion only after a validation test**: a candidate J replaces the checkpoint only if, over a
  test window (e.g. the next 30 iterations, or an explicit validation step as in step 2 of the
  procedure), its **prediction error of Δs is lower** than the checkpoint's by a margin (e.g. 20 %),
  with enough excitation (the excited directions cover every column: minimum singular value of the
  recent Δm matrix above a threshold). Otherwise it is discarded.
- **Consistency guards on each update**: residual of the pair (Δs − J·Δm) compared with the noise
  (outliers > 4σ ignored, Huber weights); no update while the signal is non-stationary (large jump of
  the drift estimate between the plateaus before and after the move), while a motor was busy by
  another owner, or right after a resume.
- **Drift budget**: if the learned J moves away from the checkpoint by more than a limit (e.g. 30 % in
  Frobenius norm, or a sign change of a diagonal term), learning stops and a warning asks for an
  explicit re-identification (levels a / b) instead of trusting the online estimate.
- **Automatic rollback**: if, after a promotion, the loop state (§ 2) becomes Oscillating or
  Diverging, or the RMS error gets worse than before the promotion by a margin, the previous
  checkpoint is restored and learning goes back to *Suggest*. The last few checkpoints are kept
  (history in the preset), and the user can go back to any of them.

In short: learning proposes, a validation decides, and the last good matrix can always be restored.
If the system is stable enough, all of this is simply left Off and the identification of levels a / b
is redone by hand from time to time.

Optional **dither**: when locked, the moves are tiny and carry no information; a very small ±δ
perturbation (PRBS, amplitude ≈ 2σ in signal space) can be enabled to keep learning. Off by default.

**Iterative procedure proposed to the user** (button *Identify*):
1. level a or b gives J₀ (with uncertainties shown);
2. **validation step**: one closed-loop move targeting 50 % of the current error; predicted vs
   observed Δs → prediction error in %;
3. if the prediction error > 20 %: one more identification pass with larger δ (or level b), merged
   with the previous estimate (weighted by the uncertainties);
4. accepted J is stored in the preset with its date, condition number and prediction error;
5. while running, level c refines J; the drift of J from the stored one is shown, and "Save learned
   matrix" copies it to the preset.

### Quality checks shown to the user
- matrix view (signals × motors, colored by value), standard errors;
- **condition number** κ = σ_max / σ_min: < 10 good, 10–100 "weakly controllable direction",
  > 100 refused (a motor is redundant or does nothing — remove it or add a signal);
- **coupling ratio** of each column (off-diagonal / diagonal): if everything is < 10 %, the tool says
  "independent PIDs are enough" (and can split the coupled PID into single PIDs);
- predicted vs measured response of the last validation step.

### Safety
- identification = an automation with an explicit envelope (start ± amplitudes), return to start,
  stopped by any human action (S7), held on stale data or low intensity (S1, S2);
- the loop state indicator (§ 2) applies to the norm of the error; **Diverging** holds the loop — a
  wrong matrix behaves like a wrong sign;
- online refinement never changes J by more than the guards above, and never while the loop is held.

### Implementation plan
1. `engines/mimo.py`: `SensitivityModel` (J, uncertainties, SVD, regularized pseudo-inverse, scaling),
   `fit_design(plateaus, drift=True, robust=True)`, `RLSModel.update(dm, ds)` — pure numpy, unit tests
   with a known J (random, ill-conditioned, with drift and spikes).
2. `engines/calibration.py` extended to the Hadamard design (level b).
3. `CoupledPIDAutomation(Automation)`: n signals, p motors, uses the model; same trace / health / UI
   hooks as the single PID.
4. UI: in the PID tab, type "Coupled" (several signals and setpoints, matrix panel, *Identify*,
   *Validate*, *Learning on/off*, *Save learned matrix*); same overview row and Alignment button.
5. Simulator: **done** — two coupled benches with known matrices (`hardware/sim.py`,
   `COUPLED_SIMPLE`: coupled mount Cpl_A/B → Cpl_x/y, moderate coupling; `BEAM_WALK`: two mirrors
   BW1/BW2 H+V → near- and far-field cameras, strong coupling, κ ≈ 6 per plane, small H↔V
   cross-coupling). The "Simulator demo" preset drives them with independent PIDs (Coupled x/y,
   BW near/far x/y) to show the problem; the coupled PID will be validated against the same matrices.

### Tests
Known J recovered within 5 % (levels a and b) with noise and linear drift; spikes rejected by the
robust fit; RLS converges to a changed J; ill-conditioned J → regularization bounds the steps, κ > 100
refused; coupled loop on the simulator converges in fewer iterations than two independent PIDs;
wrong J (column swap) → Diverging hold.

---

## Suggested order of work
1. Calibration of a single PID (§ 1) + Apply of the proposed settings.
2. Loop state indicator (§ 2) with the automatic hold on divergence.
3. `SensitivityModel` + identification level a (reuses § 1), then the coupled PID and its UI.
4. Hadamard design (level b), then online RLS refinement (level c) and the dither option.
