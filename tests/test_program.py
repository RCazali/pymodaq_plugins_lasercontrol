"""Structured routines (engines/program.py): nested blocks, background blocks that interrupt the main
sequence, setpoint ramps, per-PID tolerances, conversion of the step routines."""
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.engines.pid import PIDAutomation, PIDSettings
from pymodaq_plugins_lasercontrol.engines.program import (Cond, ConversionError, Every, Node, Position, Program,
                                                          ProgramAutomation, from_routine, validate_program)
from pymodaq_plugins_lasercontrol.engines.routine import Condition, Routine, RoutineHost, Step
from pymodaq_plugins_lasercontrol.tools.sim_preset import demo_routines

from conftest import Rig
from test_routine import CountDown, drive, make_rig


def wait(s, **kw):
    return Node(until=[Cond('delay', value=s)], **kw)


def run_program(rig, p, seconds=60, spy=None):
    a = ProgramAutomation(p, RoutineHost(rig.core))
    rig.core.add_automation(a)
    ok, msg = rig.core.start(a.id)
    assert ok, msg
    for _ in range(int(round(seconds / 0.1))):
        drive(rig, 0.1)
        if spy is not None:
            spy(a)
        if a.state == AutoState.OFF:
            break
    return a


# ---------------------------------------------------------------------- data / conversion
def test_round_trip_and_validation():
    p = Program('P', 'Prog', main=[
        Node(action='start', target='PID:A'),
        Node(type='foreach', positions=[Position('P1', {'PID:A': [1.0]})], body=[
            Node(type='ramp', speed=0.5, tolerances={'PID:A': 0.3}),
            Node(until=[Cond('locked', tolerances={'PID:A': 0.1}, hold_s=1.0)], timeout_s=20)]),
        Node(type='repeat', times=3, body=[wait(1.0), Node(until=[Cond('signal', 'S', '>', 1.0, outcome='break')])])],
        background=[Every(30.0, 'HA', [Node(action='pause', target='PID:A'), Node(action='resume', target='PID:A')])])
    d = p.to_dict()
    assert Program.from_dict(d).to_dict() == d
    assert validate_program(p) == []
    bad = Program('B', main=[Node(until=[Cond('delay', value=1.0, outcome='break')]), Node(type='ramp', speed=0.0),
                             Node(type='repeat')])
    errs = ' | '.join(validate_program(bad))
    assert 'outside a loop' in errs and 'speed must be > 0' in errs and 'ramp without position' in errs
    assert 'empty REPEAT' in errs


def test_step_routines_are_converted_to_blocks():
    X = 'PID:Pointing_X'
    old = [   # step routines ("go to" model) as saved by the previous versions
        Routine('Straight', steps=[Step('start', X), Step('pause', X),
                                   Step('resume', X, until=[Condition('locked', X, value=0.5, hold_s=3.0)])]),
        Routine('Keep', steps=[Step('start', X), Step('none', until=[Condition('delay', value=120.0)]),
                               Step('pause', X), Step('resume', X),
                               Step('none', until=[Condition('locked', X, value=0.5, then='goto', goto=2)],
                                    timeout_s=60.0)]),
        Routine('Walk_X', steps=[
            Step('stop', X),
            Step('start', 'PID:BW_Near_X', until=[Condition('locked', 'PID:BW_Near_X', value=0.3, hold_s=2.0),
                                                  Condition('travel', 'BW1_H', value=15.0)],
                 timeout_s=30.0, on_timeout='next'),
            Step('stop', 'PID:BW_Near_X'),
            Step('start', 'PID:BW_Far_X', until=[Condition('locked', 'PID:BW_Far_X', value=0.3, hold_s=2.0)],
                 timeout_s=30.0, on_timeout='next'),
            Step('none', until=[Condition('count', value=8, then='fail'),
                                Condition('signal', 'Near_x', '|x|>', 0.6, then='goto', goto=2),
                                Condition('signal', 'Far_x', '|x|>', 0.6, then='goto', goto=2),
                                Condition('delay', value=0.5, then='end')])])]
    progs = {r.id: Program.from_dict(r.to_dict()) for r in old}           # old format -> program
    assert all(n.type == 'step' for n in progs['Straight'].main) and len(progs['Straight'].main) == 3
    keep = progs['Keep']
    assert [n.type for n in keep.main] == ['step', 'repeat']
    loop = keep.main[1]
    assert loop.times == 0 and len(loop.body) == 4
    last = loop.body[-1]
    assert [c.outcome for c in last.until] == ['next'] and last.on_timeout == 'fail'   # locked -> loop again
    assert [n.origin for n in progs['Keep'].walk()][-4:] == [1, 2, 3, 4]
    walk = progs['Walk_X']
    assert [n.type for n in walk.main] == ['step', 'repeat']
    body = walk.main[1].body
    assert len(body) == 4 and [c.outcome for c in body[-1].until] == ['fail', 'next', 'next', 'end']
    assert body[0].on_timeout == 'next'
    for p in progs.values():
        assert validate_program(p) == [], p.id
    with pytest.raises(ConversionError, match='forward'):
        from_routine(Routine('F', steps=[Step('none', until=[Condition('delay', then='goto', goto=3)]),
                                         Step('none'), Step('none')]))
    with pytest.raises(ConversionError, match='cross'):
        from_routine(Routine('X', steps=[Step('none'), Step('none'),
                                         Step('none', until=[Condition('delay', then='goto', goto=1)]),
                                         Step('none', until=[Condition('delay', then='goto', goto=2)])]))


# ---------------------------------------------------------------------- execution on the fake rig
def test_repeat_break_and_converted_routine_behave_the_same():
    rig = make_rig()
    p = Program('R', main=[Node(type='repeat', body=[
        Node(action='note', text='tick'),
        Node(until=[Cond('count', value=3, outcome='break'), Cond('delay', value=0.2)])]),
        Node(action='note', text='after')])
    a = run_program(rig, p, 20)
    assert a.outcome == 'done'
    assert a.visits[p.main[0].body[1].uid] == 3
    # the converted Walk-like routine (count guard + go to) runs as before
    rig2 = make_rig()
    r = Routine('W', steps=[Step('none', until=[Condition('delay', value=0.2)]),
                            Step('none', until=[Condition('count', value=3, then='next'),
                                                Condition('delay', value=0.0, then='goto', goto=1)])])
    a2 = run_program(rig2, from_routine(r), 20)
    assert a2.outcome == 'done' and sorted(a2.visits.values()) == [3, 3]


def test_background_block_interrupts_and_freezes_the_main_sequence():
    rig = make_rig()
    p = Program('B', main=[wait(3.0)],
                background=[Every(1.0, 'bg', [wait(0.5)])])
    t0 = rig.clock.now()
    seen_frozen = []

    def spy(a):
        node, frozen = a.current
        if frozen is not None:
            seen_frozen.append(round(a.main.t, 3))
    a = run_program(rig, p, 30, spy)
    assert a.outcome == 'done'
    assert a.interruptions >= 2
    assert a.main.t == pytest.approx(3.0, abs=0.15)                       # its delay counts only while it runs
    assert rig.clock.now() - t0 >= 3.0 + 0.5 * a.interruptions - 0.3      # wall time includes the interruptions
    assert seen_frozen and len(set(seen_frozen)) <= a.interruptions       # main clock frozen during each one


def test_foreach_ramp_waits_for_the_pid_and_is_frozen_by_the_background():
    rig = make_rig()
    pid = rig.core.get('PID:A')
    pid.settings.setpoint = 0.0
    p = Program('T', main=[
        Node(action='start', target='PID:A'),
        Node(until=[Cond('locked', tolerances={'PID:A': 0.05}, hold_s=0.5)], timeout_s=30),
        Node(type='foreach', positions=[Position('P1', {'PID:A': [1.0]}), Position('Final', {'PID:A': [2.0]})], body=[
            Node(type='ramp', speed=0.5, tolerances={'PID:A': 0.3}),
            Node(until=[Cond('locked', tolerances={'PID:A': 0.05}, hold_s=0.5)], timeout_s=30)])],
        background=[Every(1.5, 'bg', [Node(action='pause', target='PID:A'), wait(0.5),
                                      Node(action='resume', target='PID:A')])])
    gaps, frozen_sp = [], []

    def spy(a):
        node, frozen = a.current
        sp = pid.settings.setpoint
        pv = rig.core.signals.value('det/data/s')
        if node is not None and node.type == 'ramp' and pid.state == AutoState.ACTIVE:
            gaps.append(abs(sp - pv))
        if frozen is not None and frozen.type == 'ramp':
            frozen_sp.append(sp)
    a = run_program(rig, p, 120, spy)
    assert a.outcome == 'done', (a.reason, list(a.history)[-6:])
    assert pid.settings.setpoint == pytest.approx(2.0)
    assert rig.acts['mod1'].position() == pytest.approx(2.0, abs=0.05)
    assert max(gaps) < 0.3 + 0.5 * 0.1 + 0.05                            # the beam is never left behind
    assert frozen_sp and len(set(round(x, 9) for x in frozen_sp)) <= a.interruptions   # frozen ramp
    assert pid.state == AutoState.ACTIVE                                  # still running at the end


def test_failure_or_end_from_a_background_block():
    rig = make_rig()
    p = Program('F', main=[Node(action='start', target='PID:A'), wait(100.0)],
                background=[Every(1.0, 'check', [Node(until=[Cond('delay', value=0.2, outcome='fail')])])])
    a = run_program(rig, p, 10)
    assert a.outcome == 'failed' and rig.core.get('PID:A').state == AutoState.OFF   # what it started is stopped
    rig2 = make_rig()
    p2 = Program('E', main=[wait(100.0)],
                 background=[Every(1.0, 'done', [Node(until=[Cond('delay', value=0.1, outcome='end')])])])
    a2 = run_program(rig2, p2, 10)
    assert a2.outcome == 'done'


def test_hold_until_stopped_and_per_pid_locked_condition():
    rig = make_rig()
    p = Program('H', main=[Node(action='start', target='PID:A'), Node(action='start', target='PID:B'),
                           Node(until=[Cond('locked', tolerances={'PID:A': 0.05, 'PID:B': 0.05}, hold_s=1.0)],
                                timeout_s=30),
                           Node(action='hold')])
    a = run_program(rig, p, 20)
    assert a.state == AutoState.ACTIVE and 'Hold' in a.status
    assert rig.acts['mod1'].position() == pytest.approx(1.0, abs=0.05)
    assert rig.acts['mod2'].position() == pytest.approx(-1.0, abs=0.05)
    rig.core.stop(a.id, 'user')
    assert a.outcome == 'stopped' and rig.core.get('PID:A').state == AutoState.OFF


# ---------------------------------------------------------------------- the target program, on the simulator
def test_beam_transport_on_the_simulator():
    """Main: lock, then FOR EACH position ramp the near / far setpoints slowly and lock; background: every 30 s
    pause the PIDs, optimise the hard aperture, resume and re-lock. The ramp is interrupted at any moment."""
    from test_routine_sim import SimBench
    bench = SimBench(seed=8)
    X, Y = 'PID:Pointing_X', 'PID:Pointing_Y'
    locked = dict(tolerances={X: 0.5, Y: 0.8}, hold_s=3.0)
    prog = Program('Transport', 'Beam transport', main=[
        Node(action='start', target=X), Node(action='start', target=Y),
        Node(until=[Cond('locked', **locked)], timeout_s=300),
        Node(type='foreach', positions=[Position('P1', {X: [4.0, 1.0], Y: [2.0, 0.5]}),
                                        Position('Final', {X: [8.0, 2.0], Y: [4.0, 1.0]})], body=[
            Node(type='ramp', speed=0.1, tolerances={X: 0.5, Y: 0.8}),     # 40 s per segment > the 30 s period
            Node(until=[Cond('locked', **locked)], timeout_s=120)]),
        wait(40.0, text='stay on the final position (the HA keeps being optimised)')],
        background=[Every(30.0, 'HA', [
            Node(action='pause', target=X), Node(action='pause', target=Y),
            Node(action='start', target='OPT:Optim_HA', option='single',
                 until=[Cond('finished', 'OPT:Optim_HA')], timeout_s=240),
            Node(action='resume', target=X), Node(action='resume', target=Y),
            Node(until=[Cond('locked', **locked)], timeout_s=60)])])
    a = ProgramAutomation(prog, bench.host)
    bench.core.add_automation(a)
    ok, msg = bench.core.start(a.id)
    assert ok, msg
    paused_during_ha, ramp_interrupted = [], []
    opt = bench.core.get('OPT:Optim_HA')

    def spy():
        node, frozen = a.current
        if opt.state == AutoState.ACTIVE and opt.in_pass:
            paused_during_ha.append((bench.core.get(X).state, bench.core.get(Y).state))
        if frozen is not None and frozen.type == 'ramp':
            ramp_interrupted.append(True)
    assert bench.run(1500, until=lambda: a.state == AutoState.OFF, spy=spy)
    assert a.outcome == 'done', (a.reason, list(a.history)[-8:])
    assert a.interruptions >= 2 and ramp_interrupted                    # the HA interrupted the ramp
    assert paused_during_ha and all(s == (AutoState.PAUSED, AutoState.PAUSED) for s in paused_during_ha)
    for role, target in (('Near_x', 8.0), ('Far_x', 2.0), ('Near_y', 4.0), ('Far_y', 1.0)):
        assert bench.value(role) == pytest.approx(target, abs=1.0), role
    assert bench.value('HA_flux') > 5.0
