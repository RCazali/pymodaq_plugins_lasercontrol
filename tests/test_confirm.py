"""'Confirm maximum' (robust extremum seeking): the estimator, the optimiser pass on the simulated hard
aperture, and the operator question of a routine when the maximum is not confirmed."""
import math
import random

import numpy as np
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.engines.optimizer.confirm import PATTERN, fit_probe
from pymodaq_plugins_lasercontrol.engines.program import Cond, Node, Program, ProgramAutomation

from test_routine_sim import SimBench


# ---------------------------------------------------------------------- estimator
def _plateaus(x0, delta, curv=0.02, drift=0.0, noise=0.0, seed=1):
    """Cost of a maximum of the signal at x0 (cost minimised: a bowl), plateaus 0 +δ −δ −δ +δ 0."""
    rng = random.Random(seed)
    xs = [u * delta for u in PATTERN]
    return xs, [curv * (x - x0) ** 2 + drift * k + rng.gauss(0, noise) for k, x in enumerate(xs)]


def test_estimator_verdicts():
    xs, c = _plateaus(0.0, 1.5, noise=0.005)
    assert fit_probe(xs, c, 0.005).verdict() == 'max'
    xs, c = _plateaus(2.0, 1.5, noise=0.005)
    f = fit_probe(xs, c, 0.005)
    assert f.verdict() == 'slope' and -f.g / (2 * f.h) == pytest.approx(2.0, abs=0.3)   # Newton step to the top
    xs, c = _plateaus(0.0, 0.2, noise=0.02)                    # probe far too small for the noise
    assert fit_probe(xs, c, 0.02).verdict() == 'flat'


def test_a_linear_drift_does_not_look_like_a_slope():
    """The order + − − + cancels a linear drift (e.g. the flux changing for another physical reason)."""
    xs, c = _plateaus(0.0, 1.5, drift=0.05, noise=0.002)       # drift much larger than the curvature signal
    f = fit_probe(xs, c, 0.002)
    assert f.verdict() == 'max' and abs(f.g) < 3 * f.se_g
    assert f.drift == pytest.approx(0.05 * np.std(np.arange(len(xs))), abs=0.01)   # (per normalised index)


def test_global_level_changes_are_ignored():
    """Only "is the top here?": the absolute value of the signal is never compared."""
    xs, c = _plateaus(0.0, 1.5, noise=0.003)
    for offset in (-5.0, 0.0, 12.0):
        assert fit_probe(xs, [v + offset for v in c], 0.003).verdict() == 'max'


# ---------------------------------------------------------------------- the pass on the simulated aperture
def _confirm(bench, tries=5):
    opt = bench.core.get('OPT:Optim_HA')
    opt.config['confirm_tries'] = tries
    opt.run_mode_override = 'confirm'
    opt.single_shot = True
    ok, msg = bench.core.start(opt.id)
    assert ok, msg
    assert bench.run(400, until=lambda: opt.state == AutoState.OFF)
    return opt


def _lock_and_pause(bench):
    """As in a routine: the pointing PIDs lock the beam (the flux depends on the far field), then are paused
    during the check (nothing else moves)."""
    for pid in ('PID:Pointing_X', 'PID:Pointing_Y'):
        assert bench.core.start(pid)[0]
    assert bench.run(120, until=lambda: all(
        abs(e) < 0.3 for pid in ('PID:Pointing_X', 'PID:Pointing_Y') for e in bench.core.get(pid).errors()))
    bench.run(3)
    for pid in ('PID:Pointing_X', 'PID:Pointing_Y'):
        assert bench.core.pause(pid, 'test')[0]


def _aperture_at(bench, dx, lock=True):
    """Pointing locked then paused; hard aperture X at its (drifting) optimum + dx, Y at its optimum."""
    if lock:
        _lock_and_pause(bench)
    opt = bench.model.optimum(bench.clock.now())
    bench.acts['Sim HA_X'].external_move(opt['HA_X'] + dx)
    bench.acts['Sim HA_Y'].external_move(opt['HA_Y'])
    bench.step()


def test_confirm_at_the_top_moves_almost_nothing():
    bench = SimBench(seed=2)
    _aperture_at(bench, 0.0)
    x0 = bench.core.position('HA_X')
    opt = _confirm(bench)
    assert opt.last_result['confirmed'], opt.last_result
    assert abs(bench.core.position('HA_X') - x0) < 1.0                     # only the probes
    assert opt.last_result['motors']['HA_X']['tries'] == 1


def test_confirm_off_the_top_climbs_then_confirms():
    bench = SimBench(seed=3)
    _aperture_at(bench, 3.0)
    opt = _confirm(bench)
    assert opt.last_result['confirmed'], opt.last_result
    x = opt.last_result['motors']['HA_X']
    assert x['tries'] >= 2 and x['offset'] < -1.0                          # it climbed back towards the top
    assert x['loss'] <= 0.01 or abs(x['g']) < 3 * x['se_g']                 # within 1 % of the top when confirmed
    top = bench.model.optimum(bench.clock.now())['HA_X']
    assert abs(bench.core.position('HA_X') - top) < 2.0                    # (the top itself keeps moving)


def test_not_confirmed_when_far_and_few_tries():
    bench = SimBench(seed=4)
    _aperture_at(bench, 15.0)
    opt = _confirm(bench, tries=1)
    assert not opt.last_result['confirmed'] and 'not confirmed' in opt.last_result['message']
    assert opt.status.startswith('HA_X: maximum not confirmed')


# ---------------------------------------------------------------------- operator question in a program
def _ha_program(on_fail='ask'):
    X, Y = 'PID:Pointing_X', 'PID:Pointing_Y'
    return Program('HA', 'HA check', main=[
        Node(action='start', target=X), Node(action='start', target=Y),
        Node(until=[Cond('locked', tolerances={X: 0.5, Y: 0.8}, hold_s=2.0)], timeout_s=120),
        Node(action='pause', target=X), Node(action='pause', target=Y),
        Node(action='confirm', target='OPT:Optim_HA', on_fail=on_fail),
        Node(action='resume', target=X), Node(action='resume', target=Y),
        Node(until=[Cond('locked', tolerances={X: 0.5, Y: 0.8}, hold_s=2.0)], timeout_s=60)])


def _start(bench, prog):
    a = ProgramAutomation(prog, bench.host)
    bench.core.add_automation(a)
    ok, msg = bench.core.start(a.id)
    assert ok, msg
    return a


def test_not_confirmed_asks_the_operator_and_the_pids_keep_running():
    bench = SimBench(seed=5)
    _aperture_at(bench, 4.0, lock=False)          # 1 try: not enough; 6 tries: reachable
    opt = bench.core.get('OPT:Optim_HA')
    opt.config['confirm_tries'] = 1
    a = _start(bench, _ha_program())
    assert bench.run(400, until=lambda: a.question is not None)
    X, Y = bench.core.get('PID:Pointing_X'), bench.core.get('PID:Pointing_Y')
    assert X.state == AutoState.ACTIVE and Y.state == AutoState.ACTIVE         # the PIDs hold the beam
    assert 'waiting for the operator' in a.status and a.state == AutoState.ACTIVE
    n = len(a.history)
    bench.run(20)
    assert a.question is not None                                              # nothing goes on by itself
    # "check again" with more tries: the PIDs are paused again, the check runs again and confirms
    opt.config['confirm_tries'] = 6
    ok, msg = a.answer(bench.core, 'check')
    assert ok, msg
    bench.step()
    assert X.state == AutoState.PAUSED and Y.state == AutoState.PAUSED
    assert bench.run(600, until=lambda: a.state == AutoState.OFF)
    assert a.outcome == 'done', (a.reason, list(a.history)[-5:])
    assert opt.last_result['confirmed']
    assert X.state == AutoState.ACTIVE and Y.state == AutoState.ACTIVE


def test_operator_continue_and_stop():
    for choice, outcome in (('continue', 'done'), ('stop', 'stopped')):
        bench = SimBench(seed=6)
        _aperture_at(bench, 15.0, lock=False)
        bench.core.get('OPT:Optim_HA').config['confirm_tries'] = 1
        a = _start(bench, _ha_program())
        assert bench.run(400, until=lambda: a.question is not None)
        assert not a.answer(bench.core, 'maybe')[0]
        assert a.answer(bench.core, choice)[0]
        bench.run(120, until=lambda: a.state == AutoState.OFF)
        assert a.outcome == outcome, (choice, a.reason)
        if choice == 'stop':                                                   # what it started is stopped
            assert bench.core.get('PID:Pointing_X').state == AutoState.OFF
    bench = SimBench(seed=6)
    _aperture_at(bench, 15.0, lock=False)
    bench.core.get('OPT:Optim_HA').config['confirm_tries'] = 1
    a = _start(bench, _ha_program(on_fail='fail'))
    assert bench.run(400, until=lambda: a.state == AutoState.OFF)
    assert a.outcome == 'failed' and 'not confirmed' in a.reason
