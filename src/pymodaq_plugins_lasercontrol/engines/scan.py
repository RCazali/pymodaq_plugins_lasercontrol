"""Scans on the broker (T4.4): 1D scan with acquisition, XY snake positioning scan.

Both acquire the scanned motors (a human action or another automation stops/pauses them like
any automation), move with bounded relative chunks (S3) and stay inside the declared scan range
(explicit S4 envelope, plus the roles' abs limits).
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
        raise ValueError('le pas du scan ne peut pas être nul')
    n = int(abs(stop - start) / step + 1e-9) + 1
    direction = 1.0 if stop >= start else -1.0
    return [start + direction * i * step for i in range(n)]


def snake_pattern(xmin, xmax, dx, ymin, ymax, dy) -> List[Tuple[float, float]]:
    """Boustrophedon pattern of the historical XY hardware scan."""
    if dx <= 0 or dy <= 0 or xmin >= xmax or ymin >= ymax:
        return []
    xs = np.arange(xmin, xmax + dx / 2, dx)
    ys = np.arange(ymin, ymax + dy / 2, dy)
    pattern = []
    for i, y in enumerate(ys):
        line = xs[::-1] if i % 2 == 1 else xs
        pattern.extend((float(x), float(y)) for x in line)
    return pattern


class Scan1DAutomation(Automation):
    kind = 'scan'

    def __init__(self, role: str, start: float, stop: float, step: float, dwell: float = 0.1,
                 samples: int = 1, channels: Optional[List[str]] = None, relative: bool = True,
                 return_to_start: bool = True, check_intensity: bool = False,
                 auto_id: str = 'SCAN', label: str = 'Scan 1D'):
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
        ref = 'relatif à la position actuelle' if self.relative else 'absolu'
        return (f"{self.label} : {self.role} de {self.start_v:g} à {self.stop_v:g} pas {self.step_v:g} ({ref}), "
                f"{len(scan_positions(self.start_v, self.stop_v, self.step_v))} points")

    def validate(self, core: 'LaserCore') -> List[str]:
        try:
            n = len(scan_positions(self.start_v, self.stop_v, self.step_v))
        except ValueError as e:
            return [str(e)]
        return [] if n > 0 else ['scan vide']

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
                if s is not None and s.seq > self._seq[r] and s.t >= self._t_min:
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
            core.events.publish(EventKind.SCAN, self.id, f"{self.label} terminé ({len(self.results)} points)")
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


class XYScanAutomation(Automation):
    """Event driven snake scan of an XY pair (historical 'hardware scan' of the XY tabs)."""
    kind = 'xyscan'

    def __init__(self, group_id: str, x_role: str, y_role: str, xmin: float, xmax: float, dx: float,
                 ymin: float, ymax: float, dy: float, dwell: float = 0.0, label: str = ''):
        super().__init__(f'XYSCAN:{group_id}', label or f'Scan XY {group_id}')
        self.period_s = 0.05
        self.x_role, self.y_role = x_role, y_role
        self.bounds = dict(xmin=float(xmin), xmax=float(xmax), dx=float(dx),
                           ymin=float(ymin), ymax=float(ymax), dy=float(dy))
        self.dwell = max(float(dwell), 0.0)
        self.pattern = snake_pattern(xmin, xmax, dx, ymin, ymax, dy)
        self.index = 0
        self.phase = 'idle'
        self.sequencer: Optional[MoveSequencer] = None
        self._dwell_end = 0.0
        self.finished = False
        self.completed = False

    def motor_roles(self) -> List[str]:
        return [self.x_role, self.y_role]

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy(check_data=False, check_intensity=False, check_rate=False, envelope='explicit')

    def explicit_ranges(self, core: 'LaserCore') -> Optional[Dict[str, Tuple[float, float]]]:
        out = {}
        for role, lo, hi in ((self.x_role, self.bounds['xmin'], self.bounds['xmax']),
                             (self.y_role, self.bounds['ymin'], self.bounds['ymax'])):
            module = core.roles.module_for(role)
            pos = core.position(role)
            if module is None or pos is None:
                return None
            out[module] = (min(lo, pos), max(hi, pos))
        return out

    def summary(self) -> str:
        b = self.bounds
        return (f"{self.label} : X={self.x_role} [{b['xmin']:g}, {b['xmax']:g}] pas {b['dx']:g}, "
                f"Y={self.y_role} [{b['ymin']:g}, {b['ymax']:g}] pas {b['dy']:g} ({len(self.pattern)} points)")

    def validate(self, core: 'LaserCore') -> List[str]:
        errors = []
        if not self.pattern:
            errors.append('paramètres de scan invalides (min < max, pas > 0)')
        if self.x_role == self.y_role:
            errors.append('X et Y doivent être deux actionneurs différents')
        return errors

    def on_start(self, core: 'LaserCore') -> Optional[str]:
        self.index = 0
        self.finished = False
        self.completed = False
        self.sequencer = MoveSequencer(core, self)
        self.phase = 'goto'
        x, y = self.pattern[0]
        self.sequencer.set_targets({self.x_role: x, self.y_role: y})
        return None

    def on_resume(self, core: 'LaserCore'):
        pass

    def is_done(self) -> bool:
        return self.finished

    def tick(self, core: 'LaserCore', now: float):
        if self.phase == 'goto':
            if self.sequencer.step() == MoveSequencer.DONE:
                self.phase = 'dwell'
                self._dwell_end = now + self.dwell
            return
        if self.phase == 'dwell' and now >= self._dwell_end:
            self.index += 1
            if self.index >= len(self.pattern):
                self.completed = True
                self.finished = True
                core.events.publish(EventKind.SCAN, self.id, f"{self.label} terminé ({len(self.pattern)} points)")
                return
            x, y = self.pattern[self.index]
            self.sequencer.set_targets({self.x_role: x, self.y_role: y})
            self.phase = 'goto'

    def progress(self) -> Tuple[int, int]:
        return self.index, len(self.pattern)

    def variables(self) -> Dict[str, float]:
        return {'index': float(self.index), 'total': float(len(self.pattern))}
