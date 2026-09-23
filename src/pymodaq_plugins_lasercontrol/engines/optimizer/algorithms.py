"""Historical drift-optimiser modes ported behind the pluggable interface (T4.5).

The logic is the one of the former ``OptimizationEngine`` (workers.py), deliberately *not*
improved (see L10): only the plumbing changed. Every former ``_move_relative`` / ``_goto_offset``
followed by ``sleep(dwell)`` + ``_read_all(n)`` became ``cost = yield Measure(offsets, n)``;
moves without reading became ``yield MoveTo(offsets)``. Offsets are relative to the pass start.
"""
from __future__ import annotations

from typing import Dict

import numpy as np

from .base import GeneratorAlgorithm, Measure, MoveTo, OptContext, Report, Status, register_algorithm


def _label(ctx: OptContext, name: str) -> str:
    return f"full optimization, {name}" if ctx.full() else name


# --------------------------------------------------------------------------- Joint (SPSA-like)
def joint_pass(ctx: OptContext):
    motors = ctx.motors
    max_iters, stall_patience = ctx.budget()
    sf, max_backoffs, db = ctx.safety_factor, ctx.max_backoffs, ctx.deadband
    label = 'full optimization' if ctx.full() else 'joint'
    base: Dict[str, float] = {m['role']: 0.0 for m in motors}

    best_cost = yield Measure(dict(base), ctx.probe_samples)
    yield Status(f"Drift optimizer ({label}): starting cost = {best_cost:.4g}")
    stall_count = backoff_count = 0
    for k in range(max_iters):
        decay = 1.0 / (1.0 + 0.5 * k)          # shrink the probe as the pass converges
        deltas, probe = {}, {}
        for m in motors:
            n = m['role']
            deltas[n] = 1.0 if ctx.rng.random() < 0.5 else -1.0
            step = ctx.probe_step(m)
            probe[n] = max(step * decay, step * 0.25)
        plus = {n: base[n] + deltas[n] * probe[n] for n in base}
        minus = {n: base[n] - deltas[n] * probe[n] for n in base}
        cost_plus = yield Measure(plus, ctx.probe_samples)
        cost_minus = yield Measure(minus, ctx.probe_samples)

        if min(cost_plus, cost_minus) > best_cost + sf * db:
            yield MoveTo(dict(base))            # back to baseline: probes degraded the signal
            backoff_count += 1
            yield Status(f"Drift optimizer: probe degraded signal, backed off (iter {k + 1}).")
            if backoff_count >= max_backoffs:
                yield Status("Drift optimizer: repeated degradation, stopping pass early.")
                break
            continue

        if cost_minus < cost_plus - db:         # already at the "-" point
            new_cost = cost_minus
            base = minus
        elif cost_plus < cost_minus - db:
            yield MoveTo(plus)
            new_cost = cost_plus
            base = plus
        else:
            yield MoveTo(dict(base))            # undo back to baseline
            new_cost = best_cost
        yield Report(new_cost)

        if best_cost - new_cost > db:
            best_cost = new_cost
            stall_count = 0
            backoff_count = 0
        else:
            stall_count += 1
            if stall_count >= stall_patience:
                yield Status("Drift optimizer (joint): converged within the noise floor.")
                break
    yield Status(f"Drift optimizer ({label}): pass complete, cost = {best_cost:.4g}")


# --------------------------------------------------------------------------- Sequential
def sequential_pass(ctx: OptContext):
    motors_sorted = sorted(ctx.motors, key=lambda m: m.get('order', 0))
    n_motors = max(len(motors_sorted), 1)
    max_iters, stall_patience = ctx.budget()
    max_iters_per_motor = max(max_iters // n_motors, 2)
    sf, db = ctx.safety_factor, ctx.deadband
    label = _label(ctx, 'sequential')
    base: Dict[str, float] = {m['role']: 0.0 for m in ctx.motors}

    best_cost = yield Measure(dict(base), ctx.probe_samples)
    yield Status(f"Drift optimizer ({label}): starting cost = {best_cost:.4g}")
    for m in motors_sorted:
        name = m['role']
        step = ctx.probe_step(m)
        stall = 0
        for _ in range(max_iters_per_motor):
            plus = dict(base)
            plus[name] = base[name] + step
            minus = dict(base)
            minus[name] = base[name] - step
            cost_plus = yield Measure(plus, ctx.probe_samples)
            cost_minus = yield Measure(minus, ctx.probe_samples)

            if min(cost_plus, cost_minus) > best_cost + sf * db:
                yield MoveTo(dict(base))
                yield Status(f"Drift optimizer: '{name}' probe degraded signal, backed off.")
                break

            if cost_minus < cost_plus - db:
                new_cost = cost_minus
                base = minus
            elif cost_plus < cost_minus - db:
                yield MoveTo(plus)
                new_cost = cost_plus
                base = plus
            else:
                yield MoveTo(dict(base))        # undo back to baseline for this motor
                stall += 1
                step *= 0.5
                if step < ctx.probe_step(m) * 0.1 or stall >= stall_patience:
                    break
                continue

            yield Report(new_cost)
            if best_cost - new_cost > db:
                best_cost = new_cost
                stall = 0
            else:
                stall += 1
                if stall >= stall_patience:
                    break
    yield Status(f"Drift optimizer ({label}): pass complete, cost = {best_cost:.4g}")


# --------------------------------------------------------------------------- Nelder-Mead
def nelder_mead_pass(ctx: OptContext):
    motors = ctx.motors
    names = [m['role'] for m in motors]
    n = len(names)
    max_iters, stall_patience = ctx.budget()
    sf, db = ctx.safety_factor, ctx.deadband
    label = _label(ctx, 'Nelder-Mead')
    ALPHA, GAMMA, RHO, SIGMA = 1.0, 2.0, 0.5, 0.5

    zero = {nm: 0.0 for nm in names}
    offsets = [ctx.clamp(zero)]
    for m in motors:
        v = dict(zero)
        v[m['role']] = ctx.probe_step(m)
        offsets.append(ctx.clamp(v))            # store the actually applied (clamped) vertex
    costs = []
    for v in offsets:
        c = yield Measure(v, ctx.probe_samples)
        costs.append(c)

    best_cost_ever = min(costs)
    yield Status(f"Drift optimizer ({label}): starting cost = {best_cost_ever:.4g}")
    stall_count = 0
    for _ in range(max_iters):
        order = sorted(range(len(offsets)), key=lambda i: costs[i])
        offsets = [offsets[i] for i in order]
        costs = [costs[i] for i in order]

        spread = costs[-1] - costs[0]
        if spread < db:
            stall_count += 1
            if stall_count >= stall_patience:
                yield Status(f"Drift optimizer ({label}): simplex converged within the noise floor.")
                break
        else:
            stall_count = 0

        centroid = {nm: sum(o[nm] for o in offsets[:-1]) / max(n, 1) for nm in names}
        worst = offsets[-1]
        x_r = ctx.clamp({nm: centroid[nm] + ALPHA * (centroid[nm] - worst[nm]) for nm in names})
        cost_r = yield Measure(x_r, ctx.probe_samples)

        if costs[0] <= cost_r < costs[-2 if len(costs) > 1 else -1]:
            offsets[-1], costs[-1] = x_r, cost_r
        elif cost_r < costs[0]:
            x_e = ctx.clamp({nm: centroid[nm] + GAMMA * (x_r[nm] - centroid[nm]) for nm in names})
            cost_e = yield Measure(x_e, ctx.probe_samples)
            if cost_e < cost_r:
                offsets[-1], costs[-1] = x_e, cost_e
            else:
                yield MoveTo(x_r)
                offsets[-1], costs[-1] = x_r, cost_r
        else:
            x_c = ctx.clamp({nm: centroid[nm] + RHO * (worst[nm] - centroid[nm]) for nm in names})
            cost_c = yield Measure(x_c, ctx.probe_samples)
            if cost_c < costs[-1]:
                offsets[-1], costs[-1] = x_c, cost_c
            else:
                best_vertex = offsets[0]
                new_offsets, new_costs = [best_vertex], [costs[0]]
                for k in range(1, len(offsets)):
                    v = ctx.clamp({nm: best_vertex[nm] + SIGMA * (offsets[k][nm] - best_vertex[nm])
                                   for nm in names})
                    c = yield Measure(v, ctx.probe_samples)
                    new_offsets.append(v)
                    new_costs.append(c)
                offsets, costs = new_offsets, new_costs

        best_cost_ever = min(best_cost_ever, min(costs))
        yield Report(min(costs))
        if min(costs) > best_cost_ever + sf * db:
            yield Status(f"Drift optimizer ({label}): simplex degraded, stopping pass early.")
            break

    best_idx = int(np.argmin(costs))
    yield MoveTo(offsets[best_idx])
    yield Status(f"Drift optimizer ({label}): pass complete, cost = {costs[best_idx]:.4g}")


# --------------------------------------------------------------------------- Gradient / Newton-lite
def gradient_pass(ctx: OptContext):
    motors = ctx.motors
    names = [m['role'] for m in motors]
    if not motors:
        return
    max_iters, stall_patience = ctx.budget()
    sf, max_backoffs, db = ctx.safety_factor, ctx.max_backoffs, ctx.deadband
    damping = max(float(ctx.config.get('damping_factor', 1.0)), 0.0)
    grad_samples = max(int(ctx.config.get('gradient_samples', 4)), 1)
    label = _label(ctx, 'gradient/Newton')

    center = {nm: 0.0 for nm in names}
    best_cost = yield Measure(dict(center), grad_samples)
    yield Status(f"Drift optimizer ({label}): starting cost = {best_cost:.4g}")
    stall_count = backoff_count = 0
    for it in range(max_iters):
        steps = {}
        for m in motors:
            name = m['role']
            h = ctx.probe_step(m)
            plus = dict(center)
            plus[name] = center[name] + h
            plus = ctx.clamp(plus)
            h_plus = plus[name] - center[name]
            cost_plus = yield Measure(plus, grad_samples)
            minus = dict(center)
            minus[name] = center[name] - h
            minus = ctx.clamp(minus)
            h_minus = center[name] - minus[name]
            cost_minus = yield Measure(minus, grad_samples)
            yield MoveTo(dict(center))          # back to center

            h_eff = max((h_plus + h_minus) / 2.0, 1e-9)
            grad = (cost_plus - cost_minus) / (2.0 * h_eff)
            curvature = (cost_plus - 2.0 * best_cost + cost_minus) / (h_eff ** 2)
            if curvature > 0:
                newton_step = -grad / curvature
                if abs(newton_step) <= ctx.NEWTON_STEP_SANITY_MULT * h_eff:
                    raw_step = damping * newton_step
                else:
                    raw_step = -damping * h_eff * float(np.sign(grad))
            else:
                raw_step = -damping * h_eff * float(np.sign(grad))
            steps[name] = float(np.clip(raw_step, -m['max_step'], m['max_step']))

        target = ctx.clamp({nm: center[nm] + steps[nm] for nm in names})
        new_cost = yield Measure(target, grad_samples)
        yield Report(new_cost)

        if new_cost > best_cost + sf * db:
            yield Status(f"Drift optimizer ({label}): step degraded signal, reverting (iter {it + 1}).")
            yield MoveTo(dict(center))
            backoff_count += 1
            if backoff_count >= max_backoffs:
                yield Status(f"Drift optimizer ({label}): repeated degradation, stopping pass early.")
                break
            continue

        backoff_count = 0
        improvement = best_cost - new_cost
        center = dict(target)
        if improvement > db:
            best_cost = new_cost
            stall_count = 0
        else:
            best_cost = min(best_cost, new_cost)
            stall_count += 1
            if stall_count >= stall_patience:
                yield Status(f"Drift optimizer ({label}): converged within the noise floor.")
                break
    yield Status(f"Drift optimizer ({label}): pass complete, cost = {best_cost:.4g}")


register_algorithm('joint', 'Joint (all motors at once)',
                   lambda: GeneratorAlgorithm('joint', 'Joint (all motors at once)', joint_pass))
register_algorithm('sequential', 'Sequential (one motor at a time)',
                   lambda: GeneratorAlgorithm('sequential', 'Sequential (one motor at a time)', sequential_pass))
register_algorithm('nelder-mead', 'Nelder-Mead (simplex)',
                   lambda: GeneratorAlgorithm('nelder-mead', 'Nelder-Mead (simplex)', nelder_mead_pass))
register_algorithm('gradient', 'Gradient / Newton-lite (local descent)',
                   lambda: GeneratorAlgorithm('gradient', 'Gradient / Newton-lite (local descent)', gradient_pass))

RUN_MODES = ['Joint (all motors at once)', 'Sequential (one motor at a time)', 'Nelder-Mead (simplex)',
             'Gradient / Newton-lite (local descent)']
