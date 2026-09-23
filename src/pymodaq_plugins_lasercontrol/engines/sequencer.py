"""Bounded, non-stacking motion towards absolute targets (used by the optimiser and the scans).

Automations may only send bounded relative steps (S3). To reach a target, the sequencer sends
one chunk (<= role ``max_step``) per motor, waits for every ``move_done``, then sends the next
chunk. Nothing is queued: if the extension dies, at most one short step is in progress (S10).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, Optional

from ..core.states import AutoState

if TYPE_CHECKING:  # pragma: no cover
    from ..core.automation import Automation
    from ..core.coordinator import LaserCore


class MoveSequencer:
    MOVING = 'moving'
    DONE = 'done'
    BLOCKED = 'blocked'

    def __init__(self, core: 'LaserCore', automation: 'Automation'):
        self.core = core
        self.automation = automation
        self.targets: Dict[str, float] = {}
        self.chunks = 0
        self.max_chunks = 0
        self.gave_up = False

    def _cap(self, role: str) -> Optional[float]:
        lim = self.core.safety.limits(role)
        return lim.max_step if lim is not None and lim.max_step else None

    def _tol(self, role: str, target: float) -> float:
        lim = self.core.safety.limits(role)
        res = lim.resolution if lim is not None else 0.0
        return max(0.5 * res, 1e-9 * max(1.0, abs(target)))

    def set_targets(self, targets: Dict[str, float]):
        self.targets = {r: float(v) for r, v in targets.items()}
        self.chunks = 0
        self.gave_up = False
        total = 0
        for role, target in self.targets.items():
            pos = self.core.position(role)
            cap = self._cap(role)
            if pos is None or not cap:
                continue
            total = max(total, int(math.ceil(abs(target - pos) / cap)))
        self.max_chunks = total + 3     # a few extra chunks for closed-loop residuals / rounding

    def remaining(self) -> Dict[str, float]:
        out = {}
        for role, target in self.targets.items():
            pos = self.core.position(role)
            if pos is not None:
                out[role] = target - pos
        return out

    def step(self) -> str:
        if self.automation.state != AutoState.ACTIVE:
            return self.BLOCKED
        roles = list(self.targets)
        if self.core.any_busy(roles):
            return self.MOVING
        deltas = {}
        for role, target in self.targets.items():
            pos = self.core.position(role)
            if pos is None:
                return self.BLOCKED
            rem = target - pos
            if abs(rem) <= self._tol(role, target):
                continue
            cap = self._cap(role)
            deltas[role] = max(-cap, min(cap, rem)) if cap else rem
        if not deltas:
            return self.DONE
        if self.chunks >= self.max_chunks:
            self.gave_up = True        # residual smaller than what the motor can resolve: accept it
            return self.DONE
        self.chunks += 1
        sent = self.core.request_moves(self.automation, deltas)
        if self.automation.state != AutoState.ACTIVE:
            return self.BLOCKED
        if not sent:
            return self.DONE           # only sub-resolution moves were left
        return self.MOVING
