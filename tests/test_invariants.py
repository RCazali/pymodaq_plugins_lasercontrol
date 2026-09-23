"""One scenario per safety invariant S1..S12 + arbitration rules D4/D5/O3 (cahier des charges T12.1, T12.3).

Everything runs on the pure core with fake ports and a fake clock (no Qt, no hardware).
"""
import math

import pytest

from pymodaq_plugins_lasercontrol.core import AutoState, GlobalState, Level
from pymodaq_plugins_lasercontrol.core.automation import Automation
from pymodaq_plugins_lasercontrol.engines import PIDAutomation, PIDSettings, OptimizerAutomation
from pymodaq_plugins_lasercontrol.hardware.channels import extract_scalars

from conftest import Rig


def make_pid(rig, pid_id='A', signal='S', motors=None, **settings):
    s = PIDSettings(setpoint=0.0, kp=0.5, ki=0.0, kd=0.0, max_step=1.0, period_s=0.5)
    for k, v in settings.items():
        setattr(s, k, v)
    pid = PIDAutomation(pid_id, signal, motors or {'M1': 1.0}, s)
    rig.core.add_automation(pid)
    return pid


# --------------------------------------------------------------------------- S9
def test_S9_nothing_starts_by_itself(rig):
    make_pid(rig)
    rig.run(3.0, s=5.0)
    assert rig.moves() == []
    assert rig.core.get('PID:A').state == AutoState.OFF
    assert rig.core.global_state == GlobalState.IDLE


# --------------------------------------------------------------------------- basic PID loop
def test_pid_moves_toward_setpoint(rig):
    pid = make_pid(rig)
    rig.frame(s=2.0)
    ok, msg = rig.core.start(pid.id)
    assert ok, msg
    rig.step(0.1, s=2.0)
    moves = rig.moves()
    assert moves and moves[0][0] == 'move_rel' and moves[0][1] == 'mod1'
    # error = 0 - 2 = -2 ; kp = 0.5 -> -1 ; clamp 1 -> -1
    assert moves[0][2] == pytest.approx(-1.0)


# --------------------------------------------------------------------------- S1
def test_S1_stale_data_freezes(rig):
    pid = make_pid(rig)
    rig.frame(s=2.0)
    assert rig.core.start(pid.id)[0]
    rig.step(0.1, s=2.0)
    n = len(rig.moves())
    rig.run(2.0, frame=False)                  # computer B silent for 2 s (> max age 1 s)
    assert pid.state == AutoState.HELD
    assert 'périmé' in pid.reason
    assert len(rig.moves()) == n
    rig.run(1.0, s=3.0)                        # data come back: stays frozen (O10-A manual resume)
    assert pid.state == AutoState.HELD
    assert len(rig.moves()) == n
    ok, _ = rig.core.resume(pid.id)
    assert ok and pid.state == AutoState.ACTIVE


def test_S1_frozen_heartbeat_is_stale(rig):
    rig.core.roles.heartbeats = {'det': 'det/stats/hb'}
    rig.core.signals.set_heartbeats(rig.core.roles.heartbeats)
    pid = make_pid(rig)
    rig.frame(s=1.0)
    assert rig.core.start(pid.id)[0]
    rig.step(0.1, s=1.0)
    # frames keep arriving, but the frame counter of B is frozen
    for _ in range(20):
        rig.clock.advance(0.1)
        rig.det.emit({'det/data/s': 1.0, 'det/data/s2': 0.0, 'det/data/i': 1.0, 'det/stats/hb': 42})
        rig.core.tick()
    assert pid.state == AutoState.HELD
    assert 'compteur' in pid.reason


# --------------------------------------------------------------------------- S2
def test_S2_low_intensity_freezes_with_hysteresis(rig):
    pid = make_pid(rig)
    rig.frame(s=2.0, i=1.0)
    assert rig.core.start(pid.id)[0]          # reference captured = 1.0
    assert rig.core.safety.guard.reference == pytest.approx(1.0)
    rig.step(0.1, s=2.0)
    n = len(rig.moves())
    rig.run(0.6, i=0.3)                        # below 0.5 * ref
    assert pid.state == AutoState.HELD and 'intensité' in pid.reason
    assert len(rig.moves()) == n
    rig.run(0.5, i=0.55)                       # above the threshold but below threshold + hysteresis
    ok, why = rig.core.resume(pid.id)
    assert not ok and 'seuil' in why
    rig.run(0.5, i=0.7)
    ok, why = rig.core.resume(pid.id)
    assert ok, why


def test_S2_guard_must_be_configured():
    rig = Rig()
    rig.core.safety.config.intensity.role = None
    rig.frame(s=1.0)
    pid = make_pid(rig)
    ok, msg = rig.core.start(pid.id)
    assert not ok and 'garde intensité' in msg


def test_S2_guard_can_be_explicitly_disabled():
    rig = Rig(guard=False)
    rig.frame(s=1.0, i=0.0)
    pid = make_pid(rig)
    ok, msg = rig.core.start(pid.id)
    assert ok, msg


# --------------------------------------------------------------------------- S3
def test_S3_only_bounded_relative_steps(rig):
    pid = make_pid(rig, kp=100.0, max_step=50.0)     # PID asks for huge steps
    rig.frame(s=10.0)
    assert rig.core.start(pid.id)[0]
    rig.run(2.0, s=10.0)
    moves = rig.moves()
    assert moves
    assert all(m[0] == 'move_rel' for m in moves)              # never move_abs
    assert all(abs(m[2]) <= 1.0 + 1e-12 for m in moves)         # role max_step = 1


# --------------------------------------------------------------------------- S4
def test_S4_envelope_clamps_and_freezes():
    rig = Rig(max_step=1.0, max_excursion=2.5, rate_max=1000)
    rig.frame(s=10.0)
    pid = make_pid(rig, kp=10.0, max_step=1.0)
    assert rig.core.start(pid.id)[0]
    rig.run(5.0, s=10.0)                       # error never decreases (signal does not follow)
    assert pid.state == AutoState.HELD
    assert 'enveloppe' in pid.reason
    pos = rig.acts['mod1'].position()
    assert pos == pytest.approx(-2.5)          # clamped exactly at ref - max_excursion
    n = len(rig.moves())
    rig.run(2.0, s=10.0)
    assert len(rig.moves()) == n


def test_S4_absolute_limits():
    rig = Rig(max_step=1.0, max_excursion=100, rate_max=1000)
    rig.core.safety.config.actuators['M1'].abs_min = -1.5
    rig.frame(s=10.0)
    pid = make_pid(rig, kp=10.0)
    assert rig.core.start(pid.id)[0]
    rig.run(5.0, s=10.0)
    assert pid.state == AutoState.HELD
    assert rig.acts['mod1'].position() == pytest.approx(-1.5)


# --------------------------------------------------------------------------- S5
def test_S5_rate_limit_freezes():
    rig = Rig(max_step=1.0, max_excursion=1000, rate_max=3.5)
    rig.frame(s=10.0)
    pid = make_pid(rig, kp=10.0)
    assert rig.core.start(pid.id)[0]
    rig.run(10.0, s=10.0)
    assert pid.state == AutoState.HELD
    assert 'débit' in pid.reason
    travelled = sum(abs(m[2]) for m in rig.moves())
    assert travelled <= 3.5 + 1e-9


# --------------------------------------------------------------------------- S6 / D4 / O3-A
def test_S6_single_owner_and_O3_pause_resume(rig):
    a = make_pid(rig, 'A', motors={'M1': 1.0})
    b = make_pid(rig, 'B', motors={'M1': 1.0, 'M2': 1.0})
    rig.frame(s=1.0)
    assert rig.core.start(a.id)[0]
    assert rig.core.ownership.owner_of('mod1') == a.id
    a.controller.integral = 3.0
    assert rig.core.start(b.id)[0]             # D4: last activated takes the shared motor
    assert rig.core.ownership.owner_of('mod1') == b.id
    assert a.state == AutoState.PAUSED and a.pause_kind == 'preempted'
    rig.run(1.0, s=1.0)
    assert all(e[1] in ('mod1', 'mod2') for e in rig.moves())
    rig.core.stop(b.id)                        # B releases -> A resumes by itself
    assert a.state == AutoState.ACTIVE
    assert a.controller.integral == 0.0        # integral reset at resume
    assert rig.core.ownership.owner_of('mod1') == a.id


def test_S6_non_owner_cannot_move(rig):
    a = make_pid(rig, 'A')
    rig.frame(s=1.0)
    assert rig.core.start(a.id)[0]
    rig.core.ownership.release(a.id)           # simulate a lost ownership
    sent = rig.core.request_moves(a, {'M1': 0.5})
    assert sent == {}
    assert a.state == AutoState.HELD
    assert not any(e[0] == 'move_rel' for e in rig.log)


# --------------------------------------------------------------------------- S7 / D5
def test_S7_human_action_stops_automation_before_order(rig):
    a = make_pid(rig, 'A', motors={'M1': 1.0, 'M2': 1.0})
    b = make_pid(rig, 'B', motors={'M1': 1.0})
    rig.frame(s=1.0)
    assert rig.core.start(a.id)[0]
    assert rig.core.start(b.id)[0]             # A paused (waiting for M1)
    events = []
    rig.core.events.subscribe(lambda e: events.append(e))
    ok, msg = rig.core.broker.manual_move_rel('M1', 0.2, source='test')
    assert ok, msg
    assert b.state == AutoState.OFF            # owner stopped
    assert a.state == AutoState.OFF            # the paused automation waiting for M1 is stopped too
    stop_idx = next(i for i, e in enumerate(events) if e.source == b.id and e.data.get('new') == 'OFF')
    assert stop_idx >= 0
    assert rig.log[-1] == ('move_rel', 'mod1', 0.2)   # the manual order is sent after the stops
    assert any('action manuelle' in e.message for e in events)


def test_D5_external_move_detected(rig):
    a = make_pid(rig, 'A')
    rig.frame(s=1.0)
    assert rig.core.start(a.id)[0]
    rig.acts['mod1'].external_move(3.0)        # DAQ_Move UI used directly
    assert a.state == AutoState.OFF
    assert any(e.kind == 'move.external' for e in rig.core.events.history)


# --------------------------------------------------------------------------- S8
class Exploding(Automation):
    def __init__(self):
        super().__init__('BOOM')
        self.calls = 0

    def policy(self):
        from pymodaq_plugins_lasercontrol.core.safety import SafetyPolicy
        return SafetyPolicy(check_data=False, check_intensity=False, check_rate=False)

    def tick(self, core, now):
        self.calls += 1
        raise ZeroDivisionError('boom')


def test_S8_exception_freezes_once(rig):
    boom = rig.core.add_automation(Exploding())
    assert rig.core.start(boom.id)[0]
    rig.run(2.0)
    assert boom.state == AutoState.ERROR
    assert boom.calls == 1                    # no retry loop
    assert rig.core.global_state == GlobalState.ERROR
    errors = [e for e in rig.core.events.history if e.level >= Level.ERROR]
    assert len(errors) == 1


# --------------------------------------------------------------------------- S10 (no stacking / no queue)
def test_S10_no_order_stacked_on_busy_motor(rig):
    pid = make_pid(rig, kp=1.0, period_s=0.1)
    rig.frame(s=2.0)
    assert rig.core.start(pid.id)[0]
    for _ in range(30):                         # moves never complete
        rig.step(0.1, s=2.0, finish=False)
    assert len(rig.moves()) == 1
    assert pid.skipped_busy > 0


def test_move_timeout_holds(rig):
    pid = make_pid(rig, kp=1.0)
    rig.frame(s=2.0)
    assert rig.core.start(pid.id)[0]
    for _ in range(80):
        rig.step(0.1, s=2.0, finish=False)
    assert pid.state == AutoState.HELD and 'délai' in pid.reason


# --------------------------------------------------------------------------- S11
def test_S11_estop_stops_everything_and_latches(rig):
    a = make_pid(rig, 'A')
    rig.frame(s=2.0)
    assert rig.core.start(a.id)[0]
    rig.step(0.1, s=2.0, finish=False)
    rig.core.emergency_stop()
    assert a.state == AutoState.OFF
    for act in rig.acts.values():             # every actuator, including the one without role
        assert act.stopped == 1
    assert rig.core.global_state == GlobalState.ESTOP
    ok, _ = rig.core.start(a.id)
    assert not ok
    ok, _ = rig.core.broker.manual_move_rel('M1', 1.0)
    assert not ok
    rig.core.reset_estop()
    assert rig.core.global_state == GlobalState.IDLE
    ok, _ = rig.core.broker.manual_move_rel('M1', 1.0)
    assert ok


# --------------------------------------------------------------------------- S12
def test_S12_failed_read_is_nan_not_zero():
    class Broken:
        def __iter__(self):
            raise RuntimeError('corrupt data')
    assert extract_scalars(Broken(), 'det') == {}
    assert extract_scalars(None, 'det') == {}


def test_S12_pid_does_not_move_on_nan(rig):
    pid = make_pid(rig)
    rig.frame(s=2.0)
    assert rig.core.start(pid.id)[0]
    rig.step(0.1, s=2.0)
    n = len(rig.moves())
    rig.run(1.0, s=math.nan)
    assert len(rig.moves()) == n
    assert pid.state == AutoState.HELD


# --------------------------------------------------------------------------- optimizer vs PID (O3-A typical case)
def test_optimizer_pass_pauses_pid_then_pid_resumes(rig):
    pid = make_pid(rig, 'A', signal='S', motors={'M1': 1.0})
    opt = OptimizerAutomation({'interval_min': 10.0, 'dwell': 0.0, 'max_iterations': 2,
                               'run_mode': 'Joint (all motors at once)'},
                              motors=[{'role': 'M1', 'max_step': 0.5, 'probe_step': 0.1, 'order': 0}],
                              signals=[{'role': 'S2', 'mode': 'Maximize', 'weight': 1.0, 'p': 2.0}])
    rig.core.add_automation(opt)
    rig.frame(s=1.0, s2=1.0)
    assert rig.core.start(pid.id)[0]
    assert rig.core.start(opt.id)[0]
    seen_paused = False
    for _ in range(300):
        rig.step(0.1, s=1.0, s2=1.0 + 0.01 * rig.hb % 3)
        if pid.state == AutoState.PAUSED:
            seen_paused = True
        if opt.pass_count >= 1:
            break
    assert seen_paused
    assert opt.pass_count == 1
    assert opt.phase == 'waiting'
    assert pid.state == AutoState.ACTIVE          # resumed by itself after the pass
    assert rig.core.ownership.owner_of('mod1') == pid.id
