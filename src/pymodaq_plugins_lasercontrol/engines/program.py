"""Structured routines ("programs"): nested blocks instead of "go to" (docs/roadmap.md § 3).

A program has a **main sequence** and **background blocks**:

* main sequence = a list of nodes:
    - ``step``    one action (start / stop / pause / resume a PID or an optimisation, move a motor, note,
                  wait, hold) then wait until one of its conditions is true (first true wins) or its timeout;
    - ``repeat``  its body again and again (``times`` = 0: forever), left by a ``break`` outcome, an end or
                  a failure;
    - ``foreach`` its body once per **position** of a table (setpoints of the PIDs = execution data);
    - ``ramp``    moves the setpoints of the PIDs slowly (``speed`` per second) to a position (the current
                  item of the enclosing ``foreach`` when none is given), and waits while a PID is out of its
                  own tolerance: the beam is never left behind;
* background = ``every`` blocks (period, body): while the main sequence runs, a due block **interrupts it
  at any moment** — the main sequence is frozen (its ramp and its timers too: its delays and timeouts only
  count while it runs), the body runs to its end, then the main sequence goes on.

Outcomes of a condition / timeout: ``next`` (default), ``repeat`` (restart the innermost loop now),
``break`` (leave the innermost loop), ``end`` (success), ``fail``.

"Locked" conditions have one tolerance per PID (``tolerances``: {'PID:x': 0.5, 'PID:y': 0.8}): true when
every signal of every listed PID stays within its tolerance for ``hold_s``.

Safety rules are those of the step routines (engines/routine.py): started by a human (S9), ticked by the
core, what it starts / resumes is watched (held, error, stopped by somebody else = failure), what it
started is stopped on failure (option), it never resumes a safety hold, its own moves go through the core.

:func:`from_routine` converts a step routine ("go to" model) to a program: backward "go to" become
``repeat`` blocks.
"""
from __future__ import annotations

import copy
import itertools
import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Tuple

from ..core.automation import Automation
from ..core.events import EventKind, Level
from ..core.safety import SafetyPolicy
from ..core.signals import is_finite
from ..core.states import AutoState, ENGAGED_STATES
from .pid import PIDAutomation
from .routine import CONDITIONS, SIGNAL_OPS, START_OPTIONS, Routine, RoutineHost
from .sequencer import MoveSequencer

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore

PREFIX = 'RTN:'
NODE_TYPES = ('step', 'repeat', 'foreach', 'ramp')
STEP_ACTIONS = {'none': 'Wait', 'start': 'Start', 'stop': 'Stop', 'pause': 'Pause', 'resume': 'Resume',
                'move': 'Move motor', 'note': 'Note', 'hold': 'Hold (until stopped)',
                'confirm': 'Confirm at maximum'}
#: when a "confirm at maximum" step does not confirm: ask the operator (default), fail, or go on
ON_FAIL = {'ask': 'ask the operator', 'fail': 'FAIL', 'next': 'go on'}
#: answers of the operator to a question of a routine
ANSWERS = {'continue': '▶ Continue', 'check': '↻ Check again', 'stop': '■ Stop'}
OUTCOMES = {'next': 'next', 'repeat': 'repeat the loop', 'break': 'leave the loop', 'end': 'END', 'fail': 'FAIL'}
AUTOMATION_ACTIONS = ('start', 'stop', 'pause', 'resume', 'confirm')
_uid = itertools.count(1)


def _num(v, default: float = 0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


# ====================================================================== data
@dataclass
class Cond:
    kind: str = 'delay'                     # delay | locked | signal | travel | finished | count
    target: str = ''
    op: str = '<'
    value: float = 0.0
    hold_s: float = 0.0
    outcome: str = 'next'
    tolerances: Dict[str, float] = field(default_factory=dict)    # locked: {PID id: |error| tolerance}

    @classmethod
    def from_dict(cls, d: dict) -> 'Cond':
        d = dict(d or {})
        c = cls(kind=str(d.get('kind', 'delay')), target=str(d.get('target', '') or ''), op=str(d.get('op', '<')),
                value=_num(d.get('value')), hold_s=max(_num(d.get('hold_s')), 0.0),
                outcome=str(d.get('outcome', 'next')),
                tolerances={str(k): _num(v) for k, v in (d.get('tolerances') or {}).items()})
        if c.op not in SIGNAL_OPS:
            c.op = '<'
        return c

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'target': self.target, 'op': self.op, 'value': self.value,
                'hold_s': self.hold_s, 'outcome': self.outcome, 'tolerances': dict(self.tolerances)}

    def pids(self) -> Dict[str, float]:
        """locked: {PID id: tolerance} (a single ``target`` with ``value`` is the short form)."""
        out = dict(self.tolerances)
        if self.target and self.target not in out:
            out[self.target] = self.value
        return out


@dataclass
class Position:
    name: str = ''
    setpoints: Dict[str, List[float]] = field(default_factory=dict)    # PID id -> one value per signal

    @classmethod
    def from_dict(cls, d: dict) -> 'Position':
        d = dict(d or {})
        return cls(name=str(d.get('name', '') or ''),
                   setpoints={str(k): [_num(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
                              for k, v in (d.get('setpoints') or {}).items()})

    def to_dict(self) -> dict:
        return {'name': self.name, 'setpoints': {k: list(v) for k, v in self.setpoints.items()}}


@dataclass
class Node:
    type: str = 'step'
    # step
    action: str = 'none'
    target: str = ''
    value: float = 0.0
    option: str = ''
    text: str = ''
    until: List[Cond] = field(default_factory=list)
    timeout_s: float = 0.0
    on_timeout: str = 'fail'
    on_fail: str = 'ask'                  # confirm: not confirmed -> ask | fail | next
    # repeat / foreach
    times: int = 0                        # repeat: 0 = forever
    body: List['Node'] = field(default_factory=list)
    positions: List[Position] = field(default_factory=list)   # foreach: the table; ramp: [target] or [] = item
    # ramp
    speed: float = 0.0                    # setpoint units per second
    tolerances: Dict[str, float] = field(default_factory=dict)   # ramp: {PID id: tolerance}
    uid: int = field(default_factory=lambda: next(_uid), compare=False)
    origin: Optional[int] = field(default=None, compare=False)   # converted step routine: index of its step

    @classmethod
    def from_dict(cls, d: dict) -> 'Node':
        d = dict(d or {})
        return cls(type=str(d.get('type', 'step')), action=str(d.get('action', 'none')),
                   target=str(d.get('target', '') or ''), value=_num(d.get('value')),
                   option=str(d.get('option', '') or ''), text=str(d.get('text', '') or ''),
                   until=[Cond.from_dict(c) for c in d.get('until', []) or []],
                   timeout_s=max(_num(d.get('timeout_s')), 0.0), on_timeout=str(d.get('on_timeout', 'fail')),
                   on_fail=str(d.get('on_fail', 'ask')),
                   times=int(_num(d.get('times'))), body=[Node.from_dict(b) for b in d.get('body', []) or []],
                   positions=[Position.from_dict(p) for p in d.get('positions', []) or []],
                   speed=_num(d.get('speed')),
                   tolerances={str(k): _num(v) for k, v in (d.get('tolerances') or {}).items()})

    def to_dict(self) -> dict:
        d = {'type': self.type}
        if self.type == 'step':
            d.update(action=self.action, target=self.target, value=self.value, option=self.option, text=self.text,
                     until=[c.to_dict() for c in self.until], timeout_s=self.timeout_s, on_timeout=self.on_timeout)
            if self.action == 'confirm':
                d['on_fail'] = self.on_fail
        elif self.type == 'repeat':
            d.update(times=self.times, text=self.text, body=[b.to_dict() for b in self.body])
        elif self.type == 'foreach':
            d.update(text=self.text, positions=[p.to_dict() for p in self.positions],
                     body=[b.to_dict() for b in self.body])
        elif self.type == 'ramp':
            d.update(speed=self.speed, text=self.text, positions=[p.to_dict() for p in self.positions],
                     tolerances=dict(self.tolerances), timeout_s=self.timeout_s, on_timeout=self.on_timeout)
        return d


@dataclass
class Every:
    period_s: float = 30.0
    text: str = ''
    body: List[Node] = field(default_factory=list)
    uid: int = field(default_factory=lambda: next(_uid), compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> 'Every':
        d = dict(d or {})
        return cls(period_s=max(_num(d.get('period_s'), 30.0), 0.1), text=str(d.get('text', '') or ''),
                   body=[Node.from_dict(b) for b in d.get('body', []) or []])

    def to_dict(self) -> dict:
        return {'period_s': self.period_s, 'text': self.text, 'body': [b.to_dict() for b in self.body]}


@dataclass
class Program:
    id: str = 'Routine_1'
    label: str = ''
    show_in_alignment: bool = True
    stop_started_on_abort: bool = True
    description: str = ''
    main: List[Node] = field(default_factory=list)
    background: List[Every] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> 'Program':
        d = dict(d or {})
        if 'steps' in d and 'main' not in d:              # a step routine ("go to" model)
            return from_routine(Routine.from_dict(d))
        return cls(id=str(d.get('id', 'Routine_1')), label=str(d.get('label', '') or ''),
                   show_in_alignment=bool(d.get('show_in_alignment', True)),
                   stop_started_on_abort=bool(d.get('stop_started_on_abort', True)),
                   description=str(d.get('description', '') or ''),
                   main=[Node.from_dict(n) for n in d.get('main', []) or []],
                   background=[Every.from_dict(e) for e in d.get('background', []) or []])

    def to_dict(self) -> dict:
        return {'id': self.id, 'label': self.label, 'show_in_alignment': self.show_in_alignment,
                'stop_started_on_abort': self.stop_started_on_abort, 'description': self.description,
                'main': [n.to_dict() for n in self.main], 'background': [e.to_dict() for e in self.background]}

    def name(self) -> str:
        return self.label or self.id

    def walk(self):
        """Every node of the program (main sequence and background), depth first."""
        def rec(nodes):
            for n in nodes:
                yield n
                yield from rec(n.body)
        yield from rec(self.main)
        for e in self.background:
            yield from rec(e.body)


# ====================================================================== editing (the block editor)
def find(p: Program, uid: int):
    """(list containing the block, its index, owner) — owner = enclosing Node, an Every, or None (main
    sequence / background list). The block is a Node or an Every. None when not found."""
    def rec(nodes, owner):
        for i, n in enumerate(nodes):
            if n.uid == uid:
                return nodes, i, owner
            r = rec(n.body, n)
            if r is not None:
                return r
        return None
    r = rec(p.main, None)
    if r is not None:
        return r
    for i, e in enumerate(p.background):
        if e.uid == uid:
            return p.background, i, None
        r = rec(e.body, e)
        if r is not None:
            return r
    return None


def block_of(p: Program, uid: int):
    f = find(p, uid)
    return f[0][f[1]] if f is not None else None


def inside_foreach(p: Program, uid: int) -> bool:
    """Is the block inside a FOR EACH (its ramp uses the position of the loop)?"""
    def rec(nodes, in_fe):
        for n in nodes:
            if n.uid == uid:
                return in_fe
            r = rec(n.body, in_fe or n.type == 'foreach')
            if r is not None:
                return r
        return None
    r = rec(p.main, False)
    if r is None:
        for e in p.background:
            r = rec(e.body, False)
            if r is not None:
                break
    return bool(r)


def insert_block(p: Program, block, at_uid: Optional[int] = None) -> object:
    """Insert ``block``: an Every goes to the background; a Node goes into the selected container (REPEAT /
    FOR EACH / EVERY: at its end), after the selected step, or at the end of the main sequence."""
    if isinstance(block, Every):
        p.background.append(block)
        return block
    sel = block_of(p, at_uid) if at_uid is not None else None
    if isinstance(sel, Every) or (isinstance(sel, Node) and sel.type in ('repeat', 'foreach')):
        sel.body.append(block)
    elif sel is not None:
        nodes, i, _ = find(p, at_uid)
        nodes.insert(i + 1, block)
    else:
        p.main.append(block)
    return block


def remove_block(p: Program, uid: int) -> bool:
    f = find(p, uid)
    if f is None:
        return False
    nodes, i, _ = f
    nodes.pop(i)
    return True


def move_block(p: Program, uid: int, delta: int) -> bool:
    """Up / down among its siblings."""
    f = find(p, uid)
    if f is None:
        return False
    nodes, i, _ = f
    j = i + delta
    if not 0 <= j < len(nodes):
        return False
    nodes[i], nodes[j] = nodes[j], nodes[i]
    return True


def duplicate_block(p: Program, uid: int):
    f = find(p, uid)
    if f is None:
        return None
    nodes, i, _ = f
    clone = type(nodes[i]).from_dict(nodes[i].to_dict())      # fresh uids
    nodes.insert(i + 1, clone)
    return clone


# ====================================================================== conversion from step routines
class ConversionError(ValueError):
    pass


def from_routine(r: Routine) -> Program:
    """Step routine ("go to" model) → program. A backward "go to" from step b to step a makes steps a…b a
    ``repeat`` block: "go to a" from its last step = end of the body (the loop restarts), from another step =
    ``repeat``; "next" from its last step = ``break``. Forward jumps and crossing loops are refused."""
    steps = r.steps
    n = len(steps)
    loops: Dict[int, int] = {}                           # start -> end (0-based, inclusive)
    for i, s in enumerate(steps):
        gotos = [c.goto - 1 for c in s.until if c.then == 'goto']
        if s.timeout_s > 0 and s.on_timeout == 'goto':
            gotos.append(s.timeout_goto - 1)
        for g in gotos:
            if not 0 <= g < n:
                raise ConversionError(f'step {i + 1}: "go to {g + 1}" does not exist')
            if g > i:
                raise ConversionError(f'step {i + 1}: forward "go to {g + 1}" cannot be converted')
            loops[g] = max(loops.get(g, g), i)
    spans = sorted(loops.items())
    for (a1, b1), (a2, b2) in itertools.combinations(spans, 2):
        if a1 < a2 <= b1 < b2:
            raise ConversionError(f'loops {a1 + 1}…{b1 + 1} and {a2 + 1}…{b2 + 1} cross each other')

    def innermost(i: int) -> Optional[Tuple[int, int]]:
        inside = [(a, b) for a, b in spans if a <= i <= b]
        return min(inside, key=lambda ab: ab[1] - ab[0]) if inside else None

    def outcome(i: int, then: str, goto: int) -> str:
        if then in ('end', 'fail'):
            return then
        loop = innermost(i)
        if then == 'goto':
            if loop is None or goto - 1 != loop[0]:
                raise ConversionError(f'step {i + 1}: "go to {goto}" is not the start of its innermost loop')
            return 'next' if i == loop[1] else 'repeat'
        return 'break' if loop is not None and i == loop[1] else 'next'      # 'next'

    def leaf(i: int) -> Node:
        s = steps[i]
        node = Node(type='step', action=s.action, target=s.target, value=s.value, option=s.option, text=s.text,
                    timeout_s=s.timeout_s,
                    on_timeout=outcome(i, s.on_timeout, s.timeout_goto) if s.timeout_s > 0 else 'fail', origin=i)
        for c in s.until:
            node.until.append(Cond(kind=c.kind, target=c.target, op=c.op, value=c.value, hold_s=c.hold_s,
                                   outcome=outcome(i, c.then, c.goto)))
        if not s.until and outcome(i, 'next', 0) == 'break':
            node.until.append(Cond('delay', value=0.0, outcome='break'))     # "when done → next" left the loop
        return node

    def build(lo: int, hi: int, own: Optional[Tuple[int, int]] = None) -> List[Node]:
        """Nodes of steps lo…hi; ``own`` = the loop whose body this is (not nested in itself again)."""
        out, i = [], lo
        while i <= hi:
            b = loops.get(i)
            if b is not None and b <= hi and (i, b) != own:
                out.append(Node(type='repeat', times=0, body=build(i, b, (i, b))))
                i = b + 1
            else:
                out.append(leaf(i))
                i += 1
        return out

    return Program(id=r.id, label=r.label, show_in_alignment=r.show_in_alignment,
                   stop_started_on_abort=r.stop_started_on_abort, description=r.description,
                   main=build(0, n - 1) if n else [])


# ====================================================================== validation / text
def validate_program(p: Program) -> List[str]:
    errors = []
    if not p.main:
        errors.append('the main sequence is empty')

    def check(nodes: List[Node], where: str, loop_depth: int, in_foreach: bool):
        for k, node in enumerate(nodes, start=1):
            w = f'{where}{k}'
            if node.type not in NODE_TYPES:
                errors.append(f'{w}: unknown block {node.type!r}')
                continue
            if node.type == 'step':
                if node.action not in STEP_ACTIONS:
                    errors.append(f'{w}: unknown action {node.action!r}')
                if node.action in AUTOMATION_ACTIONS + ('move',) and not node.target:
                    errors.append(f'{w}: {STEP_ACTIONS.get(node.action, node.action).lower()} needs a target')
                if node.action == 'move' and node.value == 0 and node.option != 'absolute':
                    errors.append(f'{w}: move by 0')
                if node.action == 'confirm':
                    if node.target and not node.target.startswith('OPT:'):
                        errors.append(f'{w}: "confirm at maximum" needs an optimization')
                    if node.on_fail not in ON_FAIL:
                        errors.append(f'{w}: unknown reaction {node.on_fail!r}')
                outs = [c.outcome for c in node.until] + ([node.on_timeout] if node.timeout_s > 0 else [])
                for c in node.until:
                    if c.kind not in CONDITIONS:
                        errors.append(f'{w}: unknown condition {c.kind!r}')
                    elif c.kind == 'locked' and not c.pids():
                        errors.append(f'{w}: "PID converged" needs at least one PID')
                    elif c.kind in ('signal', 'travel', 'finished') and not c.target:
                        errors.append(f'{w}: {CONDITIONS[c.kind].lower()} needs a target')
                for o in outs:
                    if o not in OUTCOMES:
                        errors.append(f'{w}: unknown outcome {o!r}')
                    elif o in ('repeat', 'break') and loop_depth == 0:
                        errors.append(f'{w}: "{OUTCOMES[o]}" outside a loop')
            elif node.type == 'repeat':
                if not node.body:
                    errors.append(f'{w}: empty REPEAT')
                check(node.body, f'{w}.', loop_depth + 1, in_foreach)
            elif node.type == 'foreach':
                if not node.positions:
                    errors.append(f'{w}: FOR EACH without position')
                if not node.body:
                    errors.append(f'{w}: empty FOR EACH')
                check(node.body, f'{w}.', loop_depth + 1, True)
            elif node.type == 'ramp':
                if node.speed <= 0:
                    errors.append(f'{w}: the ramp speed must be > 0')
                if not node.positions and not in_foreach:
                    errors.append(f'{w}: ramp without position (outside a FOR EACH)')

    check(p.main, '', 0, False)
    for j, e in enumerate(p.background, start=1):
        if not e.body:
            errors.append(f'EVERY {e.period_s:g} s: empty')
        check(e.body, f'every {j}: ', 0, False)
    return errors


def outcome_text(o: str) -> str:
    return OUTCOMES.get(o, o)


def describe_node(n: Node) -> str:
    if n.type == 'repeat':
        return 'REPEAT forever' if n.times <= 0 else f'REPEAT {n.times}×'
    if n.type == 'foreach':
        return 'FOR EACH position in ' + ' · '.join(p.name or '?' for p in n.positions)
    if n.type == 'ramp':
        to = n.positions[0].name if n.positions else 'the position'
        return f'Ramp the setpoints to {to} at {n.speed:g}/s'
    t = n.target or '?'
    if n.action == 'none':
        return 'Wait' + (f' — {n.text}' if n.text else '')
    if n.action == 'note':
        return f'Note: “{n.text}”'
    if n.action == 'hold':
        return 'Hold (until stopped)'
    if n.action == 'start':
        opt = f' ({START_OPTIONS[n.option]})' if t.startswith('OPT:') and n.option in START_OPTIONS else ''
        return f'Start {t}{opt}'
    if n.action == 'move':
        return f"Move {t} {'to' if n.option == 'absolute' else 'by'} {n.value:+g}"
    if n.action == 'confirm':
        return f'Confirm at maximum: {t}'
    return f'{STEP_ACTIONS.get(n.action, n.action)} {t}'


def describe_cond(c: Cond) -> str:
    if c.kind == 'delay':
        what = f'after {c.value:g} s'
    elif c.kind == 'locked':
        what = ' · '.join(f'{p} |err| ≤ {tol:g}' for p, tol in c.pids().items()) + f' for {c.hold_s:g} s'
    elif c.kind == 'signal':
        op = c.op.replace('x', c.target) if c.op.startswith('|') else f'{c.target} {c.op}'
        what = f'{op} {c.value:g}' + (f' for {c.hold_s:g} s' if c.hold_s > 0 else '')
    elif c.kind == 'travel':
        what = f'{c.target} moved ≥ {c.value:g}'
    elif c.kind == 'finished':
        what = f'{c.target} finished'
    elif c.kind == 'count':
        what = f'entered ≥ {int(c.value)}×'
    else:
        what = c.kind
    return f'{what} → {outcome_text(c.outcome)}'


# ====================================================================== runner
@dataclass
class _Frame:
    nodes: List[Node]
    i: int = 0
    owner: Optional[Node] = None          # repeat / foreach node (None: a root sequence)
    iteration: int = 0                    # repeat: done iterations ; foreach: index of the current item


class _Thread:
    """One sequence being executed (the main sequence, or the body of a background block)."""

    def __init__(self, nodes: List[Node], name: str):
        self.name = name
        self.frames: List[_Frame] = [_Frame(nodes)]
        self.t = 0.0                      # its own clock: only runs while the thread runs
        self.phase = 'enter'              # enter | moving | wait | ramp | done
        self.t_wait = 0.0                 # thread time at the end of the action
        self.wall_step = 0.0
        self.core_enter = 0.0             # core (monotonic) time at the start of the step
        self.since: Dict[int, Optional[float]] = {}
        self.pos0: Dict[str, Optional[float]] = {}
        self.ramp_targets: Dict[Tuple[str, int], float] = {}
        self.sequencer: Optional[MoveSequencer] = None

    @property
    def node(self) -> Optional[Node]:
        f = self.frames[-1] if self.frames else None
        return f.nodes[f.i] if f is not None and f.i < len(f.nodes) else None

    def current_item(self) -> Optional[Position]:
        for f in reversed(self.frames):
            if f.owner is not None and f.owner.type == 'foreach' and f.iteration < len(f.owner.positions):
                return f.owner.positions[f.iteration]
        return None

    def path(self) -> str:
        parts = []
        for f in self.frames:
            parts.append(str(f.i + 1))
        return '.'.join(parts)


class ProgramAutomation(Automation):
    kind = 'routine'
    acquire_at_start = False

    def __init__(self, program: Program, host: RoutineHost):
        super().__init__(PREFIX + program.id, program.name())
        self.program = program
        self.host = host
        self.period_s = 0.1
        self.main: Optional[_Thread] = None
        self.background: Optional[_Thread] = None       # the running background block (it interrupts main)
        self.bg_block: Optional[Every] = None
        self.bg_due: Dict[int, float] = {}
        self.status = ''
        self.outcome = ''
        self.finished = False
        self.started: Dict[str, str] = {}
        self.watched: Dict[str, str] = {}
        self.paused: Dict[str, str] = {}
        self.visits: Dict[int, int] = {}
        self.history: Deque[Tuple[float, str]] = deque(maxlen=400)
        self.interruptions = 0
        self._last_now: Optional[float] = None
        self._moving_roles: List[str] = []
        self.question: Optional[dict] = None     # {'text', 'choices'}: waiting for the operator
        self._asking: Optional[_Thread] = None
        self._repause: Dict[str, str] = {}       # resumed for the question, paused again on "check again"

    # ------------------------------------------------------------------ description
    def motor_roles(self) -> List[str]:
        return list(dict.fromkeys(n.target for n in self.program.walk()
                                  if n.type == 'step' and n.action == 'move' and n.target))

    def wanted_motor_roles(self) -> List[str]:
        return list(self._moving_roles)

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy(check_data=False, check_intensity=False, check_rate=True, envelope='excursion')

    def summary(self) -> str:
        return f'{self.label}: {len(self.program.main)} blocks, {len(self.program.background)} in the background'

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = validate_program(self.program)
        names = []
        for n in self.program.walk():
            if n.type == 'step' and n.action in AUTOMATION_ACTIONS:
                names.append(n.target)
            for c in n.until:
                if c.kind == 'locked':
                    names += list(c.pids())
                elif c.kind == 'finished':
                    names.append(c.target)
                elif c.kind == 'signal' and core.roles.channel_for(c.target) is None:
                    errors.append(f"signal role '{c.target}' not declared")
                elif c.kind == 'travel' and core.roles.module_for(c.target) is None:
                    errors.append(f"motor role '{c.target}' not declared")
            for pos in n.positions:
                names += list(pos.setpoints)
            names += list(n.tolerances)
        for t in dict.fromkeys(x for x in names if x):
            if t.startswith(PREFIX):
                errors.append(f'a routine cannot drive another routine ({t})')
                continue
            err = self.host.check_target(t)
            if err:
                errors.append(err)
        return errors

    def current_origin(self) -> Optional[int]:
        """Converted step routine: index of the step running (or frozen) in the main sequence."""
        node = self.main.node if self.main is not None else None
        return node.origin if node is not None else None

    @property
    def current(self) -> Tuple[Optional[Node], Optional[Node]]:
        """(node running now, node of the main sequence frozen by a background block or None)."""
        if self.background is not None:
            return self.background.node, self.main.node if self.main else None
        return (self.main.node if self.main else None), None

    # ------------------------------------------------------------------ lifecycle
    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.main = _Thread(self.program.main, 'main')
        self.background, self.bg_block = None, None
        self.bg_due = {e.uid: e.period_s for e in self.program.background}   # main-thread clock
        self.status, self.outcome, self.finished = 'started', '', False
        self.started, self.watched, self.paused, self.visits = {}, {}, {}, {}
        self.interruptions = 0
        self._last_now = None
        self._moving_roles = []
        self.question, self._asking, self._repause = None, None, {}
        self._log(core, f'{self.label}: started')
        return None

    # ------------------------------------------------------------------ operator
    def ask(self, core: 'LaserCore', th: _Thread, text: str):
        """Wait for the operator: what the routine paused is resumed (the PIDs hold the beam), the routine
        stays where it is until an answer (Continue / Check again / Stop)."""
        self._repause = {}
        for aid, label in list(self.paused.items()):
            a = core.get(aid)
            if a is not None and a.state == AutoState.PAUSED and a.pause_kind == 'manual':
                ok, _ = core.resume(aid, f'{self.label}: waiting for the operator')
                if ok:
                    self._repause[aid] = label
                    self.paused.pop(aid, None)
                    self.watched[aid] = label
        self.question = {'text': text, 'choices': list(ANSWERS)}
        self._asking = th
        th.phase = 'ask'
        self._log(core, f'⚠ {self.label}: {text} — waiting for the operator', Level.WARNING)

    def answer(self, core: 'LaserCore', choice: str) -> Tuple[bool, str]:
        """The operator's answer: 'continue' (go on), 'check' (pause again and run the step again), 'stop'."""
        if self.question is None or self._asking is None:
            return False, 'no question pending'
        if choice not in ANSWERS:
            return False, f'unknown answer {choice!r}'
        th = self._asking
        self.question, self._asking = None, None
        self._log(core, f'{self.label}: operator answered "{ANSWERS[choice]}"')
        if choice == 'stop':
            self.outcome = 'stopped'
            core.stop(self.id, 'stopped by the operator')
            return True, ''
        if choice == 'continue':
            self._go(core, th, 'next', 'operator: continue')
            return True, ''
        for aid, label in self._repause.items():                 # 'check': same conditions as before
            a = core.get(aid)
            if a is not None and a.state == AutoState.ACTIVE:
                ok, msg = self.host.pause(aid, f'paused by {self.label}', self.id)
                if not ok:
                    self._fail(core, f'cannot pause {label} again: {msg}')
                    return False, msg
                self.watched.pop(aid, None)
                self.paused[aid] = label
        self._repause = {}
        th.phase = 'enter'
        return True, ''

    def on_stop(self, core: 'LaserCore'):
        self._moving_roles = []
        if self.finished:
            return
        if not self.outcome:
            self.outcome = 'stopped'
        if self.program.stop_started_on_abort:
            for aid in list(self.started):
                self.started.pop(aid, None)
                a = core.get(aid)
                if a is not None and a.state != AutoState.OFF:
                    core.stop(aid, f'{self.label} {self.outcome}')
        left = [lab for aid, lab in self.paused.items()
                if core.get(aid) is not None and core.get(aid).state == AutoState.PAUSED]
        if left:
            core.events.publish(EventKind.AUTOMATION_STOP, self.id, f"{self.label} {self.outcome}: still paused "
                                f"(resume them yourself): {', '.join(left)}", Level.WARNING)

    def on_resume(self, core: 'LaserCore'):
        for th in (self.main, self.background):
            if th is not None and th.phase == 'moving' and th.sequencer is not None:
                core.acquire(self, self._moving_roles)
                th.sequencer.set_targets(th.sequencer.targets)
        self._last_now = None

    def is_done(self) -> bool:
        return self.finished

    # ------------------------------------------------------------------ helpers
    def _log(self, core: 'LaserCore', text: str, level: Level = Level.INFO):
        self.history.append((core.clock.wall(), text))
        core.events.publish(EventKind.AUTOMATION_STATE, self.id, text, level)

    def _note(self, core: 'LaserCore', text: str):
        self.history.append((core.clock.wall(), text))

    def _fail(self, core: 'LaserCore', why: str):
        self.outcome = 'failed'
        self.status = f'FAILED: {why}'
        self.history.append((core.clock.wall(), self.status))
        core.stop(self.id, f'failed: {why}', Level.WARNING)

    def _finish(self, core: 'LaserCore', why: str = ''):
        self.outcome = 'done'
        self.finished = True
        self.status = 'finished' + (f' ({why})' if why else '')
        self._log(core, f'{self.label}: finished' + (f' ({why})' if why else ''))

    def _forget(self, aid: str):
        for d in (self.started, self.watched, self.paused):
            d.pop(aid, None)

    def _check_watched(self, core: 'LaserCore') -> Optional[str]:
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

    # ------------------------------------------------------------------ tick
    def tick(self, core: 'LaserCore', now: float):
        dt = 0.0 if self._last_now is None else max(now - self._last_now, 0.0)
        self._last_now = now
        why = self._check_watched(core)
        if why:
            return self._fail(core, why)
        if (self.background is None and self.main is not None and self.main.phase != 'moving'
                and self.main.node is not None):                  # (not when the main sequence is ending)
            for e in self.program.background:                    # a due block interrupts the main sequence
                if self.main.t >= self.bg_due.get(e.uid, math.inf):
                    self.background, self.bg_block = _Thread(e.body, f'every {e.period_s:g} s'), e
                    self.interruptions += 1
                    self._note(core, f'⏱ every {e.period_s:g} s: main sequence paused')
                    break
        th = self.background if self.background is not None else self.main
        if th is None:
            return
        th.t += dt
        self._run(core, th, now)
        if self.background is not None and self.background.phase == 'done' and self.outcome == '':
            e = self.bg_block
            self.bg_due[e.uid] = self.main.t + e.period_s
            self.background, self.bg_block = None, None
            self._note(core, '⏱ background block done: main sequence goes on')
        node, frozen = self.current
        if self.outcome == '' and not self.finished:
            label = describe_node(node) if node is not None else ''
            if self.question is not None:
                label = f"⚠ waiting for the operator: {self.question['text']}"
            self.status = (f'‖ {describe_node(frozen)} — ' if frozen is not None else '') + label

    def _run(self, core: 'LaserCore', th: _Thread, now: float):
        """Advance the thread: containers are entered at once, at most one step is executed per tick."""
        for _ in range(64):                                       # bounded: never loops on empty bodies
            if self.outcome or self.finished or th.phase == 'done':
                return
            f = th.frames[-1]
            if f.i >= len(f.nodes):                               # end of a sequence
                if not self._end_of_frame(core, th):
                    return
                continue
            node = f.nodes[f.i]
            if node.type in ('repeat', 'foreach'):
                if node.type == 'foreach' and not node.positions:
                    f.i += 1
                    continue
                th.frames.append(_Frame(node.body, 0, node, 0))
                self._note(core, f'{th.name}: {describe_node(node)}')
                continue
            if th.phase == 'enter':
                self._enter(core, th, node, now)
                if th.phase == 'enter':                           # (failed)
                    return
                return
            if th.phase == 'moving':
                return self._tick_moving(core, th, node)
            if th.phase == 'ramp':
                return self._tick_ramp(core, th, node, now)
            if th.phase == 'wait':
                return self._tick_wait(core, th, node, now)
            if th.phase == 'confirm':
                return self._tick_confirm(core, th, node)
            return                                                # 'ask': nothing until the operator answers

    def _end_of_frame(self, core: 'LaserCore', th: _Thread) -> bool:
        f = th.frames[-1]
        owner = f.owner
        if owner is None:                                        # end of the root sequence
            if th is self.main:
                self._finish(core)
            th.phase = 'done'
            return False
        f.iteration += 1
        if owner.type == 'repeat' and (owner.times <= 0 or f.iteration < owner.times):
            f.i = 0
            return True
        if owner.type == 'foreach' and f.iteration < len(owner.positions):
            f.i = 0
            self._note(core, f'{th.name}: next position {owner.positions[f.iteration].name}')
            return True
        th.frames.pop()
        th.frames[-1].i += 1
        return True

    def _go(self, core: 'LaserCore', th: _Thread, outcome: str, why: str):
        node = th.node
        self._note(core, f'{th.name}: {describe_node(node)} — {why} → {outcome_text(outcome)}')
        th.phase = 'enter'
        if outcome == 'fail':
            return self._fail(core, why)
        if outcome == 'end':
            if th is not self.main:
                self.background = None
            return self._finish(core, why)
        if outcome in ('repeat', 'break'):
            k = max((j for j, fr in enumerate(th.frames) if fr.owner is not None), default=None)
            if k is None:
                return self._fail(core, f'"{outcome_text(outcome)}" outside a loop')
            del th.frames[k + 1:]
            if outcome == 'repeat':
                th.frames[k].i = len(th.frames[k].nodes)         # end of the body: the loop goes on
            else:
                th.frames.pop()
                th.frames[-1].i += 1
            return
        th.frames[-1].i += 1

    # ------------------------------------------------------------------ steps
    def _enter(self, core: 'LaserCore', th: _Thread, node: Node, now: float):
        self.visits[node.uid] = self.visits.get(node.uid, 0) + 1
        th.t_wait = th.t
        th.wall_step = core.clock.wall()
        th.core_enter = core.clock.now()
        th.since = {}
        roles = [c.target for c in node.until if c.kind == 'travel'] + ([node.target] if node.action == 'move' else [])
        th.pos0 = {r: core.position(r) for r in roles if r}
        self._note(core, f'{th.name}: {describe_node(node)}')
        if node.type == 'ramp':
            item = node.positions[0] if node.positions else th.current_item()
            if item is None:
                return self._fail(core, 'ramp without position')
            th.ramp_targets = {}
            for pid_id, values in item.setpoints.items():
                p = self.host.pid(pid_id)
                if p is None or p.state not in (AutoState.ACTIVE, AutoState.PAUSED):
                    return self._fail(core, f'ramp: {pid_id} is not running')
                for k, v in enumerate(values[:len(p.loops)]):
                    th.ramp_targets[(pid_id, k)] = float(v)
            th.phase = 'ramp'
            return
        a = node.action
        if a in ('none', 'hold'):
            pass
        elif a == 'note':
            core.events.publish(EventKind.NOTE, self.id, f'{self.label}: {node.text}', Level.INFO)
        elif a == 'start':
            d = self.host.automation(node.target)
            if d is not None and (d.state in (AutoState.ACTIVE, AutoState.ARMED) or
                                  (d.state == AutoState.PAUSED and d.pause_kind == 'preempted')):
                self.watched[d.id] = d.label
            elif d is not None and d.state == AutoState.PAUSED and d.pause_kind == 'manual':
                return self._fail(core, f'{d.label} is paused: use a "Resume" step')
            else:
                if d is not None and d.state in ENGAGED_STATES:
                    self.host.stop(node.target, f'restarted by {self.label}')
                ok, msg = self.host.start(node.target, node.option)
                if not ok:
                    return self._fail(core, f'cannot start {node.target}: {msg}')
                d = self.host.automation(node.target)
                if d is not None:
                    self._forget(d.id)
                    self.started[d.id] = self.watched[d.id] = d.label
        elif a == 'confirm':
            d = self.host.automation(node.target)
            if d is not None and d.state in ENGAGED_STATES:
                return self._fail(core, f'{d.label} is already running')
            ok, msg = self.host.start(node.target, 'confirm')
            if not ok:
                return self._fail(core, f'cannot start {node.target}: {msg}')
            d = self.host.automation(node.target)
            if d is not None:
                self._forget(d.id)
                self.started[d.id] = self.watched[d.id] = d.label
            th.phase = 'confirm'
            return
        elif a == 'stop':
            d = self.host.automation(node.target)
            if d is not None:
                self._forget(d.id)
            self.host.stop(node.target, f'stopped by {self.label}')
        elif a == 'pause':
            d = self.host.automation(node.target)
            if d is None:
                return self._fail(core, f'{node.target} does not exist')
            if d.state != AutoState.OFF:
                ok, msg = self.host.pause(node.target, f'paused by {self.label}', self.id)
                if not ok:
                    return self._fail(core, f'cannot pause {d.label}: {msg}')
                self.watched.pop(d.id, None)
                self.paused[d.id] = d.label
        elif a == 'resume':
            d = self.host.automation(node.target)
            if d is None:
                return self._fail(core, f'{node.target} does not exist')
            if d.state == AutoState.PAUSED and d.pause_kind == 'manual':
                ok, msg = self.host.resume(node.target, f'resumed by {self.label}')
                if not ok:
                    return self._fail(core, f'cannot resume {d.label}: {msg}')
                self.paused.pop(d.id, None)
                self.watched[d.id] = d.label
            elif d.state == AutoState.HELD:
                return self._fail(core, f'{d.label} is held by a safety rule: resume it by hand')
            elif d.state == AutoState.OFF and d.id in self.paused:
                return self._fail(core, f'{d.label} was stopped while paused')
        elif a == 'move':
            pos = th.pos0.get(node.target)
            if pos is None:
                return self._fail(core, f'{node.target}: unknown position')
            self._moving_roles = [node.target]
            core.acquire(self, self._moving_roles)
            th.sequencer = MoveSequencer(core, self)
            th.sequencer.set_targets({node.target: node.value if node.option == 'absolute' else pos + node.value})
            th.phase = 'moving'
            return
        th.phase = 'wait'

    def _tick_moving(self, core: 'LaserCore', th: _Thread, node: Node):
        status = th.sequencer.step()
        if status == MoveSequencer.MOVING:
            return
        if status == MoveSequencer.BLOCKED:
            if self.state == AutoState.ACTIVE:
                self._fail(core, f'move of {node.target} blocked')
            return
        self._moving_roles = []
        core.release(self)
        th.phase = 'wait'
        th.t_wait = th.t

    def _tick_confirm(self, core: 'LaserCore', th: _Thread, node: Node):
        d = self.host.automation(node.target)
        if d is None:
            return self._fail(core, f'{node.target} disappeared')
        if d.state in ENGAGED_STATES:
            return                                               # (held / error: _check_watched fails)
        result = getattr(d, 'last_result', {}) or {}
        if result.get('confirmed'):
            return self._go(core, th, 'next', f"✔ {result.get('message', 'at maximum')}")
        why = result.get('message') or 'maximum not confirmed'
        if node.on_fail == 'fail':
            return self._go(core, th, 'fail', why)
        if node.on_fail == 'next':
            return self._go(core, th, 'next', why)
        self.ask(core, th, why)

    def _within(self, pid_id: str, tol: float) -> bool:
        p = self.host.pid(pid_id)
        if p is None:
            return False
        for loop in p.loops:
            e = loop.controller.last.error
            if not is_finite(e) or abs(e) > tol:
                return False
        return True

    def _tick_ramp(self, core: 'LaserCore', th: _Thread, node: Node, now: float):
        step = node.speed * max(th.t - th.t_wait, 0.0)
        th.t_wait = th.t
        tols = node.tolerances or {pid: math.inf for pid, _ in th.ramp_targets}
        if not all(self._within(pid, tol) for pid, tol in tols.items()):
            self.status = f'{describe_node(node)}: waiting for the PIDs (out of tolerance)'
            return                                               # the beam lags: the setpoints wait
        done = True
        for (pid_id, k), target in th.ramp_targets.items():
            p = self.host.pid(pid_id)
            if p is None or p.state not in (AutoState.ACTIVE, AutoState.PAUSED):
                return self._fail(core, f'ramp: {pid_id} is not running')
            sp = p.loops[k].settings.setpoint
            if abs(target - sp) <= 1e-12:
                continue
            new = sp + max(-step, min(step, target - sp))
            ok, msg = self.host.set_setpoint(pid_id, k, new)
            if not ok:
                return self._fail(core, f'ramp: {msg}')
            done = done and abs(target - new) <= 1e-12
        if done:
            th.phase = 'wait'
            th.t_wait = th.t
            if not node.until:
                self._go(core, th, 'next', 'setpoints reached')

    # ------------------------------------------------------------------ conditions
    def _instant(self, core: 'LaserCore', th: _Thread, c: Cond) -> bool:
        if c.kind == 'locked':
            for pid_id, tol in c.pids().items():
                p = self.host.pid(pid_id)
                if p is None or p.state != AutoState.ACTIVE:
                    return False
                for loop in p.loops:
                    if not loop.trace:
                        return False
                    last = loop.trace[-1]
                    t = tol if tol > 0 else abs(loop.settings.deadband)
                    if last.t < th.wall_step or not is_finite(last.error) or abs(last.error) > t:
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

    def _satisfied(self, core: 'LaserCore', th: _Thread, node: Node, k: int, c: Cond) -> bool:
        if c.kind == 'delay':
            return th.t - th.t_wait >= c.value
        if c.kind == 'count':
            return self.visits.get(node.uid, 0) >= c.value
        if c.kind == 'travel':
            p0, p = th.pos0.get(c.target), core.position(c.target)
            return p0 is not None and p is not None and abs(p - p0) >= c.value
        if c.kind == 'finished':
            a = core.get(c.target)
            # finished by itself, and started during this step (not an old pass)
            return (a is not None and a.state == AutoState.OFF and a.reason == 'finished'
                    and a.started_at is not None and a.started_at >= th.core_enter - 1e-9)
        if self._instant(core, th, c):
            if th.since.get(k) is None:
                th.since[k] = th.t
            return th.t - th.since[k] >= c.hold_s
        th.since[k] = None
        return False

    def _tick_wait(self, core: 'LaserCore', th: _Thread, node: Node, now: float):
        if node.type == 'step' and node.action == 'hold':
            self.status = 'holding (until stopped)'
            return
        for k, c in enumerate(node.until):
            if self._satisfied(core, th, node, k, c):
                return self._go(core, th, c.outcome, describe_cond(c).split(' → ')[0])
        if not node.until:
            return self._go(core, th, 'next', 'done')
        if node.timeout_s > 0 and th.t - th.t_wait >= node.timeout_s:
            return self._go(core, th, node.on_timeout, f'timeout ({node.timeout_s:g} s)')

    def variables(self) -> Dict[str, float]:
        return {'interruptions': float(self.interruptions),
                'main_time': float(self.main.t) if self.main else 0.0}
