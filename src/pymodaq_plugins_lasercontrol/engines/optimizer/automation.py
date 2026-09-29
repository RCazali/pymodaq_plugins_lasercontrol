"""Driver of the drift optimiser on top of the core (periodic passes, 'Optimize now', 'Full').

Ownership (D4 / O3-A): the optimiser takes its motors only during a pass. The PIDs sharing a
motor are therefore PAUSED during the pass and resume by themselves (integral reset) right after
- the typical case described in O3-A. If somebody else takes a motor during a pass, the pass is
aborted and the optimiser waits for its next pass.

Measurements come from the continuous detector stream (no grab from the optimiser): after each
move, the driver waits for the move_done, then ``dwell`` seconds, then averages N *new* frames.
"""
from __future__ import annotations

import math
import random
from collections import deque
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Tuple

from ...core.automation import Automation
from ...core.events import EventKind, Level
from ...core.safety import SafetyPolicy
from ...core.signals import is_finite
from ..sequencer import MoveSequencer
from .base import Measure, MoveTo, OptContext, OptimizerAlgorithm, Report, Status, create_algorithm
from .cost import CostModel
from . import algorithms  # noqa: F401  (registers the historical modes)

if TYPE_CHECKING:  # pragma: no cover
    from ...core.coordinator import LaserCore

DEFAULT_OPT_CONFIG = {
    'interval_min': 5.0,
    'dwell': 0.1,
    'max_iterations': 6,
    'run_mode': 'Joint (all motors at once)',
    'gradient_samples': 4,
    'damping_factor': 1.0,
    'stall_patience': 3,
    'deadband_scale': 1.0,
    'safety_factor': 5.0,
    'max_backoffs': 3,
    'full_opt_iterations': 30,
    'full_opt_stall_patience': 10,
    'full_opt_deadband_scale': 0.1,
    'full_optimization': False,
    'noise_samples': 5,
    'probe_samples': 2,
}

PASS_PHASES = ('settle', 'noise', 'moving', 'dwell', 'sampling')


class OptimizerAutomation(Automation):
    kind = 'optimizer'
    acquire_at_start = False

    def __init__(self, config: Optional[dict] = None, motors: Optional[List[dict]] = None,
                 signals: Optional[List[dict]] = None, auto_id: str = 'OPT', label: str = 'Optimizer'):
        super().__init__(auto_id, label)
        self.period_s = 0.05
        self.config = dict(DEFAULT_OPT_CONFIG)
        self.config.update(config or {})
        self.motors_cfg: List[dict] = [dict(m) for m in (motors or [])]
        self.signals_cfg: List[dict] = [dict(s) for s in (signals or [])]
        self.single_shot = False
        self.phase = 'idle'
        self.next_pass_t: Optional[float] = None
        self.cost_model = CostModel(self.signals_cfg)
        self.algo: Optional[OptimizerAlgorithm] = None
        self.ctx: Optional[OptContext] = None
        self.request = None
        self.sequencer: Optional[MoveSequencer] = None
        self.pass_start: Dict[str, float] = {}
        self._buf: Dict[str, List[float]] = {}
        self._last_seq: Dict[str, int] = {}
        self._t_min = 0.0
        self._needed = 0
        self._dwell_end = 0.0
        self.iteration = 0
        self.history: Deque[Tuple[int, float]] = deque(maxlen=5000)
        self.pass_count = 0
        self.status = ''
        self.last_cost = math.nan
        self.pass_best_cost = math.nan
        self.finished = False
        self._aborted = False
        self.rng = random.Random()

    # ------------------------------------------------------------------ description
    @property
    def in_pass(self) -> bool:
        return self.phase in PASS_PHASES

    def motor_roles(self) -> List[str]:
        return [m['role'] for m in self.motors_cfg]

    def wanted_motor_roles(self) -> List[str]:
        return self.motor_roles() if self.in_pass else []

    def objective_roles(self) -> List[str]:
        return [s['role'] for s in self.signals_cfg if float(s.get('weight', 0) or 0) != 0]

    def signal_roles(self) -> List[str]:
        return self.objective_roles() if self.in_pass else []

    def policy(self) -> SafetyPolicy:
        # Between passes nothing moves: no reason to freeze the optimiser on a transient intensity
        # drop. Every check applies again from the first tick of the next pass (before any move).
        waiting = self.phase in ('waiting', 'aborted')
        return SafetyPolicy(check_data=not waiting, check_intensity=not waiting, check_rate=True,
                            envelope='excursion')

    def summary(self) -> str:
        mot = ', '.join(f"{m['role']} (±{float(m['max_step']):g})" for m in self.motors_cfg)
        sig = ', '.join(f"{s['role']} [{s.get('mode', 'Maximize')}]" for s in self.signals_cfg
                        if float(s.get('weight', 0) or 0) != 0)
        mode = 'one pass' if self.single_shot else f"one pass every {self.config['interval_min']:g} min"
        return f"{self.label} ({self.config['run_mode']}, {mode}): motors {mot} ; objectives {sig}"

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = []
        if not self.motors_cfg:
            errors.append("optimizer: no motor ticked")
        if not self.objective_roles():
            errors.append("optimizer: no objective signal ticked (non-zero weight)")
        for m in self.motors_cfg:
            if float(m.get('max_step', 0)) <= 0 or float(m.get('probe_step', 0)) <= 0:
                errors.append(f"optimizer: {m['role']} max_step and probe_step must be > 0")
        for role in self.objective_roles():
            ch = core.roles.channel_for(role)
            if ch is None:
                errors.append(f"optimizer: signal role '{role}' not declared")
            elif not core.signals.known(ch):
                errors.append(f"optimizer: channel '{ch}' never received")
        return errors

    # ------------------------------------------------------------------ control
    def configure(self, config: Optional[dict] = None, motors: Optional[List[dict]] = None,
                  signals: Optional[List[dict]] = None):
        """Live update: parameters apply to the next pass; motors only when stopped."""
        if config:
            self.config.update(config)
        if motors is not None and self.state.value == 'OFF':
            self.motors_cfg = [dict(m) for m in motors]
        if signals is not None and not self.in_pass:
            self.signals_cfg = [dict(s) for s in signals]
            self.cost_model = CostModel(self.signals_cfg)

    def trigger_now(self, now: float) -> bool:
        """'Optimize Now' while the periodic optimiser waits: run the next pass immediately."""
        if self.phase == 'waiting':
            self.next_pass_t = now
            return True
        return False

    def seconds_to_next_pass(self, now: float) -> Optional[float]:
        if self.phase != 'waiting' or self.next_pass_t is None:
            return None
        return max(self.next_pass_t - now, 0.0)

    # ------------------------------------------------------------------ lifecycle
    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.cost_model = CostModel(self.signals_cfg)
        self.finished = False
        self.phase = 'waiting'
        self.next_pass_t = core.clock.now()           # first pass right away
        self.status = 'started'
        self._aborted = False
        return None

    def on_stop(self, core: 'LaserCore'):
        self._close_algo()
        self.phase = 'idle'
        self.next_pass_t = None

    def on_pause(self, core: 'LaserCore'):
        if self.in_pass:                               # preempted during a pass: abort it
            self._close_algo()
            self._aborted = True
            self.phase = 'aborted'
            self.status = f"pass aborted ({self.reason})"
            core.events.publish(EventKind.OPTIMIZER, self.id, f"Pass aborted: {self.reason}",
                                Level.WARNING)

    def on_resume(self, core: 'LaserCore'):
        now = core.clock.now()
        if self._aborted or self.phase == 'aborted':
            self._aborted = False
            self.phase = 'waiting'
            self.next_pass_t = now + self._interval_s()
            core.release(self)                          # give the remaining motors back (O3-A)
            return
        if self.phase in ('dwell', 'sampling', 'noise', 'settle'):
            # manual resume after a hold: measure again from scratch at the current point
            if self.phase == 'noise':
                self.phase = 'settle'
            elif self.request is not None:
                self.phase = 'dwell'
                self._dwell_end = now + float(self.config.get('dwell', 0.1))

    def _interval_s(self) -> float:
        return max(float(self.config.get('interval_min', 5.0)), 0.01) * 60.0

    def _close_algo(self):
        if self.algo is not None:
            try:
                self.algo.close()
            except Exception:  # pragma: no cover
                pass
        self.algo = None
        self.request = None

    def is_done(self) -> bool:
        return self.finished

    # ------------------------------------------------------------------ pass
    def _start_pass(self, core: 'LaserCore', now: float):
        self.pass_start = {r: core.position(r) for r in self.motor_roles()}
        if any(v is None for v in self.pass_start.values()):
            self.status = 'unknown position: pass postponed'
            self.next_pass_t = now + 5.0
            return
        self.phase = 'settle'                           # wait until PIDs' last moves are finished
        core.acquire(self)                              # D4: pauses the PIDs sharing a motor
        self.sequencer = MoveSequencer(core, self)
        self.ctx = OptContext(motors=[{'role': m['role'], 'max_step': float(m['max_step']),
                                       'probe_step': float(m['probe_step']), 'order': int(m.get('order', 0))}
                                      for m in self.motors_cfg],
                              config=dict(self.config), rng=self.rng)
        self.algo = create_algorithm(self.config.get('run_mode', 'joint'))
        self.status = 'estimating noise…'
        self.pass_best_cost = math.nan
        core.events.publish(EventKind.OPTIMIZER, self.id,
                            f"Optimization pass {self.pass_count + 1} ({self.algo.label})", Level.INFO)

    def _end_pass(self, core: 'LaserCore', now: float, message: str = ''):
        self._close_algo()
        self.pass_count += 1
        self.status = message or f"pass finished (cost {self.last_cost:.4g})"
        core.events.publish(EventKind.OPTIMIZER, self.id, f"End of pass: {self.status}", Level.INFO,
                            cost=self.last_cost)
        if self.single_shot:
            self.phase = 'done'
            self.finished = True
        else:
            self.phase = 'waiting'
            self.next_pass_t = now + self._interval_s()
        core.release(self)                              # PIDs resume by themselves (O3-A)

    def _begin_sampling(self, core: 'LaserCore', n: int, t_min: float):
        self._needed = max(int(n), 1)
        self._t_min = t_min
        self._buf = {r: [] for r in self.objective_roles()}
        self._last_seq = {}
        for r in self._buf:
            s = core.signals.latest(core.roles.channel_for(r))
            self._last_seq[r] = s.seq if s is not None else 0

    def _collect(self, core: 'LaserCore') -> bool:
        for r, buf in self._buf.items():
            if len(buf) >= self._needed:
                continue
            s = core.signals.latest(core.roles.channel_for(r))
            if s is not None and s.seq > self._last_seq[r] and s.t >= self._t_min and is_finite(s.value):
                buf.append(s.value)
                self._last_seq[r] = s.seq
        return all(len(b) >= self._needed for b in self._buf.values())

    def _targets(self, offsets: Dict[str, float]) -> Dict[str, float]:
        return {r: self.pass_start[r] + float(offsets.get(r, 0.0)) for r in self.pass_start}

    def _advance(self, core: 'LaserCore', now: float, result=None):
        """Feed the algorithm and execute its next request(s)."""
        if self.algo is None:
            return
        self.algo.observe(result)
        while True:
            req = self.algo.propose()
            if req is None:
                self._end_pass(core, now)
                return
            if isinstance(req, Status):
                self.status = req.message
                continue
            if isinstance(req, Report):
                self.iteration += 1
                self.last_cost = float(req.cost)
                self.history.append((self.iteration, float(req.cost)))
                self.pass_best_cost = req.cost if not is_finite(self.pass_best_cost) \
                    else min(self.pass_best_cost, req.cost)
                self.algo.observe(None)
                continue
            if isinstance(req, (Measure, MoveTo)):
                self.request = req
                self.sequencer.set_targets(self._targets(req.offsets))
                self.phase = 'moving'
                return
            raise TypeError(f"unknown optimizer request: {req!r}")

    def tick(self, core: 'LaserCore', now: float):
        if self.phase == 'waiting':
            if self.next_pass_t is not None and now >= self.next_pass_t:
                self._start_pass(core, now)
            return
        if self.phase in ('idle', 'done', 'aborted'):
            return
        if self.phase == 'settle':
            if core.any_busy(self.motor_roles()):
                return
            self.phase = 'noise'
            self._begin_sampling(core, int(self.config.get('noise_samples', 5)),
                                 now + float(self.config.get('dwell', 0.1)))
            return
        if self.phase == 'noise':
            if not self._collect(core):
                return
            scale = self.config.get('full_opt_deadband_scale', 0.1) if self.config.get('full_optimization') \
                else self.config.get('deadband_scale', 1.0)
            self.ctx.deadband = self.cost_model.estimate(self._buf, float(scale))
            self.algo.start(self.ctx)
            self._advance(core, now, None)
            return
        if self.phase == 'moving':
            status = self.sequencer.step()
            if status == MoveSequencer.DONE:
                if isinstance(self.request, Measure):
                    self.phase = 'dwell'
                    self._dwell_end = now + float(self.config.get('dwell', 0.1))
                else:
                    self.request = None
                    self._advance(core, now, None)
            return
        if self.phase == 'dwell':
            if now >= self._dwell_end:
                self.phase = 'sampling'
                self._begin_sampling(core, self.request.samples, self._dwell_end)
            return
        if self.phase == 'sampling':
            if not self._collect(core):
                return
            readings = {r: sum(b) / len(b) for r, b in self._buf.items()}
            cost = self.cost_model.cost(readings)
            self.last_cost = cost
            self.request = None
            self._advance(core, now, cost)

    def variables(self) -> Dict[str, float]:
        phase_code = {'idle': 0, 'waiting': 1, 'settle': 2, 'noise': 3, 'moving': 4, 'dwell': 5,
                      'sampling': 6, 'aborted': 7, 'done': 8}.get(self.phase, -1)
        return {'phase': float(phase_code), 'iteration': float(self.iteration), 'cost': self.last_cost,
                'pass_best_cost': self.pass_best_cost, 'deadband': self.cost_model.deadband,
                'passes': float(self.pass_count)}
