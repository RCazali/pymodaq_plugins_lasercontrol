"""Automatic calibration of the motors of a PID.

For each motor (the others still): noise at rest, then a bipolar symmetric pattern
``0 → +δ → 0 → −δ → 0`` repeated R times. Each plateau is the mean of fresh frames taken after the
move is done (+ settle). A least squares fit ``s = s0 + g·m + d·t`` gives the sensitivity g, robust
to a linear drift. The motor goes back to its start position at the end.

Pure math in :func:`fit_sensitivity` / :func:`suggest_pid_settings` (testable without Qt nor
hardware), adapter in :class:`CalibrationAutomation` (driven by the core tick, moves through the
broker, explicit envelope start ± max δ).
"""
from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import numpy as np

from ..core.automation import Automation
from ..core.events import EventKind
from ..core.safety import SafetyPolicy
from ..core.signals import is_finite
from .sequencer import MoveSequencer

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore


@dataclass
class Plateau:
    """Mean signal at one motor offset (relative to the start position)."""
    t: float            # mean time of the frames used
    m: float            # motor offset from the start position
    s: float            # mean signal
    n: int = 1


@dataclass
class CalibrationResult:
    g: float = math.nan                 # sensitivity: signal units per motor unit
    stderr: float = math.nan            # standard error of g
    s0: float = math.nan
    drift: float = 0.0                  # signal units per second
    residual_sigma: float = math.nan    # residual of the fit (plateau level)
    noise_sigma: float = math.nan       # standard deviation of one frame at rest
    g_plus: float = math.nan            # g fitted on the + steps only
    g_minus: float = math.nan           # g fitted on the − steps only
    settle_s: float = math.nan          # measured settling time after a move
    delta: float = math.nan             # final step amplitude
    n_plateaus: int = 0
    curvature: float = 0.0              # |c|·δ / |g|: relative change of the slope over ±δ
    curvature_significant: bool = False
    warnings: List[str] = field(default_factory=list)
    ok: bool = False                    # False: the result must not be applied

    @property
    def significance(self) -> float:
        if not (math.isfinite(self.g) and math.isfinite(self.stderr)):
            return math.nan
        return math.inf if self.stderr == 0 else abs(self.g) / self.stderr

    @property
    def asymmetry(self) -> float:
        if not all(math.isfinite(v) for v in (self.g, self.g_plus, self.g_minus)) or self.g == 0:
            return math.nan
        return abs(self.g_plus - self.g_minus) / abs(self.g)

    def to_dict(self, date: Optional[str] = None) -> dict:
        """What the preset keeps (``pid['calibration'][role]``)."""
        return {'g': self.g, 'stderr': self.stderr, 'sigma': self.noise_sigma, 'settle_s': self.settle_s,
                'ok': self.ok, 'warnings': list(self.warnings),
                'date': date or datetime.datetime.now().isoformat(timespec='seconds')}

    @classmethod
    def from_dict(cls, d: dict) -> 'CalibrationResult':
        def f(k):
            try:
                return float(d.get(k))
            except (TypeError, ValueError):
                return math.nan
        return cls(g=f('g'), stderr=f('stderr'), noise_sigma=f('sigma'), settle_s=f('settle_s'),
                   warnings=[str(w) for w in d.get('warnings', []) or []], ok=bool(d.get('ok', False)))


def _lstsq(t: np.ndarray, m: np.ndarray, s: np.ndarray, drift: bool,
           quadratic: bool = False) -> Tuple[np.ndarray, np.ndarray, float]:
    """(coefficients [s0, g, (d), (c)], standard errors, residual sigma)."""
    cols = [np.ones_like(m), m]
    if drift:
        cols.append(t - t.mean())
    if quadratic:
        cols.append(m ** 2)
    x = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(x, s, rcond=None)
    dof = len(s) - x.shape[1]
    res = s - x @ coef
    sig = math.sqrt(float(res @ res) / dof) if dof > 0 else math.nan
    try:
        cov = np.linalg.inv(x.T @ x) * (sig ** 2 if math.isfinite(sig) else math.nan)
        err = np.sqrt(np.abs(np.diag(cov)))
    except np.linalg.LinAlgError:
        err = np.full(x.shape[1], math.nan)
    return coef, err, sig


def fit_sensitivity(plateaus: List[Plateau], noise_sigma: float = math.nan, samples: int = 1,
                    period_s: Optional[float] = None, settle_s: float = math.nan,
                    min_significance: float = 5.0, max_residual_ratio: float = 4.0,
                    max_asymmetry: float = 0.3, max_curvature: float = 0.3) -> CalibrationResult:
    """Least squares ``s = s0 + g·m + d·t`` over the plateaus, plus the checks of the roadmap.

    ``noise_sigma`` is the frame noise at rest; a plateau averages ``samples`` frames, so its
    expected noise is ``noise_sigma / sqrt(samples)``. Refused (``ok = False``): a response that is
    not significant, or a significant quadratic term (``s0 + g·m + c·m² + d·t``) changing the slope by
    more than ``max_curvature`` over ±δ. The other checks only warn: a residual much larger than the
    frame noise is usually slow correlated noise (drift of the beam), which the standard error of g
    already includes.
    """
    r = CalibrationResult(noise_sigma=noise_sigma, settle_s=settle_s)
    pts = [p for p in plateaus if math.isfinite(p.s) and math.isfinite(p.m) and math.isfinite(p.t)]
    r.n_plateaus = len(pts)
    if len({round(p.m, 12) for p in pts}) < 2 or len(pts) < 4:
        r.warnings.append('not enough plateaus to fit a sensitivity')
        return r
    t = np.array([p.t for p in pts], float)
    m = np.array([p.m for p in pts], float)
    s = np.array([p.s for p in pts], float)
    drift = bool(np.ptp(t) > 0) and len(pts) >= 5
    coef, err, sig = _lstsq(t, m, s, drift)
    r.s0, r.g, r.stderr, r.residual_sigma = float(coef[0]), float(coef[1]), float(err[1]), sig
    r.drift = float(coef[2]) if drift else 0.0
    for sel, attr in ((m >= 0, 'g_plus'), (m <= 0, 'g_minus')):
        if sel.sum() >= 4 and np.ptp(m[sel]) > 0:
            d = drift and sel.sum() >= 5
            setattr(r, attr, float(_lstsq(t[sel], m[sel], s[sel], d)[0][1]))

    # non-linearity: a significant quadratic term that changes the slope by more than max_curvature at ±δ
    if len({round(v, 12) for v in m}) >= 3 and len(pts) >= (6 if drift else 5) and r.g != 0:
        qc, qe, _ = _lstsq(t, m, s, drift, quadratic=True)
        c, c_err = float(qc[-1]), float(qe[-1])
        r.curvature = abs(c) * float(np.max(np.abs(m))) / abs(r.g)
        significant = c_err == 0 or (math.isfinite(c_err) and abs(c) / c_err >= 3.0)
        r.curvature_significant = bool(significant)

    r.ok = True
    sgn = r.significance
    if not (sgn >= min_significance):
        r.ok = False
        r.warnings.append('response not significant: increase δ or check the motor')
    if r.curvature > max_curvature and r.curvature_significant:
        r.ok = False
        r.warnings.append(f'non-linear response (slope changes by {100 * r.curvature:.0f} % over ±δ): '
                          f'reduce δ or move to a linear zone')
    # the plateaus scatter more than the frame noise predicts: correlated noise or a non-linear drift
    # (the standard error of g comes from the residual, so it already accounts for it: warning only)
    expected = noise_sigma / math.sqrt(max(samples, 1)) if math.isfinite(noise_sigma) else math.nan
    if math.isfinite(expected) and expected > 0 and math.isfinite(sig) and sig > max_residual_ratio * expected:
        r.warnings.append('the plateaus scatter much more than the frame noise (slow drift or unstable '
                          'signal): check g ± its error')
    if r.asymmetry > max_asymmetry:
        r.warnings.append(f'asymmetric response (+ {r.g_plus:.3g} / − {r.g_minus:.3g}): backlash?')
    if period_s and math.isfinite(settle_s) and settle_s > period_s:
        r.warnings.append(f'settling time {settle_s:.3g} s > PID period {period_s:.3g} s: '
                          f'increase "Settle after move"')
    return r


def _usable(r: CalibrationResult) -> bool:
    """A result that can enter a loop gain: accepted, or refused only because the motor has no clear
    effect on this signal (its g ≈ 0 is still the best estimate, e.g. in a decoupled combination).
    A non-linear response is never used."""
    if r.ok:
        return True
    return (math.isfinite(r.g) and any(w.startswith('response not significant') for w in r.warnings)
            and not any(w.startswith('non-linear') for w in r.warnings))


def suggest_pid_settings(results: Dict[str, CalibrationResult], weights: Dict[str, float],
                         alpha: float = 0.5) -> Tuple[Optional[dict], List[str]]:
    """Proposed PID settings from the calibration of its motors.

    The PID moves every motor by ``output × weight``: the loop gain is ``G = Σ weight·g``.
    Loop sign = sign(G), Kp = α / |G| (α = 0.5: half the error corrected per iteration),
    deadband = 2σ, settle = the longest measured settling time.
    Returns (``{'sign_kp', 'kp', 'deadband', 'settle_s'}`` or None, reasons / warnings).
    """
    notes: List[str] = []
    used = {r: w for r, w in weights.items() if w}
    missing = [r for r in used if r not in results]
    bad = [r for r in used if r in results and not _usable(results[r])]
    if missing:
        notes.append(f"not calibrated: {', '.join(missing)}")
    if bad:
        notes.append(f"calibration refused for: {', '.join(bad)}")
    if not used or missing or bad:
        return None, notes
    gain = sum(w * results[r].g for r, w in used.items())
    if not math.isfinite(gain) or gain == 0:
        notes.append('the weighted motors cancel each other (total gain ≈ 0)')
        return None, notes
    # significance of the total loop gain (a motor with no clear effect alone is fine in a combination)
    errs = [w * results[r].stderr for r, w in used.items()]
    if all(math.isfinite(e) for e in errs):
        g_err = math.sqrt(sum(e * e for e in errs))
        if g_err > 0 and abs(gain) < 5.0 * g_err:
            notes.append(f'total loop gain not significant ({gain:.3g} ± {g_err:.2g}): increase δ or '
                         f'check the motors')
            return None, notes
    sigmas = [results[r].noise_sigma for r in used if math.isfinite(results[r].noise_sigma)]
    settles = [results[r].settle_s for r in used if math.isfinite(results[r].settle_s)]
    out = {'sign_kp': '+' if gain > 0 else '-', 'kp': alpha / abs(gain),
           'deadband': 2.0 * max(sigmas) if sigmas else 0.0,
           'settle_s': max(settles) if settles else 0.0}
    for r in used:
        notes.extend(f"{r}: {w}" for w in results[r].warnings)
    return out, notes


def settling_time(frames: List[Tuple[float, float]], t_done: float, level: float, band: float) -> float:
    """Time after ``t_done`` from which every frame stays within ``level ± band``."""
    last_out = None
    for i, (t, v) in enumerate(frames):
        if abs(v - level) > band:
            last_out = i
    if last_out is None:
        return 0.0
    if last_out + 1 < len(frames):
        return max(frames[last_out + 1][0] - t_done, 0.0)
    return max(frames[-1][0] - t_done, 0.0)


class CalibrationAutomation(Automation):
    """Calibrates motors one after the other (the others still): one column of the sensitivity matrix
    each. Every motor is measured on all the signals of the run at once (``signal_role`` + the
    ``extra_signals``, used to decouple several PIDs): the first signal sets the settling time, the
    step amplitude grows only while no signal responds clearly."""
    kind = 'calibration'

    def __init__(self, signal_role: str, motor_roles: List[str], cycles: int = 3, samples: int = 5,
                 noise_samples: int = 20, settle_s: float = 0.0, delta: Optional[float] = None,
                 max_delta: Optional[float] = None, max_doublings: int = 4, min_snr: float = 5.0,
                 period_s: Optional[float] = None, auto_id: str = 'CAL', label: str = 'Calibration',
                 extra_signals: Optional[List[str]] = None):
        super().__init__(auto_id, label)
        self.period_s = 0.05
        self.signal_role = signal_role
        self.signals = [s for s in dict.fromkeys([signal_role] + list(extra_signals or [])) if s]
        self.roles = [r for r in motor_roles if r]
        self.cycles = max(int(cycles), 1)
        self.samples = max(int(samples), 1)
        self.noise_samples = max(int(noise_samples), 2)
        self.settle_s = max(float(settle_s), 0.0)
        self._delta0 = delta
        self._max_delta = max_delta
        self.max_doublings = max(int(max_doublings), 0)
        self.min_snr = float(min_snr)
        self.pid_period_s = period_s
        self.results_by: Dict[str, Dict[str, CalibrationResult]] = {s: {} for s in self.signals}
        self.finished = False
        self.completed = False
        self.sequencer: Optional[MoveSequencer] = None
        self.origins: Dict[str, float] = {}
        self._reset_motor_state()
        self.role_index = 0

    @property
    def results(self) -> Dict[str, CalibrationResult]:
        """Results on the first signal (the signal of the PID): motor -> result."""
        return self.results_by.get(self.signal_role, {})

    def _reset_motor_state(self):
        self.phase = 'idle'           # noise | move | sample | return
        self.delta = math.nan
        self.delta_max = math.nan
        self.doublings = 0
        self.plateaus: Dict[str, List[Plateau]] = {s: [] for s in self.signals}
        self.noise: Dict[str, List[float]] = {s: [] for s in self.signals}
        self.settles: List[float] = []
        self.pattern: List[float] = []
        self.pattern_index = 0
        self.cycles_left = 0
        self._frames: Dict[str, List[Tuple[float, float]]] = {s: [] for s in self.signals}
        self._seq: Dict[str, int] = {s: 0 for s in self.signals}
        self._t_done = 0.0

    # ------------------------------------------------------------------ description
    @property
    def role(self) -> Optional[str]:
        return self.roles[self.role_index] if self.role_index < len(self.roles) else None

    def motor_roles(self) -> List[str]:
        return list(self.roles)

    def signal_roles(self) -> List[str]:
        return list(self.signals)

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy(check_data=True, check_intensity=True, check_rate=False, envelope='explicit')

    def amplitudes(self, core: 'LaserCore', role: str) -> Tuple[float, float]:
        """(first δ, max δ): δ0 = max(resolution, max_step / 4), max δ = max_step (one chunk per move)."""
        lim = core.safety.limits(role)
        max_step = lim.max_step if lim is not None and lim.max_step else None
        res = lim.resolution if lim is not None else 0.0
        dmax = abs(self._max_delta) if self._max_delta else (max_step or math.nan)
        if max_step:
            dmax = min(dmax, max_step)
        d0 = abs(self._delta0) if self._delta0 else max(res, (max_step or dmax) / 4.0)
        return min(d0, dmax), dmax

    def explicit_ranges(self, core: 'LaserCore') -> Optional[Dict[str, Tuple[float, float]]]:
        out = {}
        for role in self.roles:
            module = core.roles.module_for(role)
            pos = core.position(role)
            if module is None or pos is None:
                return None
            _, dmax = self.amplitudes(core, role)
            out[module] = (pos - dmax, pos + dmax)
        return out

    def summary(self) -> str:
        return (f"{self.label}: {', '.join(self.signals)} vs {', '.join(self.roles)} — bipolar steps "
                f"×{self.cycles}, {self.samples} frames per plateau, back to the start position")

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = []
        if not self.signal_role:
            errors.append(f"{self.label}: no signal selected")
        for sig in self.signals:
            if core.roles.channel_for(sig) is None:
                errors.append(f"{self.label}: signal {sig} is not declared on this machine")
        if not self.roles:
            errors.append(f"{self.label}: no motor to calibrate")
        for role in self.roles:
            d0, dmax = self.amplitudes(core, role)
            if not (math.isfinite(dmax) and dmax > 0 and d0 > 0):
                errors.append(f"{self.label}: {role} has no max step (needed for the step amplitude)")
        return errors

    # ------------------------------------------------------------------ lifecycle
    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.results_by = {s: {} for s in self.signals}
        self.finished = False
        self.completed = False
        self.origins = {r: core.position(r) for r in self.roles}
        if any(v is None for v in self.origins.values()):
            return 'unknown motor position'
        self.sequencer = MoveSequencer(core, self)
        self.role_index = 0
        self._begin_motor(core)
        return None

    def on_resume(self, core: 'LaserCore'):
        # frames taken before the hold are not trusted: restart the current plateau
        if self.phase == 'noise':
            self._start_sampling(core, 'noise')
        elif self.phase == 'sample':
            self.phase = 'move'
            self.sequencer.set_targets({self.role: self._target()})

    def is_done(self) -> bool:
        return self.finished

    def _begin_motor(self, core: 'LaserCore'):
        self._reset_motor_state()
        self.delta, self.delta_max = self.amplitudes(core, self.role)
        self.cycles_left = 1                     # first cycle = probe of the amplitude
        self._start_sampling(core, 'noise')

    def _start_sampling(self, core: 'LaserCore', phase: str):
        self.phase = phase
        for sig in self.signals:
            self._frames[sig] = []
            s = core.signals.latest(core.roles.channel_for(sig))
            self._seq[sig] = s.seq if s is not None else 0
        done = core.last_done_t(self.role) if phase == 'sample' else None
        self._t_done = done if done is not None else core.clock.now()

    def _target(self) -> float:
        return self.origins[self.role] + self.pattern[self.pattern_index]

    def _next_cycle(self):
        self.pattern = [self.delta, 0.0, -self.delta, 0.0]
        self.pattern_index = 0
        self.cycles_left -= 1
        self.phase = 'move'
        self.sequencer.set_targets({self.role: self._target()})

    def _new_frames(self, core: 'LaserCore'):
        """Fresh frames of every signal (each one has its own sequence: they can come from different
        detectors at different rates)."""
        for sig in self.signals:
            s = core.signals.latest(core.roles.channel_for(sig))
            if s is not None and s.seq > self._seq[sig] and s.t >= self._t_done:
                self._seq[sig] = s.seq
                self._frames[sig].append((s.t, float(s.value) if is_finite(s.value) else math.nan))

    def tick(self, core: 'LaserCore', now: float):
        if self.phase == 'noise':
            self._new_frames(core)
            if all(len(self._frames[s]) >= self.noise_samples for s in self.signals):
                for sig in self.signals:
                    good = [(t, v) for t, v in self._frames[sig] if math.isfinite(v)]
                    self.noise[sig] = [v for _, v in good]
                    if len(good) >= 2:
                        self.plateaus[sig].append(Plateau(float(np.mean([t for t, _ in good])), 0.0,
                                                          float(np.mean(self.noise[sig])), len(good)))
                self._next_cycle()
            return
        if self.phase == 'move':
            if self.sequencer.step() == MoveSequencer.DONE:
                self._start_sampling(core, 'sample')
            return
        if self.phase == 'sample':
            self._new_frames(core)
            ready = {s: [(t, v) for t, v in self._frames[s] if t >= self._t_done + self.settle_s]
                     for s in self.signals}
            if any(len(p) < self.samples for p in ready.values()):
                return
            self._record(core, ready)
            return
        if self.phase == 'return':
            if self.sequencer.step() == MoveSequencer.DONE:
                self.role_index += 1
                if self.role is None:
                    self.completed = True
                    self.finished = True
                    core.events.publish(EventKind.SCAN, self.id, f"{self.label} finished")
                else:
                    self._begin_motor(core)

    def _noise_sigma(self, sig: str) -> float:
        n = self.noise.get(sig, [])
        return float(np.std(n, ddof=1)) if len(n) >= 2 else math.nan

    @property
    def noise_sigma(self) -> float:
        """Frame noise at rest of the first signal."""
        return self._noise_sigma(self.signal_role)

    def _weak(self, sig: str) -> bool:
        """The current amplitude gives no clear response on ``sig``."""
        fit = fit_sensitivity(self.plateaus[sig], self._noise_sigma(sig), self.samples)
        sigma = self._noise_sigma(sig)
        response = abs(fit.g) * self.delta if math.isfinite(fit.g) else 0.0
        return math.isfinite(sigma) and response < self.min_snr * sigma

    def _record(self, core: 'LaserCore', ready: Dict[str, List[Tuple[float, float]]]):
        pos = core.position(self.role)
        offset = (pos - self.origins[self.role]) if pos is not None else self.pattern[self.pattern_index]
        for sig, plateau in ready.items():
            last = [(t, v) for t, v in plateau[-self.samples:] if math.isfinite(v)]
            if not last:
                continue
            level = float(np.mean([v for _, v in last]))
            self.plateaus[sig].append(Plateau(float(np.mean([t for t, _ in last])), offset, level, len(last)))
            if sig == self.signal_role:
                sigma = self._noise_sigma(sig)
                band = 3.0 * sigma if math.isfinite(sigma) and sigma > 0 else 0.0
                self.settles.append(settling_time([f for f in self._frames[sig] if math.isfinite(f[1])],
                                                  self._t_done, level, band))
        self.pattern_index += 1
        if self.pattern_index < len(self.pattern):
            self.phase = 'move'
            self.sequencer.set_targets({self.role: self._target()})
            return
        # end of a cycle: probe the amplitude (larger only while no signal responds), then R cycles
        if self.cycles_left <= 0 and self.doublings is not None:
            weak = all(self._weak(sig) for sig in self.signals)
            if weak and self.doublings < self.max_doublings and self.delta < self.delta_max:
                self.delta = min(2.0 * self.delta, self.delta_max)
                self.doublings += 1
                self.cycles_left = 1
            else:
                self.doublings = None            # amplitude chosen: measurement cycles
                self.cycles_left = self.cycles
        if self.cycles_left > 0:
            self._next_cycle()
            return
        self._finish_motor(core)

    def _finish_motor(self, core: 'LaserCore'):
        settle = max(self.settles) if self.settles else math.nan
        for sig in self.signals:
            first = sig == self.signal_role
            res = fit_sensitivity(self.plateaus[sig], self._noise_sigma(sig), self.samples,
                                  period_s=self.pid_period_s if first else None, settle_s=settle)
            res.delta = self.delta
            self.results_by[sig][self.role] = res
        res = self.results_by[self.signal_role][self.role]
        verdict = 'ok' if res.ok else 'refused'
        others = ''.join(f"; {sig}: g = {r.g:.4g} ± {r.stderr:.2g}"
                         for sig in self.signals[1:] for r in [self.results_by[sig][self.role]])
        core.events.publish(EventKind.SCAN, self.id,
                            f"{self.label}: {self.role} g = {res.g:.4g} ± {res.stderr:.2g} ({verdict}){others}"
                            + (f" — {'; '.join(res.warnings)}" if res.warnings else ''))
        self.phase = 'return'
        self.sequencer.set_targets({self.role: self.origins[self.role]})

    def progress(self) -> Tuple[int, int]:
        return min(self.role_index, len(self.roles)), len(self.roles)

    def variables(self) -> Dict[str, float]:
        done, total = self.progress()
        return {'motor': float(done), 'total': float(total), 'delta': float(self.delta),
                'plateaus': float(len(self.plateaus.get(self.signal_role, [])))}
