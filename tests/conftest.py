import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

SRC = Path(__file__).resolve().parents[1] / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pymodaq_plugins_lasercontrol.core import (FakeClock, LaserCore, RoleMap, SafetyConfig, ActuatorLimits,  # noqa
                                               IntensityGuardConfig)
from pymodaq_plugins_lasercontrol.core.fakes import FakeActuator, FakeDetector  # noqa


class Rig:
    """A core wired to fake motors and a fake detector, driven by a fake clock."""

    def __init__(self, guard=True, max_step=1.0, max_excursion=10.0, rate_max=100.0, max_age=1.0):
        self.clock = FakeClock()
        roles = RoleMap(machine='test')
        roles.set_actuator('M1', 'mod1', units='um')
        roles.set_actuator('M2', 'mod2', units='um')
        roles.set_actuator('M3', 'mod3', units='um')
        roles.set_signal('S', 'det/data/s')
        roles.set_signal('S2', 'det/data/s2')
        roles.set_signal('I', 'det/data/i')
        roles.heartbeats = {}
        limits = {r: ActuatorLimits(max_step=max_step, max_excursion=max_excursion, rate_max=rate_max)
                  for r in ('M1', 'M2', 'M3')}
        cfg = SafetyConfig(max_data_age_s=max_age, move_timeout_s=5.0,
                           intensity=IntensityGuardConfig(enabled=guard, role='I', mode='relative',
                                                          threshold=0.5, hysteresis=0.1),
                           actuators=limits)
        self.core = LaserCore(self.clock, roles, cfg)
        self.log = []
        self.acts = {n: FakeActuator(n, 0.0, log=self.log) for n in ('mod1', 'mod2', 'mod3', 'unmapped')}
        self.det = FakeDetector('det')
        self.core.broker.attach(self.acts, {'det': self.det})
        self.s = 0.0
        self.s2 = 0.0
        self.i = 1.0
        self.hb = 0

    def frame(self, s=None, i=None, s2=None, **extra):
        if s is not None:
            self.s = s
        if i is not None:
            self.i = i
        if s2 is not None:
            self.s2 = s2
        self.hb += 1
        values = {'det/data/s': self.s, 'det/data/s2': self.s2, 'det/data/i': self.i,
                  'det/stats/hb': self.hb}
        values.update(extra)
        self.det.emit(values)

    def step(self, dt=0.1, frame=True, finish=True, **kw):
        """Advance time, deliver a frame, complete running moves, tick the core."""
        self.clock.advance(dt)
        if frame:
            self.frame(**kw)
        if finish:
            for a in self.acts.values():
                if a.moving:
                    a.finish()
        self.core.tick()

    def run(self, seconds, dt=0.1, **kw):
        n = int(round(seconds / dt))
        for _ in range(n):
            self.step(dt, **kw)

    def moves(self, kind=('move_rel', 'move_abs')):
        return [e for e in self.log if e[0] in kind]


@pytest.fixture
def rig():
    r = Rig()
    r.frame()
    return r
