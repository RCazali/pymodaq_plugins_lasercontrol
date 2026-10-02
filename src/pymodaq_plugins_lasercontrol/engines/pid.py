"""PID regulation of one or several 0D signals with combinations of motors (T4.1, T4.2).

Pure math in :class:`PIDController` (testable without Qt/hardware), adapter in
:class:`PIDAutomation` (driven by the core tick, moves through the broker).

Kept from the historical PIDEngine: signed gains, anti-windup (|ki * integral| <= max_step),
max step applied *after* weighting (the whole step vector is scaled, its direction kept), "confirm
moves" mode. A PID may regulate several signals at once (one loop each, see :class:`PIDAutomation`).
New (specification): skip the iteration while a motor is still moving (no stacking),
one fresh sample per iteration (never re-use a value), integral reset on resume, no derivative
kick at start, optional error deadband and settle time after a move.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Tuple

from ..core.automation import Automation
from ..core.safety import SafetyPolicy
from ..core.signals import is_finite

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore


def _sign(s) -> float:
    return -1.0 if str(s).strip() == '-' else 1.0


@dataclass
class PIDSettings:
    setpoint: float = 0.0
    setpoint_mode: str = 'fixed'      # 'fixed' | 'capture' (setpoint = value at each start)
    kp: float = 0.1
    sign_kp: str = '+'
    ki: float = 0.01
    sign_ki: str = '+'
    kd: float = 0.0
    sign_kd: str = '+'
    max_step: float = 1.0             # per motor and per iteration, after weighting
    period_s: float = 0.5             # 2 Hz by default
    deadband: float = 0.0             # |error| below -> no move, integral frozen
    settle_s: float = 0.0             # wait after a move before using a new sample
    confirm_moves: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'PIDSettings':
        d = dict(d or {})
        if 'sample_rate' in d and 'period_s' not in d:          # legacy keys
            d['period_s'] = d.pop('sample_rate')
        if 'confirm_before_move' in d and 'confirm_moves' not in d:
            d['confirm_moves'] = d.pop('confirm_before_move')
        s = cls()
        for k in asdict(s):
            if k in d and d[k] is not None:
                cur = getattr(s, k)
                try:
                    val = type(cur)(d[k]) if not isinstance(cur, bool) else bool(d[k])
                except (TypeError, ValueError):
                    continue
                setattr(s, k, val)
        if s.setpoint_mode not in ('fixed', 'capture'):
            s.setpoint_mode = 'fixed'
        return s

    def gains(self) -> Tuple[float, float, float]:
        return (abs(self.kp) * _sign(self.sign_kp), abs(self.ki) * _sign(self.sign_ki),
                abs(self.kd) * _sign(self.sign_kd))


@dataclass
class PIDTerms:
    pv: float = math.nan
    setpoint: float = math.nan
    error: float = math.nan
    p: float = 0.0
    i: float = 0.0
    d: float = 0.0
    output: float = 0.0
    in_deadband: bool = False


@dataclass
class TracePoint:
    """One PID iteration, for the live plots and the statistics of the PID tab."""
    t: float                  # wall clock time
    pv: float
    setpoint: float
    error: float
    p: float
    i: float
    d: float
    output: float
    moved: float = 0.0        # sum of |step| actually sent at this iteration (0: no move)


def error_stats(points: List[TracePoint], since: float) -> Tuple[float, float, int]:
    """(RMS of the error, standard deviation of the measurement, number of points) over the points
    with ``t >= since``. NaN when there is no point."""
    pts = [p for p in points if p.t >= since and math.isfinite(p.error)]
    if not pts:
        return math.nan, math.nan, 0
    n = len(pts)
    rms = math.sqrt(sum(p.error ** 2 for p in pts) / n)
    mean = sum(p.pv for p in pts) / n
    std = math.sqrt(sum((p.pv - mean) ** 2 for p in pts) / n)
    return rms, std, n


class PIDController:
    def __init__(self, settings: PIDSettings):
        self.settings = settings
        self.integral = 0.0
        self.prev_error: Optional[float] = None
        self.last = PIDTerms()

    def reset(self):
        self.integral = 0.0
        self.prev_error = None
        self.last = PIDTerms()

    def update(self, pv: float, dt: float) -> PIDTerms:
        s = self.settings
        kp, ki, kd = s.gains()
        dt = max(float(dt), 1e-3)
        max_step = abs(s.max_step)
        error = s.setpoint - pv
        if abs(error) <= abs(s.deadband):
            self.prev_error = error
            self.last = PIDTerms(pv, s.setpoint, error, 0.0, ki * self.integral, 0.0, 0.0, True)
            return self.last
        derivative = 0.0 if self.prev_error is None else (error - self.prev_error) / dt
        self.integral += error * dt
        if abs(ki) > 1e-12:                                   # anti-windup
            lim = max_step / abs(ki)
            self.integral = max(-lim, min(lim, self.integral))
        p, i, d = kp * error, ki * self.integral, kd * derivative
        self.prev_error = error
        self.last = PIDTerms(pv, s.setpoint, error, p, i, d, p + i + d, False)
        return self.last

    def scaled(self, output: float, weights: Dict[str, float]) -> Dict[str, float]:
        """Per motor step = output * weight, the whole vector scaled down (not clipped motor by motor)
        so that no step exceeds max_step: the direction is kept, which a decoupled combination needs."""
        steps = {r: output * float(w) for r, w in weights.items() if w}
        m = abs(self.settings.max_step)
        big = max((abs(v) for v in steps.values()), default=0.0)
        k = m / big if big > m else 1.0
        return {r: v * k for r, v in steps.items() if v != 0.0 and is_finite(v)}

    def split(self, output: float, weights: Dict[str, float]) -> Dict[str, float]:
        """Per motor step = clip(output * weight, +/- max_step): the clamp is applied AFTER the
        weighting so that a large weight can never exceed max_step (historical bug fix kept)."""
        m = abs(self.settings.max_step)
        out = {}
        for role, w in weights.items():
            if not w:
                continue
            step = max(-m, min(m, output * float(w)))
            if step != 0.0 and is_finite(step):
                out[role] = step
        return out


#: common settings of a PID (the same for all its loops); the others are per loop (signal)
COMMON_KEYS = ('max_step', 'period_s', 'settle_s', 'confirm_moves')
LOOP_KEYS = ('setpoint', 'setpoint_mode', 'sign', 'kp', 'ki', 'kd', 'deadband')


def default_loop(signal: str = '') -> dict:
    s = PIDSettings()
    return {'signal': signal, 'setpoint': s.setpoint, 'setpoint_mode': s.setpoint_mode, 'sign': '+',
            'kp': s.kp, 'ki': s.ki, 'kd': s.kd, 'deadband': s.deadband, 'weights': {}}


def normalize_loop(d: dict) -> dict:
    out = default_loop()
    d = dict(d or {})
    st = PIDSettings.from_dict({k: d[k] for k in ('setpoint', 'setpoint_mode', 'kp', 'ki', 'kd', 'deadband')
                                if k in d})
    out.update(signal=str(d.get('signal') or ''), setpoint=st.setpoint, setpoint_mode=st.setpoint_mode,
               sign='-' if str(d.get('sign', '+')).strip() == '-' else '+', kp=abs(st.kp), ki=abs(st.ki),
               kd=abs(st.kd), deadband=abs(st.deadband),
               weights={str(r): float(w) for r, w in (d.get('weights') or {}).items()})
    return out


def loop_settings(loop: dict, common: dict) -> PIDSettings:
    """The :class:`PIDSettings` of one loop: its own gains / setpoint + the common settings of the PID."""
    d = {k: loop[k] for k in ('setpoint', 'setpoint_mode', 'kp', 'ki', 'kd', 'deadband') if k in loop}
    d.update({k: common[k] for k in COMMON_KEYS if k in (common or {})})
    sign = loop.get('sign', '+')
    d.update(sign_kp=sign, sign_ki=sign, sign_kd=sign)
    return PIDSettings.from_dict(d)


class PIDLoop:
    """One regulated signal of a PID: its controller, its motor combination (weights) and its trace."""

    def __init__(self, signal_role: str, weights: Dict[str, float], settings: PIDSettings):
        self.signal_role = signal_role
        self.weights: Dict[str, float] = {r: float(w) for r, w in (weights or {}).items()}
        self.settings = settings
        self.controller = PIDController(settings)
        self.trace: Deque[TracePoint] = deque(maxlen=4000)
        self.last_sample_seq = 0
        self.iterations = 0
        self.last_moves: Dict[str, float] = {}

    def set_settings(self, settings: PIDSettings):
        self.settings = settings
        self.controller.settings = settings

    def active_weights(self) -> Dict[str, float]:
        return {r: w for r, w in self.weights.items() if w}

    def prepare(self, core: 'LaserCore') -> Optional[str]:
        self.controller.reset()
        self.iterations = 0
        self.last_moves = {}
        if self.settings.setpoint_mode == 'capture':
            v = core.signals.value(core.roles.channel_for(self.signal_role))
            if not is_finite(v):
                return f"{self.signal_role}: cannot capture the setpoint (invalid signal)"
            self.settings.setpoint = float(v)
        self.last_sample_seq = 0          # first iteration may use the current (fresh) sample
        return None

    def on_resume(self, core: 'LaserCore'):
        self.controller.reset()            # O3-A / O10-A: integral reset at resume
        s = core.signals.latest(core.roles.channel_for(self.signal_role))
        self.last_sample_seq = s.seq if s is not None else 0     # wait for a sample taken after the resume

    def iterate(self, core: 'LaserCore', roles: List[str]) -> Optional[Tuple[PIDTerms, TracePoint]]:
        """One regulation step if a new valid sample is there, taken after the end of the last move of
        ``roles`` + settle (None otherwise). Updates the controller and the trace."""
        sample = core.signals.latest(core.roles.channel_for(self.signal_role))
        if sample is None or sample.seq <= self.last_sample_seq:
            return None                       # wait for a new frame (no double counting)
        last_done = [t for t in (core.last_done_t(r) for r in roles) if t is not None]
        if last_done and sample.t < max(last_done) + self.settings.settle_s:
            return None                       # sample older than the end of the last move + settle
        if not is_finite(sample.value):
            return None                       # S12 (the core holds on stale/invalid data anyway)
        self.last_sample_seq = sample.seq
        terms = self.controller.update(sample.value, self.settings.period_s)
        self.iterations += 1
        point = TracePoint(core.clock.wall(), terms.pv, terms.setpoint, terms.error, terms.p, terms.i, terms.d,
                           terms.output)
        self.trace.append(point)
        return terms, point


class PIDAutomation(Automation):
    """A PID: one or several signals (loops), several motors (automation ``PID:<id>``).

    Each loop has its own setpoint, gains and motor combination (weights); the common settings (max step,
    period, settle, confirmation) are the same for all. At each period every loop with a fresh sample
    computes its output; the contributions ``output × weights`` (each scaled down as a whole to the max
    step, so that its direction is kept) are summed, the sum is scaled the same way, and **one order per
    motor** is sent. With decoupled weights (``engines/decoupling.py``: columns of J⁺) each loop moves only
    its own signal, so near field and far field (for instance) are corrected at once without fighting.
    With a single loop this is the classic PID of one signal with a combination of motors.
    """
    kind = 'pid'

    def __init__(self, pid_id: str, signal_role: str = '', motors: Optional[Dict[str, float]] = None,
                 settings: Optional[PIDSettings] = None, label: str = '', in_stabilization: bool = True,
                 loops: Optional[List[PIDLoop]] = None):
        super().__init__(f'PID:{pid_id}', label or f'PID {pid_id}')
        self.pid_id = pid_id
        self.loops: List[PIDLoop] = list(loops) if loops else [PIDLoop(signal_role, motors or {},
                                                                        settings or PIDSettings())]
        self.in_stabilization = in_stabilization
        self.period_s = self._period()
        self.iterations = 0
        self.skipped_busy = 0
        self.last_moves: Dict[str, float] = {}

    @classmethod
    def from_config(cls, cfg: dict) -> 'PIDAutomation':
        """From a preset PID (``loops``, ``settings`` = common settings)."""
        common = cfg.get('settings', {}) or {}
        loops = [PIDLoop(l.get('signal', ''), l.get('weights', {}), loop_settings(l, common))
                 for l in cfg.get('loops', []) or [default_loop()]]
        return cls(cfg['id'], label=cfg.get('label') or f"PID {cfg['id']}",
                   in_stabilization=cfg.get('in_stabilization', True), loops=loops)

    def configure(self, cfg: dict, structure: bool = True):
        """Apply a preset PID. Live (engaged): only gains, setpoints and common settings (integrals kept);
        ``structure`` (stopped only): signals and weights too (traces kept per signal)."""
        common = cfg.get('settings', {}) or {}
        loops_cfg = cfg.get('loops', []) or []
        self.label = cfg.get('label') or f"PID {self.pid_id}"
        self.in_stabilization = cfg.get('in_stabilization', True)
        if structure:
            old = {l.signal_role: l for l in self.loops}
            new = []
            for lc in loops_cfg:
                loop = PIDLoop(lc.get('signal', ''), lc.get('weights', {}), loop_settings(lc, common))
                prev = old.get(loop.signal_role)
                if prev is not None:
                    loop.trace = prev.trace
                new.append(loop)
            self.loops = new or [PIDLoop('', {}, PIDSettings())]
        else:
            for loop, lc in zip(self.loops, loops_cfg):
                if lc.get('signal', '') == loop.signal_role:
                    loop.set_settings(loop_settings(lc, common))
        self.period_s = self._period()

    # ------------------------------------------------------------------ single-loop view (compatibility)
    @property
    def loop(self) -> PIDLoop:
        return self.loops[0]

    @property
    def signal_role(self) -> str:
        return self.loop.signal_role

    @signal_role.setter
    def signal_role(self, v: str):
        self.loop.signal_role = v

    @property
    def motors(self) -> Dict[str, float]:
        return self.loop.weights

    @motors.setter
    def motors(self, v: Dict[str, float]):
        self.loop.weights = {r: float(w) for r, w in (v or {}).items()}

    @property
    def settings(self) -> PIDSettings:
        return self.loop.settings

    @property
    def controller(self) -> PIDController:
        return self.loop.controller

    @property
    def trace(self) -> Deque[TracePoint]:
        return self.loop.trace

    def set_settings(self, settings: PIDSettings):
        """Live update of the first loop (gains, setpoint...). The controller keeps its integral."""
        self.loop.set_settings(settings)
        self.period_s = self._period()

    def loop_of(self, signal_role: str) -> Optional[PIDLoop]:
        return next((l for l in self.loops if l.signal_role == signal_role), None)

    def errors(self) -> List[float]:
        return [l.controller.last.error for l in self.loops]

    def _period(self) -> float:
        return max((l.settings.period_s for l in self.loops), default=0.5)

    # ------------------------------------------------------------------ description
    def motor_roles(self) -> List[str]:
        return list(dict.fromkeys(r for l in self.loops for r, w in l.weights.items() if w))

    def signal_roles(self) -> List[str]:
        return [l.signal_role for l in self.loops if l.signal_role]

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy(check_data=True, check_intensity=True, check_rate=True, envelope='excursion')

    def summary(self) -> str:
        parts = []
        for l in self.loops:
            mot = ', '.join(f"{r}×{w:+g}" for r, w in l.weights.items() if w)
            sp = 'value at start' if l.settings.setpoint_mode == 'capture' else f"{l.settings.setpoint:.6g}"
            parts.append(f"{l.signal_role} → setpoint {sp} ; motors {mot}")
        return (f"{self.label}: " + ' | '.join(parts) +
                f" ; max step {self.loop.settings.max_step:g}/iteration ; period {self._period():g} s")

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = []
        sigs = self.signal_roles()
        if len(sigs) != len(self.loops):
            errors.append(f"{self.label}: a signal is not selected")
        if len(set(sigs)) != len(sigs):
            errors.append(f"{self.label}: the same signal is used twice")
        for l in self.loops:
            if not l.active_weights():
                errors.append(f"{self.label}: {l.signal_role or 'a signal'} has no motor (non-zero weight)")
        s = self.loop.settings
        if s.max_step <= 0:
            errors.append(f"{self.label}: max step must be > 0")
        if s.period_s <= 0:
            errors.append(f"{self.label}: period must be > 0")
        return errors

    # ------------------------------------------------------------------ lifecycle
    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.iterations = 0
        self.skipped_busy = 0
        self.last_moves = {}
        self.period_s = self._period()
        for l in self.loops:
            err = l.prepare(core)
            if err:
                return f"{self.label}: {err}"
        return None

    def on_resume(self, core: 'LaserCore'):
        for l in self.loops:
            l.on_resume(core)

    # ------------------------------------------------------------------ loop
    def tick(self, core: 'LaserCore', now: float):
        self.period_s = self._period()
        roles = self.motor_roles()
        if core.any_busy(roles):
            self.skipped_busy += 1            # a move is not finished: skip, never stack (T4.1)
            return
        total: Dict[str, float] = {}
        done = []
        for l in self.loops:
            step = l.iterate(core, roles)     # settle counted from the last move of ANY motor of the PID
            if step is None:
                continue
            terms, point = step
            contrib = l.controller.scaled(terms.output, l.active_weights())
            l.last_moves = contrib
            done.append((l, terms, point, contrib))
            for r, d in contrib.items():
                total[r] = total.get(r, 0.0) + d
        if done:
            self.iterations += 1
        # the sum of the contributions is scaled too (direction kept)
        lim = abs(self.loop.settings.max_step)
        big = max((abs(v) for v in total.values()), default=0.0)
        if big > lim > 0:
            total = {r: v * lim / big for r, v in total.items()}
        total = {r: v for r, v in total.items() if v != 0.0 and is_finite(v)}
        if not total:
            self.last_moves = {}
            return
        if self.loop.settings.confirm_moves:
            lines = [f"{l.signal_role}: PV = {t.pv:.6g}   Setpoint = {t.setpoint:.6g}   Error = {t.error:.6g}"
                     for l, t, _, _ in done] + ['']
            for r, d in total.items():
                pos = core.position(r)
                pos_s = f"{pos:.6g}" if pos is not None else '?'
                new_s = f"{pos + d:.6g}" if pos is not None else '?'
                lines.append(f"  {r}: {pos_s} → {new_s}  (Δ = {d:+.6g})")
            core.request_confirmation(self, '\n'.join(lines), total)
            return
        self.last_moves = core.request_moves(self, total)
        for l, _, point, contrib in done:
            point.moved = float(sum(abs(d) for d in contrib.values())) if self.last_moves else 0.0

    def variables(self) -> Dict[str, float]:
        out = {'iterations': float(self.iterations)}
        for k, l in enumerate(self.loops):
            t = l.controller.last
            pre = '' if len(self.loops) == 1 else f'{l.signal_role}.'
            out.update({f'{pre}setpoint': l.settings.setpoint, f'{pre}pv': t.pv, f'{pre}error': t.error,
                        f'{pre}p': t.p, f'{pre}i': t.i, f'{pre}d': t.d, f'{pre}output': t.output,
                        f'{pre}integral': l.controller.integral})
        return out
