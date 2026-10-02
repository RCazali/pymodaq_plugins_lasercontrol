"""Routines (engines/routine.py): sequences of start / pause / resume / move steps with wait conditions."""
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.core.automation import Automation
from pymodaq_plugins_lasercontrol.core.safety import SafetyPolicy
from pymodaq_plugins_lasercontrol.engines.pid import PIDAutomation, PIDSettings
from pymodaq_plugins_lasercontrol.engines.routine import (Condition, Routine, RoutineAutomation, RoutineHost, Step,
                                                          delete_step, describe_until, duplicate_step, insert_step,
                                                          move_step, validate_routine)

from conftest import Rig


class CountDown(Automation):
    """Stands for one optimisation pass: finishes by itself after ``n`` ticks, moves nothing."""
    kind = 'test'

    def __init__(self, n=5):
        super().__init__('CD', 'Countdown')
        self.n = n
        self.k = 0
        self.period_s = 0.1

    def policy(self):
        return SafetyPolicy(check_data=False, check_intensity=False)

    def on_start(self, core):
        self.k = 0

    def tick(self, core, now):
        self.k += 1

    def is_done(self):
        return self.k >= self.n


def make_rig(**kw):
    """S follows M1, S2 follows M2 (the PIDs A and B regulate them)."""
    rig = Rig(max_step=0.5, max_excursion=20.0, **kw)
    rig.frame(s=0.0, s2=0.0)
    for pid_id, sig, motor, sp in (('A', 'S', 'M1', 1.0), ('B', 'S2', 'M2', -1.0)):
        st = PIDSettings(setpoint=sp, kp=0.5, ki=0.0, max_step=0.5, period_s=0.2)
        rig.core.add_automation(PIDAutomation(pid_id, sig, {motor: 1.0}, st, label=f'PID {pid_id}'))
    rig.core.add_automation(CountDown())
    return rig


def drive(rig, seconds, until=None):
    for _ in range(int(round(seconds / 0.1))):
        rig.clock.advance(0.1)
        rig.frame(s=rig.acts['mod1'].position(), s2=rig.acts['mod2'].position())
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        rig.core.tick()
        if until is not None and until():
            return True
    return False


def run_routine(rig, steps, seconds=60, **kw):
    r = RoutineAutomation(Routine('T', 'Test routine', steps=steps, **kw), RoutineHost(rig.core))
    rig.core.add_automation(r)
    ok, msg = rig.core.start(r.id)
    assert ok, msg
    drive(rig, seconds, until=lambda: r.state == AutoState.OFF)
    return r


def locked(target, then='next', tol=0.05, hold=1.0):
    return Condition('locked', target, value=tol, hold_s=hold, then=then)


# ---------------------------------------------------------------------- data and editing
def test_serialization_and_goto_renumbering():
    r = Routine('R', steps=[Step('start', 'PID:A', until=[locked('PID:A')]),
                            Step('none', until=[Condition('delay', value=1.0, then='goto', goto=1)]),
                            Step('stop', 'PID:A', timeout_s=5.0, on_timeout='goto', timeout_goto=2)])
    d = r.to_dict()
    assert Routine.from_dict(d).to_dict() == d
    assert validate_routine(r) == []
    move_step(r, 0, 2)                                # step 1 becomes step 3: the gotos follow it
    assert [s.action for s in r.steps] == ['none', 'stop', 'start']
    assert r.steps[0].until[0].goto == 3 and r.steps[1].timeout_goto == 1
    insert_step(r, 0, Step('note', text='hello'))
    assert r.steps[1].until[0].goto == 4 and r.steps[2].timeout_goto == 2
    duplicate_step(r, 0)
    assert r.steps[2].until[0].goto == 5
    delete_step(r, 4)                                 # the goto to the deleted step is reported
    assert any('does not exist' in e for e in validate_routine(r))
    assert 'converged' in describe_until(Step(until=[locked('PID:A')]))
    bad = Routine('B', steps=[Step('decouple', 'PID:A'), Step('move', 'M1', value=0.0),
                              Step('none', until=[Condition('travel', 'M1', value=0.0)])])
    errs = ' | '.join(validate_routine(bad))
    assert "unknown action 'decouple'" in errs and 'move by 0' in errs and 'travel must be > 0' in errs
    assert validate_routine(Routine('E')) == ['no step']


# ---------------------------------------------------------------------- core pause
def test_manual_pause_is_not_resumed_by_itself():
    rig = make_rig()
    a = rig.core.get('PID:A')
    assert rig.core.start(a.id)[0]
    drive(rig, 1.0)
    ok, _ = rig.core.pause(a.id, 'for a test', by='me')
    assert ok and a.state == AutoState.PAUSED and a.pause_kind == 'manual'
    assert rig.core.ownership.owner_of('mod1') is None            # nothing held while paused
    other = PIDAutomation('X', 'S', {'M1': 1.0}, PIDSettings(period_s=0.2), label='PID X')
    rig.core.add_automation(other)
    assert rig.core.start(other.id)[0]
    rig.core.stop(other.id)                                       # motors released: a preempted PID would resume
    assert a.state == AutoState.PAUSED
    n = len(rig.moves())
    drive(rig, 1.0)
    assert len(rig.moves()) == n                                   # nothing moves while paused
    ok, msg = rig.core.resume(a.id, 'test resume')
    assert ok, msg
    assert a.state == AutoState.ACTIVE and a.reason == 'test resume'
    assert not rig.core.pause('PID:B')[0]                         # not running


# ---------------------------------------------------------------------- sequences
def test_start_lock_pause_optimise_resume():
    """The daily case: PIDs locked, paused during another optimisation, resumed and locked again."""
    rig = make_rig()
    steps = [Step('start', 'PID:A', until=[locked('PID:A')], timeout_s=20),
             Step('start', 'PID:B', until=[locked('PID:B')], timeout_s=20),
             Step('pause', 'PID:A'), Step('pause', 'PID:B'),
             Step('start', 'CD', until=[Condition('finished', 'CD')], timeout_s=10),
             Step('resume', 'PID:A'), Step('resume', 'PID:B'),
             Step('none', until=[locked('PID:A', then='end')], timeout_s=20)]
    paused_during_cd = []

    def spy():
        if rig.core.get('CD').state == AutoState.ACTIVE:
            paused_during_cd.append((rig.core.get('PID:A').state, rig.core.get('PID:B').state))
        return False

    r = RoutineAutomation(Routine('T', 'Daily', steps=steps), RoutineHost(rig.core))
    rig.core.add_automation(r)
    assert rig.core.start(r.id)[0]
    for _ in range(600):
        drive(rig, 0.1)
        spy()
        if r.state == AutoState.OFF:
            break
    assert r.outcome == 'done', r.status
    assert paused_during_cd and all(s == (AutoState.PAUSED, AutoState.PAUSED) for s in paused_during_cd)
    assert rig.core.get('CD').reason == 'finished'
    # what the routine started keeps running after a successful end
    assert rig.core.get('PID:A').state == AutoState.ACTIVE and rig.core.get('PID:B').state == AutoState.ACTIVE
    assert rig.acts['mod1'].position() == pytest.approx(1.0, abs=0.05)
    assert rig.acts['mod2'].position() == pytest.approx(-1.0, abs=0.05)


def test_travel_limit_switches_to_the_next_motor():
    """PID A would need 1.0 on M1 but M1 may only travel 0.4: the routine then hands over to B."""
    rig = make_rig()
    steps = [Step('start', 'PID:A', until=[locked('PID:A', then='goto'),
                                            Condition('travel', 'M1', value=0.4, then='next')]),
             Step('stop', 'PID:A'), Step('start', 'PID:B', until=[locked('PID:B', then='end')], timeout_s=20)]
    steps[0].until[0].goto = 3
    r = run_routine(rig, steps)
    assert r.outcome == 'done', r.status
    assert rig.core.get('PID:A').state == AutoState.OFF
    assert 0.4 <= rig.acts['mod1'].position() < 0.4 + 0.5 + 1e-9        # at most one step beyond the limit
    assert any('moved ≥ 0.4' in text for _, text in r.history)


def test_timeout_fails_and_stops_what_it_started():
    rig = make_rig()
    rig.core.start('PID:B')                                            # started by the operator, not the routine
    steps = [Step('start', 'PID:A'), Step('none', until=[Condition('delay', value=100.0)], timeout_s=1.0)]
    r = run_routine(rig, steps, seconds=10)
    assert r.outcome == 'failed' and 'timeout' in r.reason
    assert rig.core.get('PID:A').state == AutoState.OFF
    assert rig.core.get('PID:B').state == AutoState.ACTIVE
    rig2 = make_rig()
    r2 = run_routine(rig2, steps, seconds=10, stop_started_on_abort=False)
    assert r2.outcome == 'failed' and rig2.core.get('PID:A').state == AutoState.ACTIVE


def test_a_held_or_stopped_automation_makes_the_routine_fail():
    rig = make_rig()
    steps = [Step('start', 'PID:A'), Step('none', until=[Condition('delay', value=100.0)])]
    r = RoutineAutomation(Routine('T', steps=steps), RoutineHost(rig.core))
    rig.core.add_automation(r)
    assert rig.core.start(r.id)[0]
    drive(rig, 1.0)
    rig.i = 0.1                                                         # intensity drop: the PID is held
    drive(rig, 1.0)
    assert r.state == AutoState.OFF and r.outcome == 'failed' and 'held' in r.reason
    assert rig.core.get('PID:A').state == AutoState.OFF                 # held -> stopped by the routine
    rig.i = 1.0
    drive(rig, 0.5)
    assert rig.core.start(r.id)[0]
    drive(rig, 1.0)
    rig.core.broker.manual_move_rel('M1', 0.1, 'jog')                   # a human takes the motor
    drive(rig, 0.3)
    assert r.outcome == 'failed' and 'manual action' in r.reason


def test_user_stop_leaves_paused_pids_paused():
    rig = make_rig()
    rig.core.start('PID:A')
    drive(rig, 0.5)
    steps = [Step('pause', 'PID:A'), Step('none', until=[Condition('delay', value=100.0)])]
    r = RoutineAutomation(Routine('T', steps=steps), RoutineHost(rig.core))
    rig.core.add_automation(r)
    assert rig.core.start(r.id)[0]
    drive(rig, 0.5)
    assert rig.core.get('PID:A').state == AutoState.PAUSED
    rig.core.stop(r.id, 'user')
    assert r.outcome == 'stopped' and rig.core.get('PID:A').state == AutoState.PAUSED
    assert any('still paused' in e.message for e in rig.core.events.recent(50))


def test_move_step_preempts_the_pid_of_that_motor():
    rig = make_rig()
    rig.core.start('PID:A')
    drive(rig, 3.0)
    states = []
    steps = [Step('move', 'M2', value=1.2), Step('move', 'M1', value=-0.8)]
    r = RoutineAutomation(Routine('T', steps=steps), RoutineHost(rig.core))
    rig.core.add_automation(r)
    assert rig.core.start(r.id)[0]
    start2 = rig.acts['mod2'].position()
    for _ in range(100):
        rig.clock.advance(0.1)
        rig.frame(s=rig.acts['mod1'].position(), s2=rig.acts['mod2'].position())
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        rig.core.tick()
        states.append(rig.core.get('PID:A').state)
        if r.state == AutoState.OFF:
            break
    assert r.outcome == 'done', r.status
    assert rig.acts['mod2'].position() == pytest.approx(start2 + 1.2)
    assert all(abs(m[2]) <= 0.5 + 1e-12 for m in rig.moves() if m[1] == 'mod2')      # bounded chunks
    assert AutoState.PAUSED in states                                  # preempted during the M1 move
    assert rig.core.get('PID:A').state == AutoState.ACTIVE              # and resumed by itself


def test_goto_loop_with_a_count_guard_and_signal_condition():
    rig = make_rig()
    steps = [Step('none', until=[Condition('delay', value=0.3)]),
             Step('none', until=[Condition('count', value=3, then='next'),
                                 Condition('delay', value=0.0, then='goto', goto=1)]),
             Step('start', 'PID:A', until=[Condition('signal', 'S', '>', 0.9, hold_s=1.0, then='end')], timeout_s=30)]
    r = run_routine(rig, steps)
    assert r.outcome == 'done' and r.visits == {1: 3, 2: 3, 3: 1}
    assert rig.core.signals.value('det/data/s') > 0.9


def test_start_refused_when_a_target_is_missing():
    rig = make_rig()
    r = RoutineAutomation(Routine('T', steps=[Step('start', 'PID:Nope'),
                                              Step('none', until=[Condition('signal', 'Nope', value=1.0)])]),
                          RoutineHost(rig.core))
    rig.core.add_automation(r)
    ok, msg = rig.core.start(r.id)
    assert not ok and 'PID:Nope does not exist' in msg and "signal role 'Nope'" in msg
