"""Decoupling of several PIDs that share motors (docs/pid_roadmap.md § 3, identification level a).

The calibration of every motor on every signal gives the sensitivity matrix ``J`` (signals × motors):
``Δs = J·Δm``. A PID moves its motors by ``output × weights``. With the weights ``w_k`` = column k of
the pseudo-inverse of J, the PID of signal k moves **only** signal k (``J·w_k ∝ e_k``): the PIDs stop
fighting each other and each one can be tuned alone (Kp from its own loop gain ``(J·w_k)_k``).

Pure numpy, no Qt: :func:`decouple` takes the calibration results
(``{signal: {motor: CalibrationResult}}``) and returns the proposed weights with their quality.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .calibration import CalibrationResult

#: condition number above which the decoupling is refused (two motors do almost the same thing)
MAX_COND = 100.0
#: above this condition number the decoupling works but one direction is weakly controllable
WARN_COND = 10.0


@dataclass
class DecouplingResult:
    signals: List[str]
    motors: List[str]
    J: np.ndarray                                   # sensitivity matrix, signals × motors
    J_err: np.ndarray                               # standard errors of J
    weights: Dict[str, Dict[str, float]] = field(default_factory=dict)   # signal -> motor -> weight
    gains: Dict[str, float] = field(default_factory=dict)         # signal -> loop gain (J·w_k)_k
    crosstalk: Dict[str, float] = field(default_factory=dict)     # signal -> max |(J·w_k)_j| / |(J·w_k)_k|
    crosstalk_err: Dict[str, float] = field(default_factory=dict)  # same, expected from the errors of J
    cond: float = math.nan
    ok: bool = False
    notes: List[str] = field(default_factory=list)


def sensitivity_matrix(results_by: Dict[str, Dict[str, CalibrationResult]], signals: List[str],
                       motors: List[str]) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """(J, standard errors, missing 'signal/motor' pairs). Off-diagonal terms are used even when the
    single-signal check refused them as 'not significant': a coupling close to 0 is a valid value."""
    J = np.full((len(signals), len(motors)), np.nan)
    E = np.full_like(J, np.nan)
    missing = []
    for i, s in enumerate(signals):
        for j, m in enumerate(motors):
            r = (results_by.get(s) or {}).get(m)
            if r is None or not math.isfinite(r.g):
                missing.append(f'{s}/{m}')
                continue
            J[i, j] = r.g
            E[i, j] = r.stderr if math.isfinite(r.stderr) else 0.0
    return J, E, missing


def crosstalk(J: np.ndarray, w: np.ndarray, k: int) -> float:
    """Largest response of the other signals relative to signal k, for the motor weights ``w``."""
    resp = J @ w
    if resp[k] == 0 or len(resp) < 2:
        return math.inf if resp[k] == 0 else 0.0
    others = np.delete(np.abs(resp), k)
    return float(others.max() / abs(resp[k]))


def decouple(results_by: Dict[str, Dict[str, CalibrationResult]], signals: List[str], motors: List[str],
             max_cond: float = MAX_COND, decimals: int = 4) -> DecouplingResult:
    """Weights of each PID (one per signal) that move only its own signal.

    ``w_k`` = column k of ``J⁺`` (minimum-norm when there are more motors than signals), scaled so that
    its largest weight is ±1 (the max step of the PID then applies to the motor that moves most, as with
    a single motor), rounded to ``decimals`` (the precision of the weight fields).
    """
    J, E, missing = sensitivity_matrix(results_by, signals, motors)
    res = DecouplingResult(list(signals), list(motors), J, E)
    if len(signals) < 2:
        res.notes.append('choose at least two PIDs (two signals) to decouple')
        return res
    if missing:
        res.notes.append(f"not calibrated: {', '.join(missing)}")
        return res
    if len(motors) < len(signals):
        res.notes.append(f'{len(motors)} motors for {len(signals)} signals: at least as many motors as '
                         f'signals are needed')
        return res
    sv = np.linalg.svd(J, compute_uv=False)
    res.cond = float(sv[0] / sv[-1]) if sv[-1] > 0 else math.inf
    if not res.cond <= max_cond:
        res.notes.append(f'condition number {res.cond:.3g} > {max_cond:g}: two motors (or two signals) do '
                         f'almost the same thing — remove one or add another motor')
        return res
    if res.cond > WARN_COND:
        res.notes.append(f'condition number {res.cond:.3g}: one direction needs large moves of the motors '
                         f'(weakly controllable)')
    W = np.linalg.pinv(J)                                  # motors × signals, J·W = I
    for k, s in enumerate(signals):
        w = W[:, k]
        w = np.round(w / np.max(np.abs(w)), decimals)
        res.weights[s] = {m: float(v) for m, v in zip(motors, w)}
        res.gains[s] = float((J @ w)[k])
        res.crosstalk[s] = crosstalk(J, w, k)
        # first order: an error δJ on the matrix leaves a coupling δJ·w on the other signals
        err = np.sqrt((E ** 2) @ (w ** 2))
        res.crosstalk_err[s] = float(np.delete(err, k).max() / abs(res.gains[s])) if len(signals) > 1 else 0.0
    res.ok = True
    for s in signals:
        if res.crosstalk_err[s] > 0.2:
            res.notes.append(f'{s}: the calibration errors allow ~{100 * res.crosstalk_err[s]:.0f} % of '
                             f'remaining coupling (calibrate again with more cycles or a larger step)')
    return res


def current_crosstalk(J: np.ndarray, signals: List[str], motors: List[str],
                      weights: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Coupling of each PID with its present weights (to compare with the proposal)."""
    out = {}
    for k, s in enumerate(signals):
        w = np.array([float((weights.get(s) or {}).get(m, 0.0)) for m in motors])
        out[s] = crosstalk(J, w, k) if np.any(w) else math.nan
    return out


def format_matrix(res: DecouplingResult, current: Optional[Dict[str, Dict[str, float]]] = None) -> str:
    """Plain text report: matrix ± errors, condition number, weights, coupling before / after."""
    motors, signals = res.motors, res.signals
    colw = max([16] + [len(m) + 2 for m in motors])         # '-0.0979±0.05' + spacing
    namew = max([8] + [len(s) + 2 for s in signals])
    lines = ['Sensitivity (signal units per motor unit):',
             ' ' * namew + ''.join(f'{m:>{colw}}' for m in motors)]
    for i, s in enumerate(signals):
        cells = ''.join(f"{f'{res.J[i, j]:+.3g}±{res.J_err[i, j]:.1g}':>{colw}}" if math.isfinite(res.J[i, j])
                        else f"{'—':>{colw}}" for j in range(len(motors)))
        lines.append(f'{s:<{namew}}' + cells)
    if math.isfinite(res.cond):
        lines.append(f'condition number κ = {res.cond:.3g}')
    if res.weights:
        before = current_crosstalk(res.J, signals, motors, current) if current else {}
        lines.append('')
        lines.append('Proposed weights (each PID moves only its own signal):')
        lines.append(' ' * namew + ''.join(f'{m:>{colw}}' for m in motors) + '   coupling now → after')
        for s in signals:
            b = before.get(s, math.nan)
            now = f'{100 * b:.0f} %' if math.isfinite(b) else '—'
            lines.append(f'{s:<{namew}}' + ''.join(f'{res.weights[s][m]:>{colw}.4f}' for m in motors)
                         + f'   {now} → {100 * res.crosstalk[s]:.1f} % (±{100 * res.crosstalk_err[s]:.0f} %)')
    lines.extend(f'⚠ {n}' for n in res.notes)
    return '\n'.join(lines)
