"""Decoupling of several PIDs sharing motors: pure math (known matrices) and the multi-signal calibration."""
import math
import random

import numpy as np
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.engines.calibration import CalibrationAutomation, CalibrationResult
from pymodaq_plugins_lasercontrol.engines.decoupling import current_crosstalk, decouple, format_matrix
from pymodaq_plugins_lasercontrol.hardware.sim import BEAM_WALK, COUPLED_SIMPLE

from conftest import Rig


def _results(matrix, err=0.01):
    """{signal: {motor: CalibrationResult}} from {signal: {motor: g}}."""
    motors = sorted({m for row in matrix.values() for m in row})
    return {s: {m: CalibrationResult(g=row.get(m, 0.0), stderr=err, ok=True) for m in motors}
            for s, row in matrix.items()}, motors


def test_coupled_mount_is_decoupled():
    results, motors = _results(COUPLED_SIMPLE)
    signals = ['Coupled/x', 'Coupled/y']
    res = decouple(results, signals, motors)
    assert res.ok, res.notes
    J = np.array([[COUPLED_SIMPLE[s].get(m, 0.0) for m in motors] for s in signals])
    for k, s in enumerate(signals):
        w = np.array([res.weights[s][m] for m in motors])
        resp = J @ w
        assert abs(resp[1 - k]) < 1e-3 * abs(resp[k])        # the other signal does not move
        assert max(abs(w)) == pytest.approx(1.0)              # the motor that moves most has weight ±1
        assert res.gains[s] == pytest.approx(resp[k])
        assert res.crosstalk[s] < 1e-3
    # with one motor each (today's demo PIDs) the coupling is 25 % / 35 %
    now = current_crosstalk(J, signals, motors, {'Coupled/x': {'CPL_A': 1.0}, 'Coupled/y': {'CPL_B': 1.0}})
    assert now['Coupled/x'] == pytest.approx(0.25) and now['Coupled/y'] == pytest.approx(0.35)
    text = format_matrix(res, {'Coupled/x': {'CPL_A': 1.0}, 'Coupled/y': {'CPL_B': 1.0}})
    assert 'κ' in text and '25 % →' in text


def test_beam_walk_four_signals_four_motors():
    results, motors = _results(BEAM_WALK)
    signals = list(BEAM_WALK)
    res = decouple(results, signals, motors)
    assert res.ok, res.notes
    assert 1.0 < res.cond < 100.0
    assert all(res.crosstalk[s] < 0.01 for s in signals)
    assert all(res.gains[s] > 0 for s in signals)             # loop sign '+' for every PID


def test_more_motors_than_signals_minimum_norm():
    matrix = {'a': {'M1': 1.0, 'M2': 0.5, 'M3': 0.2}, 'b': {'M1': -0.3, 'M2': 1.0, 'M3': 0.4}}
    res = decouple(_results(matrix)[0], ['a', 'b'], ['M1', 'M2', 'M3'])
    assert res.ok, res.notes
    assert res.crosstalk['a'] < 1e-3 and res.crosstalk['b'] < 1e-3


def test_refusals():
    # redundant motors: two columns almost equal
    matrix = {'a': {'M1': 1.0, 'M2': 1.0}, 'b': {'M1': 0.5, 'M2': 0.501}}
    res = decouple(_results(matrix)[0], ['a', 'b'], ['M1', 'M2'])
    assert not res.ok and 'condition number' in res.notes[0]
    # fewer motors than signals
    res = decouple({'a': {'M1': CalibrationResult(g=1.0)}, 'b': {'M1': CalibrationResult(g=2.0)}}, ['a', 'b'], ['M1'])
    assert not res.ok and 'at least as many motors' in res.notes[0]
    # a pair not calibrated
    res = decouple({'a': {'M1': CalibrationResult(g=1.0)}}, ['a', 'b'], ['M1', 'M2'])
    assert not res.ok and 'not calibrated' in res.notes[0]
    # one signal only
    assert not decouple({}, ['a'], ['M1']).ok


def test_calibration_errors_are_reported_as_possible_coupling():
    results, motors = _results(COUPLED_SIMPLE, err=0.3)
    res = decouple(results, ['Coupled/x', 'Coupled/y'], motors)
    assert res.ok
    assert res.crosstalk_err['Coupled/x'] > 0.2
    assert any('remaining coupling' in n for n in res.notes)


def test_decoupling_proposal_for_a_coupled_pid():
    """Settings of every loop from the measured matrix; refused when the errors allow too much coupling."""
    from pymodaq_plugins_lasercontrol.engines.decoupling import decoupling_proposal
    J = {'S': {'M1': 1.0, 'M2': 0.8}, 'S2': {'M1': 1.0, 'M2': 1.5}}

    def results(stderr):
        return {s: {m: CalibrationResult(g=g, stderr=stderr, noise_sigma=0.1, settle_s=0.3, ok=True)
                    for m, g in row.items()} for s, row in J.items()}
    prop, res, problems = decoupling_proposal(results(0.005), ['S', 'S2'], ['M1', 'M2'])
    assert prop is not None and not problems
    for k, s in enumerate(('S', 'S2')):
        w = prop[s]['weights']
        resp = {x: sum(J[x][m] * w[m] for m in w) for x in J}
        other = 'S2' if s == 'S' else 'S'
        assert abs(resp[other]) < 1e-3 and prop[s]['kp'] == pytest.approx(0.5 / resp[s], rel=1e-3)
        assert prop[s]['sign_kp'] == '+' and prop[s]['deadband'] == pytest.approx(0.2)
    prop, res, problems = decoupling_proposal(results(0.3), ['S', 'S2'], ['M1', 'M2'], max_coupling=0.25)
    assert prop is None and any('remaining coupling' in p for p in problems)


def test_calibration_measures_every_signal_at_once():
    """One run: each motor measured on S and S2 (one column of J each), motors back to the start."""
    rig = Rig(max_step=0.5)
    rig.frame(s=0.0, s2=0.0)
    cal = CalibrationAutomation('S', ['M1', 'M2'], cycles=2, samples=3, noise_samples=10, extra_signals=['S2'])
    rig.core.add_automation(cal)
    ok, msg = rig.core.start(cal.id)
    assert ok, msg
    rng = random.Random(4)
    J = np.array([[2.0, 0.7], [-0.5, 1.5]])
    for _ in range(15000):
        rig.clock.advance(0.05)
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        m = np.array([rig.acts['mod1'].position(), rig.acts['mod2'].position()])
        s = J @ m
        rig.frame(s=s[0] + rng.gauss(0, 0.02), s2=s[1] + rng.gauss(0, 0.02))
        rig.core.tick()
        if cal.state == AutoState.OFF:
            break
    assert cal.completed, cal.reason
    for i, sig in enumerate(('S', 'S2')):
        for j, mot in enumerate(('M1', 'M2')):
            r = cal.results_by[sig][mot]
            assert abs(r.g - J[i, j]) < 3 * r.stderr + 1e-3, (sig, mot, r.g, r.stderr)
    assert cal.results is cal.results_by['S']
    assert rig.acts['mod1'].position() == pytest.approx(0.0) and rig.acts['mod2'].position() == pytest.approx(0.0)
    res = decouple(cal.results_by, ['S', 'S2'], ['M1', 'M2'])
    assert res.ok, res.notes
    assert all(res.crosstalk[s] < 0.05 for s in ('S', 'S2'))
    assert not math.isnan(res.cond)
