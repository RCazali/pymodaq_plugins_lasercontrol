"""Routines: sequences of high-level steps ("start A, wait until it converges, pause B, optimise C…").

A routine is a list of **steps**. Each step does one **action** (start / stop / pause / resume an automation,
move a motor, write a note, or nothing), then waits until one of its
**conditions** is true — the first true condition decides what comes next (next step, go to step n, end,
fail). A step without condition goes on as soon as its action is done. A **timeout** is the last resort.
Delays and timeouts count from the end of the action (a move takes time). A routine only EXECUTES: it
never changes a setting (setpoints, gains, decoupling live in the PID / Optimization tabs).

Conditions: time elapsed, PID converged (``|error| ≤ tol`` for ``hold`` s, every signal of the PID), signal
compared to a value for ``hold`` s, motor travel since the step began, automation finished (one optimiser
pass…), number of times the step was entered (loop guard).

The routine runs as an automation of the core (``RTN:<id>``), ticked like the others (no thread, no
blocking). It is started by a human (S9); what it starts is then watched: an automation it started that
goes HELD / ERROR, or is stopped by somebody else, makes the routine fail. On a failure or a user stop, the
automations it started are stopped (option, default on); the ones it paused stay paused (nothing moves,
the operator resumes them). Its own motor moves go through the core like any automation move (ownership,
max step, envelope); the PIDs using that motor are preempted during the move and resume by themselves.

Starting what needs the application (PID tab, optimisation settings) goes through a :class:`RoutineHost`;
the default host uses the core alone (tests, offline simulations).
"""
from __future__ import annotations

import copy
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Tuple

from ..core.automation import Automation
from ..core.events import EventKind, Level
from ..core.safety import SafetyPolicy
from ..core.signals import is_finite
from ..core.states import AutoState, ENGAGED_STATES
from .pid import PIDAutomation
from .sequencer import MoveSequencer

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore

#: action -> label shown in the editor
#: execution only: a routine never changes a setting (settings live in the PID / Optimization tabs)
ACTIONS = {'none': 'Wait only', 'start': 'Start', 'stop': 'Stop', 'pause': 'Pause', 'resume': 'Resume',
           'move': 'Move motor', 'note': 'Write a note in the log'}
#: condition kind -> label shown in the editor
CONDITIONS = {'delay': 'Time (s)', 'locked': 'PID converged', 'signal': 'Signal', 'travel': 'Motor travel',
              'finished': 'Finished', 'count': 'Step count'}
THENS = {'next': 'next step', 'goto': 'go to step', 'end': 'end (success)', 'fail': 'fail (stop)'}
SIGNAL_OPS = ('<', '>', '|x|<', '|x|>')
START_OPTIONS = {'single': 'one pass', 'periodic': 'periodic', 'full': 'full optimization'}
MOVE_OPTIONS = {'relative': 'relative', 'absolute': 'absolute'}
AUTOMATION_ACTIONS = ('start', 'stop', 'pause', 'resume')
PREFIX = 'RTN:'


# ====================================================================== data
def _num(v, default: float = 0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


@dataclass
class Condition:
    kind: str = 'delay'
    target: str = ''          # PID / automation id, signal role or motor role (by kind)
    op: str = '<'             # signal: '<', '>', '|x|<', '|x|>'
    value: float = 0.0        # seconds, tolerance, threshold, travel or count (by kind)
    hold_s: float = 0.0       # locked / signal: must stay true that long
    then: str = 'next'
    goto: int = 1             # step number (1-based) when then == 'goto'

    @classmethod
    def from_dict(cls, d: dict) -> 'Condition':
        d = dict(d or {})
        c = cls(kind=str(d.get('kind', 'delay')), target=str(d.get('target', '') or ''),
                op=str(d.get('op', '<')), value=_num(d.get('value')), hold_s=max(_num(d.get('hold_s')), 0.0),
                then=str(d.get('then', 'next')), goto=int(_num(d.get('goto'), 1)))
        if c.op not in SIGNAL_OPS:
            c.op = '<'
        return c

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Step:
    action: str = 'none'
    target: str = ''          # automation id ('PID:x', 'OPT:y') or motor role
    value: float = 0.0        # move: step or position
    option: str = ''          # start: single/periodic/full ; move: relative/absolute
    text: str = ''            # note text, or a comment
    until: List[Condition] = field(default_factory=list)
    timeout_s: float = 0.0    # 0 = none; counted from the end of the action
    on_timeout: str = 'fail'
    timeout_goto: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> 'Step':
        d = dict(d or {})
        return cls(action=str(d.get('action', 'none')), target=str(d.get('target', '') or ''),
                   value=_num(d.get('value')), option=str(d.get('option', '') or ''),
                   text=str(d.get('text', '') or ''),
                   until=[Condition.from_dict(c) for c in d.get('until', []) or []],
                   timeout_s=max(_num(d.get('timeout_s')), 0.0), on_timeout=str(d.get('on_timeout', 'fail')),
                   timeout_goto=int(_num(d.get('timeout_goto'), 1)))

    def to_dict(self) -> dict:
        d = asdict(self)
        d['until'] = [c.to_dict() for c in self.until]
        return d

    def targets(self) -> List[str]:
        return [t.strip() for t in self.target.split(',') if t.strip()]


@dataclass
class Routine:
    id: str = 'Routine_1'
    label: str = ''
    show_in_alignment: bool = True
    stop_started_on_abort: bool = True
    description: str = ''
    steps: List[Step] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> 'Routine':
        d = dict(d or {})
        return cls(id=str(d.get('id', 'Routine_1')), label=str(d.get('label', '') or ''),
                   show_in_alignment=bool(d.get('show_in_alignment', True)),
                   stop_started_on_abort=bool(d.get('stop_started_on_abort', True)),
                   description=str(d.get('description', '') or ''),
                   steps=[Step.from_dict(s) for s in d.get('steps', []) or []])

    def to_dict(self) -> dict:
        return {'id': self.id, 'label': self.label, 'show_in_alignment': self.show_in_alignment,
                'stop_started_on_abort': self.stop_started_on_abort, 'description': self.description,
                'steps': [s.to_dict() for s in self.steps]}

    def name(self) -> str:
        return self.label or self.id


def default_routine(routine_id: str = 'Routine_1') -> dict:
    return Routine(routine_id).to_dict()


# ====================================================================== editing helpers (step numbers)
def _renumber(routine: Routine, mapping: Dict[int, Optional[int]]):
    """Apply ``mapping`` (old step number -> new number or None when deleted) to every 'go to'."""
    def fix(n: int) -> int:
        new = mapping.get(n, n)
        return new if new is not None else n        # a goto to a deleted step stays: validation reports it
    for s in routine.steps:
        s.timeout_goto = fix(s.timeout_goto)
        for c in s.until:
            c.goto = fix(c.goto)


def move_step(routine: Routine, index: int, new_index: int):
    """Move step ``index`` to ``new_index`` (0-based); the 'go to' numbers follow their step."""
    n = len(routine.steps)
    if not (0 <= index < n and 0 <= new_index < n) or index == new_index:
        return
    order = list(range(n))
    order.insert(new_index, order.pop(index))
    mapping = {old + 1: pos + 1 for pos, old in enumerate(order)}
    routine.steps = [routine.steps[i] for i in order]
    _renumber(routine, mapping)


def insert_step(routine: Routine, index: int, step: Optional[Step] = None) -> Step:
    """Insert ``step`` at ``index`` (0-based); the 'go to' numbers after it are shifted."""
    step = step or Step()
    index = max(0, min(index, len(routine.steps)))
    mapping = {k: (k + 1 if k > index else k) for k in range(1, len(routine.steps) + 1)}
    _renumber(routine, mapping)
    routine.steps.insert(index, step)
    return step


def delete_step(routine: Routine, index: int):
    if not 0 <= index < len(routine.steps):
        return
    mapping = {k: (None if k == index + 1 else (k - 1 if k > index + 1 else k))
               for k in range(1, len(routine.steps) + 1)}
    routine.steps.pop(index)
    _renumber(routine, mapping)


def duplicate_step(routine: Routine, index: int):
    if 0 <= index < len(routine.steps):
        insert_step(routine, index + 1, copy.deepcopy(routine.steps[index]))


# ====================================================================== validation / text
def validate_routine(routine: Routine) -> List[str]:
    """Structural problems (the host checks the targets against the machine at start)."""
    errors = []
    n = len(routine.steps)
    if not n:
        errors.append('no step')

    def check_goto(where: str, then: str, goto: int):
        if then not in THENS:
            errors.append(f'{where}: unknown outcome {then!r}')
        elif then == 'goto' and not 1 <= goto <= n:
            errors.append(f'{where}: "go to step {goto}" does not exist')

    for i, s in enumerate(routine.steps, start=1):
        w = f'step {i}'
        if s.action not in ACTIONS:
            errors.append(f'{w}: unknown action {s.action!r}')
            continue
        if s.action in AUTOMATION_ACTIONS + ('move',) and not s.target:
            errors.append(f'{w}: {ACTIONS[s.action].lower()} needs a target')
        if s.action == 'move' and s.value == 0 and s.option != 'absolute':
            errors.append(f'{w}: move by 0')
        for k, c in enumerate(s.until, start=1):
            cw = f'{w}, condition {k}'
            if c.kind not in CONDITIONS:
                errors.append(f'{cw}: unknown condition {c.kind!r}')
                continue
            if c.kind in ('locked', 'signal', 'travel', 'finished') and not c.target:
                errors.append(f'{cw}: {CONDITIONS[c.kind].lower()} needs a target')
            if c.kind == 'locked' and not c.target.startswith('PID:'):
                errors.append(f'{cw}: "PID converged" needs a PID')
            if c.kind == 'travel' and c.value <= 0:
                errors.append(f'{cw}: the travel must be > 0')
            if c.kind == 'count' and c.value < 1:
                errors.append(f'{cw}: the count must be ≥ 1')
            check_goto(cw, c.then, c.goto)
        if s.timeout_s > 0:
            check_goto(f'{w}, timeout', s.on_timeout, s.timeout_goto)
    return errors


def _then_text(then: str, goto: int) -> str:
    return f'go to {goto}' if then == 'goto' else {'next': 'next', 'end': 'END', 'fail': 'FAIL'}.get(then, then)


def describe_action(s: Step) -> str:
    t = s.target or '?'
    if s.action == 'none':
        return 'Wait' + (f' — {s.text}' if s.text else '')
    if s.action == 'note':
        return f'Note: “{s.text}”'
    if s.action == 'start':
        opt = f' ({START_OPTIONS[s.option]})' if t.startswith('OPT:') and s.option in START_OPTIONS else ''
        return f'Start {t}{opt}'
    if s.action in ('stop', 'pause', 'resume'):
        return f'{ACTIONS[s.action]} {t}'
    if s.action == 'move':
        return f"Move {t} {'to' if s.option == 'absolute' else 'by'} {s.value:+g}"
    return s.action


def describe_condition(c: Condition) -> str:
    if c.kind == 'delay':
        what = f'after {c.value:g} s'
    elif c.kind == 'locked':
        tol = f'|error| ≤ {c.value:g}' if c.value > 0 else '|error| ≤ deadband'
        what = f'{c.target} converged ({tol} for {c.hold_s:g} s)'
    elif c.kind == 'signal':
        op = c.op.replace('x', c.target) if c.op.startswith('|') else f'{c.target} {c.op}'
        what = f'{op} {c.value:g}' + (f' for {c.hold_s:g} s' if c.hold_s > 0 else '')
    elif c.kind == 'travel':
        what = f'{c.target} moved ≥ {c.value:g}'
    elif c.kind == 'finished':
        what = f'{c.target} finished'
    elif c.kind == 'count':
        what = f'step entered ≥ {int(c.value)}×'
    else:
        what = c.kind
    return f'{what} → {_then_text(c.then, c.goto)}'


def describe_until(s: Step) -> str:
    parts = [describe_condition(c) for c in s.until]
    if s.timeout_s > 0:
        parts.append(f'timeout {s.timeout_s:g} s → {_then_text(s.on_timeout, s.timeout_goto)}')
    return ' | '.join(parts) if parts else 'when done → next'


# ====================================================================== host
class RoutineHost:
    """What a routine may do. This default implementation uses the core alone; the application gives a
    host that starts from its tabs (PID configurations, optimisation settings)."""

    def __init__(self, core: 'LaserCore'):
        self.core = core

    def automation(self, target: str) -> Optional[Automation]:
        return self.core.get(target)

    def pid(self, target: str) -> Optional[PIDAutomation]:
        a = self.core.get(target)
        return a if isinstance(a, PIDAutomation) else None

    def check_target(self, target: str) -> Optional[str]:
        return None if self.core.get(target) is not None else f'{target} does not exist in this preset'

    def start(self, target: str, option: str = '') -> Tuple[bool, str]:
        a = self.automation(target)
        if a is None:
            return False, f'{target} does not exist'
        if hasattr(a, 'run_mode_override'):
            a.run_mode_override = 'confirm' if option == 'confirm' else None   # only for this start
        if option and hasattr(a, 'single_shot'):
            a.single_shot = option != 'periodic'
            if hasattr(a, 'config'):
                a.config['full_optimization'] = option == 'full'
        return self.core.start(a.id)

    def stop(self, target: str, reason: str) -> bool:
        a = self.automation(target)
        return a is not None and self.core.stop(a.id, reason)

    def pause(self, target: str, reason: str, by: str) -> Tuple[bool, str]:
        a = self.automation(target)
        return (False, f'{target} does not exist') if a is None else self.core.pause(a.id, reason, by)

    def resume(self, target: str, reason: str) -> Tuple[bool, str]:
        a = self.automation(target)
        return (False, f'{target} does not exist') if a is None else self.core.resume(a.id, reason)

    def set_setpoint(self, target: str, k: int, value: float) -> Tuple[bool, str]:
        """Setpoint of signal ``k`` of a PID (execution data: positions and ramps of a program)."""
        p = self.pid(target)
        if p is None or not 0 <= k < len(p.loops):
            return False, f'{target}: no signal {k + 1}'
        p.loops[k].settings.setpoint = float(value)
        return True, ''

# ====================================================================== runner
class RoutineAutomation(Automation):
    kind = 'routine'
    acquire_at_start = False

    def __init__(self, routine: Routine, host: RoutineHost):
        super().__init__(PREFIX + routine.id, routine.name())
        self.routine = routine
        self.host = host
        self.period_s = 0.1
        self.index = 0
        self.phase = 'idle'        # idle | enter | moving | wait | done
        self.status = ''
        self.outcome = ''          # '' | 'done' | 'failed' | 'stopped'
        self.finished = False
        self.started: Dict[str, str] = {}        # automation id -> label, started by this routine
        self.watched: Dict[str, str] = {}        # started or resumed by it: held / error / stopped = failure
        self.paused: Dict[str, str] = {}         # automation id -> label, paused by this routine
        self.visits: Dict[int, int] = {}
        self.history: Deque[Tuple[float, str]] = deque(maxlen=300)
        self._t_step = 0.0
        self._t_wait = 0.0
        self._wall_step = 0.0
        self._pos0: Dict[str, Optional[float]] = {}
        self._since: Dict[int, Optional[float]] = {}
        self._moving_roles: List[str] = []
        self.sequencer: Optional[MoveSequencer] = None

    # ------------------------------------------------------------------ description
    def motor_roles(self) -> List[str]:
        return list(dict.fromkeys(s.target for s in self.routine.steps if s.action == 'move' and s.target))

    def wanted_motor_roles(self) -> List[str]:
        return list(self._moving_roles)

    def policy(self) -> SafetyPolicy:
        # the routine itself only moves in 'move' steps (bounded chunks, ownership, envelope); what it
        # starts applies its own policy (freshness, intensity guard...)
        return SafetyPolicy(check_data=False, check_intensity=False, check_rate=True, envelope='excursion')

    def summary(self) -> str:
        lines = [f'{self.label}: {len(self.routine.steps)} steps']
        for i, s in enumerate(self.routine.steps, start=1):
            lines.append(f'  {i}. {describe_action(s)} — {describe_until(s)}')
        return '\n'.join(lines)

    @property
    def step(self) -> Optional[Step]:
        return self.routine.steps[self.index] if 0 <= self.index < len(self.routine.steps) else None

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = validate_routine(self.routine)
        for i, s in enumerate(self.routine.steps, start=1):
            names = s.targets() if s.action in AUTOMATION_ACTIONS else []
            names += [c.target for c in s.until if c.kind in ('locked', 'finished') and c.target]
            for t in dict.fromkeys(names):
                if t.startswith(PREFIX):
                    errors.append(f'step {i}: a routine cannot drive another routine ({t})')
                    continue
                err = self.host.check_target(t)
                if err:
                    errors.append(f'step {i}: {err}')
            for c in s.until:
                if c.kind == 'signal' and core.roles.channel_for(c.target) is None:
                    errors.append(f"step {i}: signal role '{c.target}' not declared")
                if c.kind == 'travel' and core.roles.module_for(c.target) is None:
                    errors.append(f"step {i}: motor role '{c.target}' not declared")
        return errors

    # ------------------------------------------------------------------ lifecycle
    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.index = 0
        self.phase = 'enter'
        self.status = 'started'
        self.outcome = ''
        self.finished = False
        self.started = {}
        self.watched = {}
        self.paused = {}
        self.visits = {}
        self._moving_roles = []
        self._log(core, f'{self.label}: started ({len(self.routine.steps)} steps)')
        return None

    def on_stop(self, core: 'LaserCore'):
        self._moving_roles = []
        if self.finished:
            return
        if not self.outcome:
            self.outcome = 'stopped'
        if self.routine.stop_started_on_abort:
            for aid, label in list(self.started.items()):
                self.started.pop(aid, None)
                a = core.get(aid)
                if a is not None and a.state != AutoState.OFF:
                    core.stop(aid, f'{self.label} {self.outcome}')
        if self.paused:
            left = [label for aid, label in self.paused.items()
                    if core.get(aid) is not None and core.get(aid).state == AutoState.PAUSED]
            if left:
                core.events.publish(EventKind.AUTOMATION_STOP, self.id,
                                    f"{self.label} {self.outcome}: still paused (resume them yourself): "
                                    f"{', '.join(left)}", Level.WARNING)

    def on_resume(self, core: 'LaserCore'):
        if self.phase == 'moving' and self.sequencer is not None:
            core.acquire(self, self._moving_roles)
            self.sequencer.set_targets(self.sequencer.targets)

    def is_done(self) -> bool:
        return self.finished

    # ------------------------------------------------------------------ helpers
    def _log(self, core: 'LaserCore', text: str, level: Level = Level.INFO):
        self.history.append((core.clock.wall(), text))
        core.events.publish(EventKind.AUTOMATION_STATE, self.id, text, level)

    def _where(self) -> str:
        return f'step {self.index + 1}/{len(self.routine.steps)}'

    def _fail(self, core: 'LaserCore', why: str):
        self.outcome = 'failed'
        self.phase = 'done'
        self.status = f'FAILED at {self._where()}: {why}'
        self.history.append((core.clock.wall(), self.status))
        core.stop(self.id, f'failed at {self._where()}: {why}', Level.WARNING)

    def _finish(self, core: 'LaserCore', why: str = ''):
        self.outcome = 'done'
        self.phase = 'done'
        self.finished = True
        self.status = 'finished' + (f' ({why})' if why else '')
        self._log(core, f'{self.label}: finished at {self._where()}' + (f' ({why})' if why else ''))

    def _go(self, core: 'LaserCore', then: str, goto: int, why: str):
        if then == 'fail':
            self._fail(core, why)
        elif then == 'end':
            self._finish(core, why)
        else:
            nxt = goto - 1 if then == 'goto' else self.index + 1
            if nxt >= len(self.routine.steps):
                self._finish(core)
                return
            self.history.append((core.clock.wall(), f'{self._where()}: {why} → step {nxt + 1}'))
            self.index = nxt
            self.phase = 'enter'

    def _check_watched(self, core: 'LaserCore') -> Optional[str]:
        """An automation started (or resumed) by the routine that is held, in error, or stopped by somebody
        else. Finishing by itself (one optimisation pass) is normal."""
        for aid, label in list(self.watched.items()):
            a = core.get(aid)
            if a is None:
                self.watched.pop(aid, None)
                continue
            if a.state == AutoState.HELD:
                return f'{label} held: {a.reason}'
            if a.state == AutoState.ERROR:
                return f'{label} in error: {a.reason}'
            if a.state == AutoState.OFF:
                self.watched.pop(aid, None)
                self.started.pop(aid, None)
                if a.reason != 'finished':
                    return f'{label} stopped: {a.reason or "?"}'
        return None

    def _forget(self, aid: str):
        for d in (self.started, self.watched, self.paused):
            d.pop(aid, None)

    # ------------------------------------------------------------------ actions
    def _enter(self, core: 'LaserCore', now: float):
        s = self.step
        n = self.index + 1
        self.visits[n] = self.visits.get(n, 0) + 1
        self._t_step = now
        self._t_wait = now
        self._wall_step = core.clock.wall()
        self._since = {}
        roles = [c.target for c in s.until if c.kind == 'travel'] + [s.target] * (s.action == 'move')
        self._pos0 = {r: core.position(r) for r in roles if r}
        self.status = describe_action(s)
        self.history.append((core.clock.wall(), f'step {n}: {describe_action(s)}'))
        a = s.action
        if a == 'none':
            pass
        elif a == 'note':
            core.events.publish(EventKind.NOTE, self.id, f'{self.label}: {s.text}', Level.INFO)
        elif a == 'start':
            d = self.host.automation(s.target)
            if d is not None and (d.state in (AutoState.ACTIVE, AutoState.ARMED) or
                                  (d.state == AutoState.PAUSED and d.pause_kind == 'preempted')):
                self.watched[d.id] = d.label                  # already running: watched, not ours to stop
            elif d is not None and d.state == AutoState.PAUSED and d.pause_kind == 'manual':
                return self._fail(core, f'{d.label} is paused: use a "Resume" step')
            else:
                if d is not None and d.state in ENGAGED_STATES:  # held / error / preempted: clean restart
                    self.host.stop(s.target, f'restarted by {self.label}')
                ok, msg = self.host.start(s.target, s.option)
                if not ok:
                    return self._fail(core, f'cannot start {s.target}: {msg}')
                d = self.host.automation(s.target)
                if d is not None:
                    self._forget(d.id)
                    self.started[d.id] = self.watched[d.id] = d.label
        elif a == 'stop':
            d = self.host.automation(s.target)
            if d is not None:
                self._forget(d.id)
            self.host.stop(s.target, f'stopped by {self.label}')
        elif a == 'pause':
            d = self.host.automation(s.target)
            if d is None:
                return self._fail(core, f'{s.target} does not exist')
            if d.state == AutoState.OFF:
                self.history.append((core.clock.wall(), f'{d.label} not running: nothing to pause'))
            else:
                ok, msg = self.host.pause(s.target, f'paused by {self.label}', self.id)
                if not ok:
                    return self._fail(core, f'cannot pause {d.label}: {msg}')
                self.watched.pop(d.id, None)
                self.paused[d.id] = d.label
        elif a == 'resume':
            d = self.host.automation(s.target)
            if d is None:
                return self._fail(core, f'{s.target} does not exist')
            if d.state == AutoState.PAUSED and d.pause_kind == 'manual':
                ok, msg = self.host.resume(s.target, f'resumed by {self.label}')
                if not ok:
                    return self._fail(core, f'cannot resume {d.label}: {msg}')
                self.paused.pop(d.id, None)
                self.watched[d.id] = d.label
            elif d.state == AutoState.HELD:
                return self._fail(core, f'{d.label} is held by a safety rule: resume it by hand')
            elif d.state == AutoState.OFF and d.id in self.paused:
                return self._fail(core, f'{d.label} was stopped while paused')
            elif d.state == AutoState.OFF:
                self.history.append((core.clock.wall(), f'{d.label} not running: nothing to resume'))
        elif a == 'move':
            pos = self._pos0.get(s.target)
            if pos is None:
                return self._fail(core, f'{s.target}: unknown position')
            target = s.value if s.option == 'absolute' else pos + s.value
            self._moving_roles = [s.target]
            core.acquire(self, self._moving_roles)      # the PIDs on this motor are preempted (resume after)
            self.sequencer = MoveSequencer(core, self)
            self.sequencer.set_targets({s.target: target})
            self.phase = 'moving'
            self.status = f'{describe_action(s)}: moving'
            return
        self.phase = 'wait'

    def _tick_moving(self, core: 'LaserCore', now: float):
        status = self.sequencer.step()
        if status == MoveSequencer.MOVING:
            return
        if status == MoveSequencer.BLOCKED:
            if self.state == AutoState.ACTIVE:       # (held: the core froze us, resume goes on moving)
                self._fail(core, f'move of {self.step.target} blocked')
            return
        self._moving_roles = []
        core.release(self)
        self.phase = 'wait'
        self._t_wait = now

    # ------------------------------------------------------------------ conditions
    def _instant(self, core: 'LaserCore', c: Condition) -> bool:
        """Is the condition true right now (before the hold time)?"""
        if c.kind == 'locked':
            d = self.host.automation(c.target)
            if not isinstance(d, PIDAutomation) or d.state != AutoState.ACTIVE:
                return False
            for m in d.loops:                          # every signal of the PID
                if not m.trace:
                    return False
                last = m.trace[-1]
                tol = c.value if c.value > 0 else abs(m.settings.deadband)
                if last.t < self._wall_step or not is_finite(last.error) or abs(last.error) > tol:
                    return False
            return True
        if c.kind == 'signal':
            ch = core.roles.channel_for(c.target)
            if ch is None or not core.signals.is_fresh(ch):
                return False
            v = core.signals.value(ch)
            if not is_finite(v):
                return False
            x = abs(v) if c.op.startswith('|') else v
            return x < c.value if c.op.endswith('<') else x > c.value
        return False

    def _satisfied(self, core: 'LaserCore', k: int, c: Condition, now: float) -> bool:
        if c.kind == 'delay':
            return now - self._t_wait >= c.value
        if c.kind == 'count':
            return self.visits.get(self.index + 1, 0) >= c.value
        if c.kind == 'travel':
            p0, p = self._pos0.get(c.target), core.position(c.target)
            return p0 is not None and p is not None and abs(p - p0) >= c.value
        if c.kind == 'finished':
            a = core.get(c.target)
            return (a is not None and a.state == AutoState.OFF and a.reason == 'finished'
                    and a.started_at is not None and a.started_at >= self._t_step - 1e-9)
        if self._instant(core, c):
            if self._since.get(k) is None:
                self._since[k] = now
            return now - self._since[k] >= c.hold_s
        self._since[k] = None
        return False

    def _tick_wait(self, core: 'LaserCore', now: float):
        s = self.step
        for k, c in enumerate(s.until):
            if self._satisfied(core, k, c, now):
                return self._go(core, c.then, c.goto, describe_condition(c).split(' → ')[0])
        if not s.until:
            return self._go(core, 'next', 0, 'done')
        if s.timeout_s > 0 and now - self._t_wait >= s.timeout_s:
            return self._go(core, s.on_timeout, s.timeout_goto, f'timeout ({s.timeout_s:g} s)')
        waited = now - self._t_wait
        self.status = f'{describe_action(s)} · waiting {waited:.0f} s' + (
            f' / {s.timeout_s:g} s' if s.timeout_s > 0 else '')

    # ------------------------------------------------------------------ loop
    def tick(self, core: 'LaserCore', now: float):
        if self.phase in ('idle', 'done'):
            return
        why = self._check_watched(core)
        if why:
            return self._fail(core, why)
        if self.step is None:
            return self._finish(core)
        if self.phase == 'enter':
            return self._enter(core, now)
        if self.phase == 'moving':
            return self._tick_moving(core, now)
        if self.phase == 'wait':
            return self._tick_wait(core, now)

    def variables(self) -> Dict[str, float]:
        return {'step': float(self.index + 1), 'visits': float(self.visits.get(self.index + 1, 0))}
