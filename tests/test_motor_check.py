"""tools/motor_check: +1 / -1 per motor, one command per second, lock at the first motor that does not answer."""
import math
import time

import pytest
from qtpy import QtCore

from pymodaq_gui.parameter import Parameter

import pymodaq_plugins_lasercontrol.tools.motor_check as mc


class FakeMove(QtCore.QObject):
    """The part of DAQ_Move used by motor_check; ``answer`` False = a motor that never says "move done"."""
    move_done_signal = QtCore.Signal(float)
    current_value_signal = QtCore.Signal(float)

    def __init__(self, title, position=0.0, answer=True, drift=0.0, units='mm', bounds=None):
        super().__init__()
        self.title = title
        self.units = units
        self.initialized_state = True
        self.current_value = position
        self.answer = answer
        self.drift = drift
        self.commands = []
        children = [{'name': 'epsilon', 'type': 'float', 'value': 0.001},
                    {'name': 'timeout', 'type': 'int', 'value': 1}]
        if bounds is not None:
            children.append({'name': 'bounds', 'type': 'group', 'children': [
                {'name': 'is_bounds', 'type': 'bool', 'value': True},
                {'name': 'min_bound', 'type': 'float', 'value': bounds[0]},
                {'name': 'max_bound', 'type': 'float', 'value': bounds[1]}]})
        self.settings = Parameter.create(name='s', type='group', children=[
            {'name': 'move_settings', 'type': 'group', 'children': children}])

    def move_rel(self, delta):
        self.commands.append(('move_rel', delta, time.monotonic()))
        if self.answer:
            self.current_value += delta + self.drift
            QtCore.QTimer.singleShot(50, lambda: self.move_done_signal.emit(self.current_value))

    def get_actuator_value(self):
        self.commands.append(('get', None, time.monotonic()))

    def stop_motion(self):
        self.commands.append(('stop', None, time.monotonic()))


@pytest.fixture
def lock(tmp_path, monkeypatch):
    path = tmp_path / 'MOTOR_LOCK.txt'
    monkeypatch.setattr(mc, 'LOCK_FILE', path)
    monkeypatch.setattr(mc, 'MIN_CALL_INTERVAL_S', 0.2)      # faster tests; the spacing itself is checked
    mc._app()
    return path


def test_each_motor_goes_plus_one_and_back_with_spaced_commands(lock):
    mods = [FakeMove('A', 3.0), FakeMove('B', -2.5)]
    res = mc.check_motors(mods, log=lambda s: None)
    assert [r.status for r in res] == ['ok', 'ok']
    assert [m.current_value for m in mods] == [3.0, -2.5]
    assert [(c[0], c[1]) for c in mods[0].commands] == [('move_rel', 1.0), ('move_rel', -1.0)]
    times = sorted(c[2] for m in mods for c in m.commands)
    assert all(b - a >= 0.2 - 1e-3 for a, b in zip(times, times[1:]))
    assert not lock.exists()


def test_a_motor_that_does_not_answer_locks_every_motor(lock):
    mods = [FakeMove('A', 0.0), FakeMove('dead', 0.0, answer=False), FakeMove('C', 0.0)]
    res = mc.check_motors(mods, log=lambda s: None, timeout_s=0.5)
    assert [r.status for r in res] == ['ok', 'FAILED']             # C is never reached
    assert mods[2].commands == []
    assert [c[0] for c in mods[1].commands] == ['move_rel', 'stop']
    assert lock.exists() and 'dead' in lock.read_text()
    again = mc.check_motors([FakeMove('C', 0.0)], log=lambda s: None)
    assert again == []                                             # refused while the lock file exists


def test_a_motor_that_does_not_come_back_locks(lock):
    res = mc.check_motors([FakeMove('slip', 0.0, drift=0.2), FakeMove('B')], log=lambda s: None)
    assert res[0].status == 'FAILED' and 'instead of' in res[0].reason and len(res) == 1
    assert lock.exists()


def test_skips_and_dry_run_never_move(lock):
    out_of_bounds = FakeMove('edge', 9.5, bounds=(0.0, 10.0))
    unreadable = FakeMove('nan', math.nan)
    off = FakeMove('off')
    off.initialized_state = False
    res = mc.check_motors([out_of_bounds, unreadable, off], log=lambda s: None)
    assert [r.status for r in res] == ['skipped', 'skipped', 'skipped']
    assert all(c[0] != 'move_rel' for m in (out_of_bounds, unreadable, off) for c in m.commands)
    dry = FakeMove('dry', 1.0)
    assert mc.check_motors([dry], dry_run=True, log=lambda s: None)[0].status == 'dry-run'
    assert dry.commands == [] and not lock.exists()
