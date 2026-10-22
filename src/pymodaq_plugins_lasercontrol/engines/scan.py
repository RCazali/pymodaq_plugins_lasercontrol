"""Scan on the broker (T4.4): 1D scan with acquisition.

It acquires the scanned motor (a human action or another automation stops/pauses it like
any automation), moves with bounded relative chunks (S3) and stays inside the declared scan range
(explicit S4 envelope, plus the role's abs limits).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import numpy as np

from ..core.automation import Automation
from ..core.events import EventKind, Level
from ..core.safety import SafetyPolicy
from ..core.signals import is_finite
from .sequencer import MoveSequencer

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore


def scan_positions(start: float, stop: float, step: float) -> List[float]:
    """Same grid as the historical ScanEngine (start included, stop included when reached)."""
    step = abs(float(step))
    if step == 0:
        raise ValueError('the scan step cannot be zero')
    n = int(abs(stop - start) / step + 1e-9) + 1
    direction = 1.0 if stop >= start else -1.0
    return [start + direction * i * step for i in range(n)]


class Scan1DAutomation(Automation):
    kind = 'scan'

    def __init__(self, role: str, start: float, stop: float, step: float, dwell: float = 0.1,
                 samples: int = 1, channels: Optional[List[str]] = None, relative: bool = True,
                 return_to_start: bool = True, check_intensity: bool = False,
                 auto_id: str = 'SCAN', label: str = '1D Scan'):
        super().__init__(auto_id, label)
        self.period_s = 0.05
        self.role = role
        self.start_v, self.stop_v, self.step_v = float(start), float(stop), float(step)
        self.dwell = max(float(dwell), 0.0)
        self.samples = max(int(samples), 1)
        self.channels = list(channels or [])
        self.relative = bool(relative)
        self.return_to_start = bool(return_to_start)
        self._check_intensity = bool(check_intensity)
        self.points: List[float] = []
        self.index = 0
        self.phase = 'idle'
        self.origin: Optional[float] = None
        self.results: List[Tuple[float, Dict[str, float]]] = []   # (position read, {role: value})
        self.sequencer: Optional[MoveSequencer] = None
        self._buf: Dict[str, List[float]] = {}
        self._seq: Dict[str, int] = {}
        self._t_min = 0.0
        self._dwell_end = 0.0
        self.finished = False
        self.completed = False

    # ------------------------------------------------------------------ description
    def motor_roles(self) -> List[str]:
        return [self.role]

    def signal_roles(self) -> List[str]:
        return list(self.channels)

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy(check_data=bool(self.channels), check_intensity=self._check_intensity,
                            check_rate=False, envelope='explicit')

    def absolute_points(self, origin: float) -> List[float]:
        pts = scan_positions(self.start_v, self.stop_v, self.step_v)
        return [origin + p for p in pts] if self.relative else pts

    def explicit_ranges(self, core: 'LaserCore') -> Optional[Dict[str, Tuple[float, float]]]:
        module = core.roles.module_for(self.role)
        pos = core.position(self.role)
        if module is None or pos is None:
            return None
        pts = self.absolute_points(pos) + [pos]
        return {module: (min(pts), max(pts))}

    def summary(self) -> str:
        ref = 'relative to the current position' if self.relative else 'absolute'
        return (f"{self.label}: {self.role} from {self.start_v:g} to {self.stop_v:g} step {self.step_v:g} ({ref}), "
                f"{len(scan_positions(self.start_v, self.stop_v, self.step_v))} points")

    def validate(self, core: 'LaserCore') -> List[str]:
        try:
            n = len(scan_positions(self.start_v, self.stop_v, self.step_v))
        except ValueError as e:
            return [str(e)]
        return [] if n > 0 else ['empty scan']

    # ------------------------------------------------------------------ lifecycle
    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.origin = core.position(self.role)
        self.points = self.absolute_points(self.origin)
        self.index = 0
        self.results = []
        self.finished = False
        self.completed = False
        self.sequencer = MoveSequencer(core, self)
        self.phase = 'goto'
        self._goto_current()
        return None

    def on_resume(self, core: 'LaserCore'):
        if self.phase in ('dwell', 'sampling'):
            self.phase = 'dwell'
            self._dwell_end = core.clock.now() + self.dwell

    def _goto_current(self):
        self.sequencer.set_targets({self.role: self.points[self.index]})

    def is_done(self) -> bool:
        return self.finished

    def tick(self, core: 'LaserCore', now: float):
        if self.phase == 'goto':
            st = self.sequencer.step()
            if st == MoveSequencer.DONE:
                self.phase = 'dwell'
                self._dwell_end = now + self.dwell
            return
        if self.phase == 'dwell':
            if now < self._dwell_end:
                return
            if not self.channels:
                self._record(core, {})
                return
            self.phase = 'sampling'
            self._t_min = self._dwell_end
            self._buf = {r: [] for r in self.channels}
            self._seq = {}
            for r in self.channels:
                s = core.signals.latest(core.roles.channel_for(r))
                self._seq[r] = s.seq if s is not None else 0
            return
        if self.phase == 'sampling':
            for r, buf in self._buf.items():
                if len(buf) >= self.samples:
                    continue
                s = core.signals.latest(core.roles.channel_for(r))
                if s is not None and s.seq > self._seq[r] and s.t_acq >= self._t_min:
                    buf.append(s.value if is_finite(s.value) else math.nan)
                    self._seq[r] = s.seq
            if all(len(b) >= self.samples for b in self._buf.values()):
                self._record(core, {r: float(np.nanmean(b)) if np.any(np.isfinite(b)) else math.nan
                                    for r, b in self._buf.items()})
            return
        if self.phase == 'return':
            st = self.sequencer.step()
            if st == MoveSequencer.DONE:
                self.finished = True
            return

    def _record(self, core: 'LaserCore', readings: Dict[str, float]):
        pos = core.position(self.role)
        self.results.append((pos if pos is not None else self.points[self.index], readings))
        self.index += 1
        if self.index >= len(self.points):
            self.completed = True
            core.events.publish(EventKind.SCAN, self.id, f"{self.label} finished ({len(self.results)} points)")
            if self.return_to_start and self.origin is not None:
                self.phase = 'return'
                self.sequencer.set_targets({self.role: self.origin})
            else:
                self.finished = True
            return
        self.phase = 'goto'
        self._goto_current()

    def progress(self) -> Tuple[int, int]:
        return min(self.index, len(self.points)), len(self.points)

    def variables(self) -> Dict[str, float]:
        done, total = self.progress()
        return {'index': float(done), 'total': float(total)}
