"""Robust extremum seeking whose only goal is to CONFIRM that we are at the maximum (docs/roadmap.md § 3).

The absolute value of the signal is never compared (it changes for other physical reasons): only "is the
top here?". For each motor, plateaus are measured at the offsets ``0, +δ, −δ, −δ, +δ, 0`` around the
centre (the symmetric order cancels a linear drift), and fitted with

    cost(x, k) = a + g·x + h·x² + d·k          (x = offset, k = index of the plateau: linear drift)

using the known noise of a plateau (σ of the cost / √samples, measured at the start of the pass) for the
errors of g and h. The cost of the optimiser is minimised (Maximize → cost = −signal / anchor), so a
maximum of the signal is a minimum of the cost:

* gradient not significant (|g| < z·σ_g) and curvature significantly up (h > z_h·σ_h) → ✔ at maximum;
* gradient significant → one step downhill on the cost (Newton −g / 2h when the curvature is known,
  otherwise δ), bounded by the max step, then probe again;
* flat (neither significant) → the probe is too small for the noise: δ × 1.5 (up to the max step).

At most ``confirm_tries`` probes per motor; the result (confirmed or not, per motor) is written in
``ctx.result`` for the caller (a routine waits for the operator when it is not confirmed).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

from .base import GeneratorAlgorithm, Measure, MoveTo, OptContext, Report, Status, register_algorithm

PATTERN = (0.0, 1.0, -1.0, -1.0, 1.0, 0.0)          # in units of δ


@dataclass
class ProbeFit:
    g: float
    se_g: float
    h: float
    se_h: float
    drift: float
    sigma: float                                    # noise of one plateau used for the errors

    @property
    def top(self) -> float:
        """Offset of the top from the probe centre (−g / 2h), nan when there is no top (h ≤ 0)."""
        return -self.g / (2 * self.h) if self.h > 0 else math.nan

    @property
    def loss(self) -> float:
        """Relative signal lost by being at the centre instead of the top (cost normalised by the signal)."""
        return self.h * self.top ** 2 if self.h > 0 else math.nan

    def verdict(self, z: float = 3.0, z_h: float = 2.0, max_loss: float = 0.0) -> str:
        """'max' | 'slope' | 'flat' (cost minimised: a maximum of the signal has h > 0).
        At the maximum: the curvature is that of a top, and the gradient is not significant or the
        estimated loss (h·top²) is at most ``max_loss`` (the top moves too: within tolerance is enough)."""
        curved = self.h > z_h * self.se_h
        if abs(self.g) >= z * self.se_g:
            return 'max' if curved and max_loss > 0 and self.loss <= max_loss else 'slope'
        return 'max' if curved else 'flat'


def fit_probe(offsets: Sequence[float], costs: Sequence[float], sigma: Optional[float] = None) -> ProbeFit:
    """Least squares fit of cost = a + g·x + h·x² + drift(k) (k = index of the plateau; linear drift, and
    quadratic too when there are ≥ 10 plateaus). ``sigma`` = noise of one plateau from the frame noise; the
    error used is the larger of it and of the residual scatter (≥ 3 degrees of freedom), so a slow wander
    of the signal (the top moving, other physical causes) makes the decision more careful, never wrong."""
    x = np.asarray(offsets, dtype=float)
    y = np.asarray(costs, dtype=float)
    k = np.arange(len(x), dtype=float)
    k = (k - k.mean()) / max(k.std(), 1.0)
    cols = [np.ones_like(x), x, x ** 2, k] + ([k ** 2] if len(x) >= 10 else [])
    A = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    dof = len(y) - A.shape[1]
    resid = y - A @ coef
    s_res = float(np.sqrt(resid @ resid / dof)) if dof > 0 else math.nan
    known = sigma if sigma is not None and math.isfinite(sigma) and sigma > 0 else math.nan
    if dof >= 3 and math.isfinite(s_res):
        sigma = max(s_res, known) if math.isfinite(known) else s_res
    elif math.isfinite(known):
        sigma = known
    else:
        sigma = s_res if math.isfinite(s_res) else math.inf
    cov = np.linalg.pinv(A.T @ A) * sigma ** 2
    return ProbeFit(g=float(coef[1]), se_g=float(math.sqrt(max(cov[1, 1], 0.0))), h=float(coef[2]),
                    se_h=float(math.sqrt(max(cov[2, 2], 0.0))), drift=float(coef[3]), sigma=float(sigma))


def confirm_pass(ctx: OptContext):
    cfg = ctx.config
    tries = max(int(cfg.get('confirm_tries', 5)), 1)
    z = float(cfg.get('confirm_z', 3.0))
    max_loss = float(cfg.get('confirm_max_loss', 0.01))     # "at the maximum" = within 1 % of the signal
    samples = max(ctx.probe_samples, int(cfg.get('confirm_samples', 5)))     # plateaus averaged on ≥ 5 frames
    cycles = max(int(cfg.get('confirm_cycles', 2)), 1)                         # the + − − + pattern, n times
    pattern = PATTERN[:-1] * cycles + PATTERN[-1:]
    sigma_plateau = getattr(ctx, 'cost_sigma', math.nan) / math.sqrt(samples)
    center = {m['role']: 0.0 for m in ctx.motors}
    delta = {m['role']: ctx.probe_step(m) for m in ctx.motors}
    result: Dict[str, dict] = {}
    ctx.result = {'confirmed': False, 'motors': result, 'message': ''}
    for m in ctx.motors:
        role, max_step = m['role'], float(m['max_step'])
        verdict = 'flat'
        for attempt in range(1, tries + 1):
            costs = []
            for u in pattern:
                # the centre stays within ± max step of the start; the probes go ± δ around it (never
                # truncated: a truncated side would make the pattern asymmetric = a false gradient). The
                # S3 / S4 limits of the core still apply to every move.
                off = dict(center)
                off[role] = center[role] + u * delta[role]
                costs.append((yield Measure(off, samples)))
            fit = fit_probe([u * delta[role] for u in pattern], costs, sigma_plateau)
            verdict = fit.verdict(z, max_loss=max_loss)
            yield Report(min(costs))
            yield Status(f'{role}: g = {fit.g:.3g} ± {fit.se_g:.2g}, curvature {fit.h:.3g} ± {fit.se_h:.2g} → '
                         f'{ {"max": "✔ at maximum", "slope": "slope: step towards the top", "flat": "flat: larger probe"}[verdict] }')
            result[role] = {'verdict': verdict, 'g': fit.g, 'se_g': fit.se_g, 'h': fit.h, 'se_h': fit.se_h,
                            'loss': fit.loss, 'delta': delta[role], 'tries': attempt, 'offset': center[role]}
            if verdict == 'max':
                break
            if verdict == 'flat':
                delta[role] = min(delta[role] * 1.5, max_step)
                continue
            step = -fit.g / (2 * fit.h) if fit.h > z * fit.se_h else -math.copysign(delta[role], fit.g)
            step = max(-max_step, min(max_step, step))
            center[role] = max(-max_step, min(max_step, center[role] + step))
            yield MoveTo(dict(center))
            result[role]['offset'] = center[role]
        if verdict != 'max':
            ctx.result['message'] = f'{role}: maximum not confirmed after {tries} probes ({verdict})'
            yield MoveTo(dict(center))
            yield Status('✖ ' + ctx.result['message'])
            return
    ctx.result['confirmed'] = True
    ctx.result['message'] = 'at maximum (' + ', '.join(f"{r} moved {v['offset']:+.3g}" for r, v in result.items()) + ')'
    yield MoveTo(dict(center))
    yield Status('✔ ' + ctx.result['message'])


LABEL = 'Confirm maximum (extremum seeking)'
register_algorithm('confirm', LABEL, lambda: GeneratorAlgorithm('confirm', LABEL, confirm_pass))
