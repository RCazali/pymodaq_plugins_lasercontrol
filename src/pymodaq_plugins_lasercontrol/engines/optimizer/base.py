"""Pluggable optimiser interface (specification T4.5).

An optimiser algorithm never moves anything and never reads anything by itself. It *proposes*
requests and *observes* results::

    algo.start(ctx)
    while (req := algo.propose()) is not None:
        ... driver executes req (move / measure) ...
        algo.observe(result)       # cost for a Measure, None otherwise

Offsets are relative to the motor positions at the start of the pass. The driver
(:class:`OptimizerAutomation`) turns them into bounded, checked relative moves (S3-S5).

Historical algorithms (Joint/SPSA, Sequential, Nelder-Mead, Gradient/Newton-lite) are ported as
generators and wrapped by :class:`GeneratorAlgorithm`; a future algorithm (L10: extremum seeking,
Bayesian...) only has to implement ``start/propose/observe`` and be registered with
:func:`register_algorithm`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple


# --------------------------------------------------------------------------- requests
@dataclass
class Measure:
    """Move to ``offsets`` (from the pass start), wait the dwell time, average ``samples`` fresh
    frames of every objective signal and return the cost."""
    offsets: Dict[str, float]
    samples: int = 2


@dataclass
class MoveTo:
    """Move to ``offsets`` without measuring."""
    offsets: Dict[str, float]


@dataclass
class Report:
    """One iteration done: cost to plot in the convergence curve."""
    cost: float


@dataclass
class Status:
    message: str


Request = Any  # Measure | MoveTo | Report | Status


# --------------------------------------------------------------------------- context
@dataclass
class OptContext:
    motors: List[dict]                 # {'role', 'max_step', 'probe_step', 'order'}
    config: dict
    deadband: float = 0.1
    rng: random.Random = field(default_factory=random.Random)

    NOISE_SAMPLES = 5
    PROBE_SAMPLES = 2
    NEWTON_STEP_SANITY_MULT = 50.0

    @property
    def names(self) -> List[str]:
        return [m['role'] for m in self.motors]

    def full(self) -> bool:
        return bool(self.config.get('full_optimization', False))

    def budget(self) -> Tuple[int, int]:
        """(max_iterations, stall_patience), larger and more patient in Full Optimization."""
        if self.full():
            return (int(self.config.get('full_opt_iterations', 30)),
                    max(int(self.config.get('full_opt_stall_patience', 10)), 1))
        return (int(self.config.get('max_iterations', 6)),
                max(int(self.config.get('stall_patience', 3)), 1))

    def probe_step(self, m: dict) -> float:
        """Full Optimization probes at the full max_step, otherwise min(probe_step, max_step)."""
        if self.full():
            return float(m['max_step'])
        return min(float(m['probe_step']), float(m['max_step']))

    def clamp(self, offsets: Dict[str, float]) -> Dict[str, float]:
        """Hard cap of every offset to +/- max_step of its motor (historical ``_goto_offset``)."""
        out = {}
        for m in self.motors:
            n = m['role']
            v = float(offsets.get(n, 0.0))
            out[n] = max(-float(m['max_step']), min(float(m['max_step']), v))
        return out

    @property
    def probe_samples(self) -> int:
        return max(int(self.config.get('probe_samples', self.PROBE_SAMPLES)), 1)

    @property
    def safety_factor(self) -> float:
        return max(float(self.config.get('safety_factor', 5.0)), 0.1)

    @property
    def max_backoffs(self) -> int:
        return max(int(self.config.get('max_backoffs', 3)), 1)


# --------------------------------------------------------------------------- interface
class OptimizerAlgorithm:
    name = 'base'
    label = 'base'

    def start(self, ctx: OptContext) -> None:
        raise NotImplementedError

    def propose(self) -> Optional[Request]:
        raise NotImplementedError

    def observe(self, result) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


GenFunc = Callable[[OptContext], Generator[Request, Any, Any]]


class GeneratorAlgorithm(OptimizerAlgorithm):
    """Adapter: a generator function becomes an ask/tell optimiser."""

    def __init__(self, name: str, label: str, func: GenFunc):
        self.name = name
        self.label = label
        self._func = func
        self._gen = None
        self._pending = None
        self._started = False
        self.finished = False

    def start(self, ctx: OptContext) -> None:
        self._gen = self._func(ctx)
        self._pending = None
        self._started = False
        self.finished = False

    def propose(self) -> Optional[Request]:
        if self._gen is None or self.finished:
            return None
        try:
            if not self._started:
                self._started = True
                req = next(self._gen)
            else:
                req = self._gen.send(self._pending)
        except StopIteration:
            self.finished = True
            return None
        self._pending = None
        return req

    def observe(self, result) -> None:
        self._pending = result

    def close(self) -> None:
        if self._gen is not None:
            self._gen.close()
        self.finished = True


_REGISTRY: Dict[str, Tuple[str, Callable[[], OptimizerAlgorithm]]] = {}


def register_algorithm(name: str, label: str, factory: Callable[[], OptimizerAlgorithm]):
    _REGISTRY[name] = (label, factory)


def algorithm_names() -> List[Tuple[str, str]]:
    return [(n, lab) for n, (lab, _) in _REGISTRY.items()]


def create_algorithm(run_mode: str) -> OptimizerAlgorithm:
    """Accepts a registry name or the historical UI labels ('Joint (all motors at once)'...)."""
    key = str(run_mode or '').strip().lower()
    if key in _REGISTRY:
        return _REGISTRY[key][1]()
    for name in _REGISTRY:
        if key.startswith(name.split('-')[0]):
            return _REGISTRY[name][1]()
    return _REGISTRY['joint'][1]()
