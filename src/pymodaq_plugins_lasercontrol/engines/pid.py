"""PID regulation of one 0D signal with a linear combination of motors (T4.1, T4.2).

Pure math in :class:`PIDController` (testable without Qt/hardware), adapter in
:class:`PIDAutomation` (driven by the core tick, moves through the broker).

Kept from the historical PIDEngine: signed gains, anti-windup (|ki * integral| <= max_step),
per motor clamp *after* weighting, "confirm moves" mode.
New (cahier des charges): skip the iteration while a motor is still moving (no stacking),
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


class PIDAutomation(Automation):
    kind = 'pid'

    def __init__(self, pid_id: str, signal_role: str, motors: Dict[str, float],
                 settings: Optional[PIDSettings] = None, label: str = '', in_stabilization: bool = True):
        super().__init__(f'PID:{pid_id}', label or f'PID {pid_id}')
        self.pid_id = pid_id
        self.signal_role = signal_role
        self.motors: Dict[str, float] = {r: float(w) for r, w in (motors or {}).items()}
        self.settings = settings or PIDSettings()
        self.controller = PIDController(self.settings)
        self.in_stabilization = in_stabilization
        self.period_s = self.settings.period_s
        self.trace: Deque[Tuple[float, float, float]] = deque(maxlen=4000)   # (wall t, pv, sp)
        self.last_sample_seq = 0
        self.iterations = 0
        self.skipped_busy = 0
        self.last_moves: Dict[str, float] = {}

    # ------------------------------------------------------------------ description
    def motor_roles(self) -> List[str]:
        return [r for r, w in self.motors.items() if w]

    def signal_roles(self) -> List[str]:
        return [self.signal_role] if self.signal_role else []

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy(check_data=True, check_intensity=True, check_rate=True, envelope='excursion')

    def summary(self) -> str:
        mot = ', '.join(f"{r}×{w:+g}" for r, w in self.motors.items() if w)
        sp = 'valeur au démarrage' if self.settings.setpoint_mode == 'capture' else f"{self.settings.setpoint:.6g}"
        return (f"{self.label} : {self.signal_role} → consigne {sp} ; moteurs {mot} ; "
                f"pas max {self.settings.max_step:g}/itération ; période {self.settings.period_s:g} s")

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = []
        if not self.signal_role:
            errors.append(f"{self.label} : aucun signal choisi")
        if not self.motor_roles():
            errors.append(f"{self.label} : aucun moteur coché (poids non nul)")
        if self.settings.max_step <= 0:
            errors.append(f"{self.label} : pas max doit être > 0")
        if self.settings.period_s <= 0:
            errors.append(f"{self.label} : période doit être > 0")
        return errors

    # ------------------------------------------------------------------ lifecycle
    def _current_seq(self, core: 'LaserCore') -> int:
        s = core.signals.latest(core.roles.channel_for(self.signal_role))
        return s.seq if s is not None else 0

    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.controller.reset()
        self.iterations = 0
        self.skipped_busy = 0
        self.last_moves = {}
        self.period_s = self.settings.period_s
        if self.settings.setpoint_mode == 'capture':
            v = core.signals.value(core.roles.channel_for(self.signal_role))
            if not is_finite(v):
                return "impossible de capturer la consigne (signal invalide)"
            self.settings.setpoint = float(v)
        self.last_sample_seq = 0          # first iteration may use the current (fresh) sample
        return None

    def on_resume(self, core: 'LaserCore'):
        self.controller.reset()            # O3-A / O10-A: integral reset at resume
        self.last_sample_seq = self._current_seq(core)   # wait for a sample taken after the resume

    def set_settings(self, settings: PIDSettings):
        """Live update (gains, setpoint...). The controller keeps its integral."""
        self.settings = settings
        self.controller.settings = settings
        self.period_s = settings.period_s

    # ------------------------------------------------------------------ loop
    def tick(self, core: 'LaserCore', now: float):
        self.period_s = self.settings.period_s
        roles = self.motor_roles()
        if core.any_busy(roles):
            self.skipped_busy += 1            # a move is not finished: skip, never stack (T4.1)
            return
        ch = core.roles.channel_for(self.signal_role)
        sample = core.signals.latest(ch)
        if sample is None or sample.seq <= self.last_sample_seq:
            return                            # wait for a new frame (no double counting)
        last_done = [t for t in (core.last_done_t(r) for r in roles) if t is not None]
        if last_done and sample.t < max(last_done) + self.settings.settle_s:
            return                            # sample older than the end of the last move + settle
        if not is_finite(sample.value):
            return                            # S12 (the core holds on stale/invalid data anyway)
        self.last_sample_seq = sample.seq
        terms = self.controller.update(sample.value, self.settings.period_s)
        self.iterations += 1
        self.trace.append((core.clock.wall(), terms.pv, terms.setpoint))
        moves = self.controller.split(terms.output, {r: self.motors[r] for r in roles})
        if not moves:
            self.last_moves = {}
            return
        if self.settings.confirm_moves:
            lines = [f"PV = {terms.pv:.6g}   Consigne = {terms.setpoint:.6g}   Erreur = {terms.error:.6g}", '']
            for r, d in moves.items():
                pos = core.position(r)
                pos_s = f"{pos:.6g}" if pos is not None else '?'
                new_s = f"{pos + d:.6g}" if pos is not None else '?'
                lines.append(f"  {r} : {pos_s} → {new_s}  (Δ = {d:+.6g})")
            core.request_confirmation(self, '\n'.join(lines), moves)
            return
        self.last_moves = core.request_moves(self, moves)

    def variables(self) -> Dict[str, float]:
        t = self.controller.last
        return {'setpoint': self.settings.setpoint, 'pv': t.pv, 'error': t.error, 'p': t.p, 'i': t.i,
                'd': t.d, 'output': t.output, 'integral': self.controller.integral,
                'iterations': float(self.iterations)}
