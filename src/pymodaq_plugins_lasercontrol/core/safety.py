"""Safety supervisor (cahier des charges T3.4, invariants S1-S5, option O10-A).

Every *automatic* order goes through :meth:`SafetySupervisor.check_move` before reaching the
hardware:

* S3  relative steps only, each one bounded by the role ``max_step`` (clamped),
* S4  the motor stays within ``engagement position +/- max_excursion`` and within
      ``abs_min/abs_max``; reaching the envelope freezes the automation and raises an alarm,
* S5  the cumulated displacement of a motor over a sliding window is bounded (``rate_max``),
* S1/S2 fresh data and intensity above the threshold are checked by the core before every tick
  (see :class:`IntensityGuard` for S2, with a hysteresis).

A violation never retries by itself: the automation goes HELD and the operator resumes it
manually after checking (O10-A).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Deque, Dict, List, Optional, Tuple

from .clock import Clock
from .events import EventBus, EventKind, Level
from .roles import RoleMap
from .signals import SignalSource, is_finite


def _f(v) -> Optional[float]:
    """float or None (empty strings / None / NaN -> None)."""
    if v is None or v == '':
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass
class ActuatorLimits:
    """Per role safety limits, in the actuator units. ``None`` = not configured."""
    max_step: Optional[float] = None        # S3: max |delta| of one automatic order
    max_excursion: Optional[float] = None   # S4: +/- around the engagement position
    abs_min: Optional[float] = None         # S4: absolute limits (also applied to manual moves)
    abs_max: Optional[float] = None
    rate_window_s: float = 60.0             # S5: sliding window
    rate_max: Optional[float] = None        # S5: max cumulated |displacement| within the window
    resolution: float = 0.0                 # smallest meaningful move (e.g. 1 picomotor step)

    REQUIRED = ('max_step', 'max_excursion', 'rate_max')

    def missing(self) -> List[str]:
        out = []
        for name in self.REQUIRED:
            v = getattr(self, name)
            if v is None or v <= 0:
                out.append(name)
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ActuatorLimits':
        d = d or {}
        return cls(max_step=_f(d.get('max_step')), max_excursion=_f(d.get('max_excursion')),
                   abs_min=_f(d.get('abs_min')), abs_max=_f(d.get('abs_max')),
                   rate_window_s=_f(d.get('rate_window_s')) or 60.0,
                   rate_max=_f(d.get('rate_max')), resolution=_f(d.get('resolution')) or 0.0)


@dataclass
class IntensityGuardConfig:
    enabled: bool = True
    role: Optional[str] = None          # signal role used as intensity reference (e.g. Intensity_main)
    mode: str = 'relative'              # 'relative' (fraction of a reference) | 'absolute'
    threshold: float = 0.5              # fraction (relative) or value (absolute): below -> freeze
    hysteresis: float = 0.1             # added to the threshold to consider the intensity OK again
    reference: Optional[float] = None   # relative mode: fixed reference; None = captured at engagement

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'IntensityGuardConfig':
        d = d or {}
        mode = str(d.get('mode', 'relative'))
        return cls(enabled=bool(d.get('enabled', True)), role=d.get('role') or None,
                   mode=mode if mode in ('relative', 'absolute') else 'relative',
                   threshold=_f(d.get('threshold')) if _f(d.get('threshold')) is not None else 0.5,
                   hysteresis=_f(d.get('hysteresis')) if _f(d.get('hysteresis')) is not None else 0.1,
                   reference=_f(d.get('reference')))


@dataclass
class SafetyConfig:
    max_data_age_s: float = 1.0          # S1 / O9: older data are stale
    move_timeout_s: float = 20.0         # T3.1: a move not done after this time -> alarm + hold
    manual_abs_limits: bool = True       # apply abs_min/abs_max of the roles to manual moves too
    intensity: IntensityGuardConfig = field(default_factory=IntensityGuardConfig)
    actuators: Dict[str, ActuatorLimits] = field(default_factory=dict)

    def limits(self, role: str) -> Optional[ActuatorLimits]:
        return self.actuators.get(role)

    def to_dict(self) -> dict:
        return {'max_data_age_s': self.max_data_age_s, 'move_timeout_s': self.move_timeout_s,
                'manual_abs_limits': self.manual_abs_limits,
                'intensity': self.intensity.to_dict(),
                'actuators': {k: v.to_dict() for k, v in self.actuators.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> 'SafetyConfig':
        d = d or {}
        return cls(max_data_age_s=_f(d.get('max_data_age_s')) or 1.0,
                   move_timeout_s=_f(d.get('move_timeout_s')) or 20.0,
                   manual_abs_limits=bool(d.get('manual_abs_limits', True)),
                   intensity=IntensityGuardConfig.from_dict(d.get('intensity', {})),
                   actuators={str(k): ActuatorLimits.from_dict(v)
                              for k, v in (d.get('actuators', {}) or {}).items()})


@dataclass
class SafetyPolicy:
    """Which rules apply to an automation kind."""
    check_data: bool = True        # S1
    check_intensity: bool = True   # S2
    check_rate: bool = True        # S5
    envelope: str = 'excursion'    # S4: 'excursion' (+/- max_excursion) or 'explicit' (declared range)


@dataclass
class Violation:
    code: str          # 'stale' | 'intensity' | 'envelope' | 'rate' | 'config' | 'timeout' | 'owner'
    message: str
    role: Optional[str] = None


class IntensityGuard:
    """S2 with hysteresis. States: 'disabled', 'ok', 'low', 'unknown'."""

    def __init__(self, cfg: IntensityGuardConfig, signals: SignalSource, roles: RoleMap):
        self.cfg = cfg
        self.signals = signals
        self.roles = roles
        self.captured_reference: Optional[float] = None
        self._low = False

    @property
    def reference(self) -> Optional[float]:
        if self.cfg.reference is not None:
            return self.cfg.reference
        return self.captured_reference

    def channel(self) -> Optional[str]:
        return self.roles.channel_for(self.cfg.role) if self.cfg.role else None

    def value(self) -> float:
        return self.signals.value(self.channel()) if self.channel() else math.nan

    def thresholds(self) -> Tuple[Optional[float], Optional[float]]:
        """(trip, release) in signal units."""
        if self.cfg.mode == 'absolute':
            return self.cfg.threshold, self.cfg.threshold + abs(self.cfg.hysteresis)
        ref = self.reference
        if ref is None:
            return None, None
        return self.cfg.threshold * ref, (self.cfg.threshold + abs(self.cfg.hysteresis)) * ref

    def capture_reference(self) -> bool:
        v = self.value()
        if self.cfg.mode != 'relative' or not is_finite(v) or not self.signals.is_fresh(self.channel()):
            return False
        self.captured_reference = float(v)
        self._low = False
        return True

    def evaluate(self) -> str:
        """Update the hysteresis latch and return the state."""
        if not self.cfg.enabled:
            return 'disabled'
        ch = self.channel()
        if not ch or not self.signals.is_fresh(ch):
            return 'unknown'
        trip, release = self.thresholds()
        if trip is None:
            return 'unknown'
        v = self.signals.value(ch)
        if self._low:
            if v >= release:
                self._low = False
        elif v < trip:
            self._low = True
        return 'low' if self._low else 'ok'

    def can_resume(self) -> Tuple[bool, str]:
        """Resume requires the value above the *release* threshold (hysteresis)."""
        st = self.evaluate()
        if st in ('disabled', 'ok'):
            return True, ''
        if st == 'unknown':
            return False, "intensité inconnue (signal absent ou périmé)"
        return False, "intensité toujours sous le seuil (+ hystérésis)"

    def ratio(self) -> float:
        ref = self.reference
        v = self.value()
        if self.cfg.mode == 'absolute' or not ref:
            return math.nan
        return v / ref if is_finite(v) else math.nan


class RateTracker:
    """Cumulated |displacement| per module over a sliding window (S5)."""

    def __init__(self):
        self._moves: Dict[str, Deque[Tuple[float, float]]] = {}

    def add(self, module: str, delta: float, t: float):
        self._moves.setdefault(module, deque(maxlen=10000)).append((t, abs(float(delta))))

    def total(self, module: str, window_s: float, now: float) -> float:
        q = self._moves.get(module)
        if not q:
            return 0.0
        while q and now - q[0][0] > max(window_s, 3600.0):   # memory bound
            q.popleft()
        return float(sum(d for t, d in q if now - t <= window_s))

    def reset(self, module: Optional[str] = None):
        if module is None:
            self._moves.clear()
        else:
            self._moves.pop(module, None)


class SafetySupervisor:
    def __init__(self, clock: Clock, events: EventBus, signals: SignalSource, roles: RoleMap,
                 config: Optional[SafetyConfig] = None):
        self.clock = clock
        self.events = events
        self.signals = signals
        self.roles = roles
        self.config = config or SafetyConfig()
        self.guard = IntensityGuard(self.config.intensity, signals, roles)
        self.rate = RateTracker()
        self._refs: Dict[Tuple[str, str], float] = {}                 # (owner, module) -> engagement pos
        self._explicit: Dict[str, Dict[str, Tuple[float, float]]] = {}  # owner -> module -> (lo, hi)
        self._last_guard_state = None
        self.signals.set_max_age(self.config.max_data_age_s)

    # ------------------------------------------------------------------ config
    def set_config(self, cfg: SafetyConfig, roles: Optional[RoleMap] = None):
        if roles is not None:
            self.roles = roles
        keep_ref = self.guard.captured_reference if cfg.intensity.role == self.config.intensity.role else None
        self.config = cfg
        self.guard = IntensityGuard(cfg.intensity, self.signals, self.roles)
        self.guard.captured_reference = keep_ref
        self.signals.set_max_age(cfg.max_data_age_s)

    def limits(self, role: str) -> Optional[ActuatorLimits]:
        return self.config.limits(role)

    def config_errors(self, motor_roles, policy: SafetyPolicy) -> List[str]:
        errors = []
        for role in motor_roles:
            lim = self.limits(role)
            if lim is None:
                errors.append(f"{role}: limites de sécurité non définies (max_step, max_excursion, rate_max)")
                continue
            missing = [m for m in lim.missing()
                       if not (m == 'max_excursion' and policy.envelope == 'explicit')
                       and not (m == 'rate_max' and not policy.check_rate)]
            if missing:
                errors.append(f"{role}: limite(s) manquante(s) {', '.join(missing)}")
            if lim.abs_min is not None and lim.abs_max is not None and lim.abs_min >= lim.abs_max:
                errors.append(f"{role}: abs_min >= abs_max")
        if policy.check_intensity and self.config.intensity.enabled:
            role = self.config.intensity.role
            if not role:
                errors.append("garde intensité (S2) : aucun rôle d'intensité défini "
                              "(ou désactiver explicitement la garde dans le preset)")
            elif not self.roles.is_signal(role):
                errors.append(f"garde intensité (S2) : rôle '{role}' inconnu sur cette machine")
        return errors

    # --------------------------------------------------------------- envelopes
    def engage(self, owner: str, positions: Dict[str, float],
               explicit: Optional[Dict[str, Tuple[float, float]]] = None, reset: bool = False):
        """Record the engagement position of each module (S4 reference)."""
        for module, pos in positions.items():
            key = (owner, module)
            if reset or key not in self._refs:
                if pos is not None and is_finite(pos):
                    self._refs[key] = float(pos)
        if explicit is not None:
            self._explicit[owner] = {m: (min(lo, hi), max(lo, hi)) for m, (lo, hi) in explicit.items()}

    def disengage(self, owner: str):
        for key in [k for k in self._refs if k[0] == owner]:
            del self._refs[key]
        self._explicit.pop(owner, None)

    def reference(self, owner: str, module: str) -> Optional[float]:
        return self._refs.get((owner, module))

    def references(self, owner: str) -> Dict[str, float]:
        return {m: v for (o, m), v in self._refs.items() if o == owner}

    def allowed_range(self, owner: str, role: str, module: str,
                      policy: SafetyPolicy) -> Tuple[float, float]:
        lim = self.limits(role) or ActuatorLimits()
        lo, hi = -math.inf, math.inf
        if policy.envelope == 'explicit':
            rng = self._explicit.get(owner, {}).get(module)
            if rng is not None:
                tol = max(lim.resolution, 0.0)
                lo, hi = rng[0] - tol, rng[1] + tol
        else:
            ref = self._refs.get((owner, module))
            if ref is not None and lim.max_excursion is not None:
                lo, hi = ref - lim.max_excursion, ref + lim.max_excursion
        if lim.abs_min is not None:
            lo = max(lo, lim.abs_min)
        if lim.abs_max is not None:
            hi = min(hi, lim.abs_max)
        return lo, hi

    # ------------------------------------------------------------------ checks
    def check_move(self, owner: str, role: str, module: str, position: Optional[float], delta: float,
                   policy: SafetyPolicy) -> Tuple[float, Optional[Violation]]:
        """Returns (allowed_delta, violation). A violation always means: freeze the automation.
        For S4 the move is clamped to the envelope and sent, then the automation is frozen."""
        lim = self.limits(role)
        if lim is None:
            return 0.0, Violation('config', f"limites de sécurité non définies pour {role}", role)
        if lim.max_step is None or lim.max_step <= 0:
            return 0.0, Violation('config', f"max_step non défini pour {role}", role)
        if position is None or not is_finite(position):
            return 0.0, Violation('config', f"position inconnue pour {role}", role)
        if policy.envelope == 'excursion' and (owner, module) not in self._refs:
            return 0.0, Violation('config', f"pas de position d'engagement pour {role}", role)
        d = float(delta)
        if not math.isfinite(d):
            return 0.0, Violation('config', f"pas invalide (NaN) demandé sur {role}", role)
        # S3: bounded relative step
        if abs(d) > lim.max_step:
            d = math.copysign(lim.max_step, d)
        # S4: envelope and absolute limits
        lo, hi = self.allowed_range(owner, role, module, policy)
        eps = 1e-9 * max(1.0, abs(position))
        violation = None
        target = position + d
        if target > hi + eps:
            d = max(hi - position, 0.0) if position <= hi else 0.0
            violation = Violation('envelope', f"enveloppe atteinte sur {role} (limite haute {hi:.6g})", role)
        elif target < lo - eps:
            d = min(lo - position, 0.0) if position >= lo else 0.0
            violation = Violation('envelope', f"enveloppe atteinte sur {role} (limite basse {lo:.6g})", role)
        # S5: rate limit
        if policy.check_rate:
            if lim.rate_max is None or lim.rate_max <= 0:
                return 0.0, Violation('config', f"rate_max non défini pour {role}", role)
            used = self.rate.total(module, lim.rate_window_s, self.clock.now())
            if used + abs(d) > lim.rate_max * (1 + 1e-9):
                return 0.0, Violation(
                    'rate', f"limite de débit atteinte sur {role} ({used:.4g} déjà parcourus en "
                            f"{lim.rate_window_s:.0f} s, limite {lim.rate_max:.4g})", role)
        return d, violation

    def record_move(self, module: str, delta: float):
        self.rate.add(module, delta, self.clock.now())

    def check_manual(self, role: Optional[str], target: float) -> Optional[str]:
        """Absolute limits applied to manual moves (typo protection). Returns a refusal message."""
        if not self.config.manual_abs_limits or not role:
            return None
        lim = self.limits(role)
        if lim is None:
            return None
        if lim.abs_min is not None and target < lim.abs_min:
            return f"{role}: cible {target:.6g} sous abs_min ({lim.abs_min:.6g})"
        if lim.abs_max is not None and target > lim.abs_max:
            return f"{role}: cible {target:.6g} au-dessus de abs_max ({lim.abs_max:.6g})"
        return None

    def intensity_state(self) -> str:
        st = self.guard.evaluate()
        if st != self._last_guard_state:
            if st == 'low':
                self.events.publish(EventKind.SAFETY_INFO, 'SAFETY',
                                    f"Intensité sous le seuil ({self.guard.value():.4g})", Level.WARNING)
            elif st == 'ok' and self._last_guard_state == 'low':
                self.events.publish(EventKind.SAFETY_INFO, 'SAFETY',
                                    "Intensité revenue au-dessus du seuil (+ hystérésis)", Level.INFO)
            self._last_guard_state = st
        return st
