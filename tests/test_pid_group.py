"""Coupled PID: one PID regulating several signals with shared motors (one order per motor per period)."""
import random

import numpy as np
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.engines.pid import PIDAutomation, PIDController, PIDLoop, PIDSettings
from pymodaq_plugins_lasercontrol.presets import normalize, normalize_pid

from conftest import Rig

J = np.array([[1.0, 0.5], [-0.3, 1.0]])            # S, S2 vs M1, M2: strong coupling


def _decoupled_weights():
    W = np.linalg.pinv(J)
    return [dict(zip(('M1', 'M2'), W[:, k] / np.max(np.abs(W[:, k])))) for k in range(2)]


def _coupled(weights, sp=(1.0, -0.5), kp=0.5):
    loops = []
    for k, sig in enumerate(('S', 'S2')):
        gain = float((J @ np.array([weights[k].get('M1', 0.0), weights[k].get('M2', 0.0)]))[k])
        st = PIDSettings(setpoint=sp[k], kp=kp / gain, ki=0.0, max_step=0.5, period_s=0.1)
        loops.append(PIDLoop(sig, weights[k], st))
    return PIDAutomation('AB', label='PID AB', loops=loops)


def _drive(rig, a, steps=400, noise=0.0, seed=1, per_tick=None):
    rng = random.Random(seed)
    for _ in range(steps):
        rig.clock.advance(0.05)
        for x in rig.acts.values():
            if x.moving:
                x.finish()
        m = np.array([rig.acts['mod1'].position(), rig.acts['mod2'].position()])
        s = J @ m
        rig.frame(s=s[0] + rng.gauss(0, noise), s2=s[1] + rng.gauss(0, noise))
        n = len(rig.moves(('move_rel',)))
        rig.core.tick()
        if per_tick is not None:
            per_tick(rig.moves(('move_rel',))[n:])
        if a.state == AutoState.OFF:
            break


def test_scaled_keeps_the_direction():
    c = PIDController(PIDSettings(max_step=0.5))
    steps = c.scaled(2.0, {'M1': 1.0, 'M2': -0.25})
    assert steps['M1'] == pytest.approx(0.5) and steps['M2'] == pytest.approx(-0.125)
    assert c.scaled(0.1, {'M1': 1.0, 'M2': 0.0}) == {'M1': pytest.approx(0.1)}


def test_coupled_pid_regulates_both_signals():
    rig = Rig(max_step=0.5, max_excursion=10.0)
    rig.frame(s=0.0, s2=0.0)
    a = rig.core.add_automation(_coupled(_decoupled_weights()))
    assert a.motor_roles() == ['M1', 'M2'] and a.signal_roles() == ['S', 'S2']
    ok, msg = rig.core.start(a.id)
    assert ok, msg
    orders = []

    def one_order_per_motor(new):
        mods = [m[1] for m in new]
        assert len(mods) == len(set(mods))          # never two orders on the same motor in one period
        orders.extend(new)

    _drive(rig, a, steps=300, per_tick=one_order_per_motor)
    assert a.state == AutoState.ACTIVE
    s = J @ np.array([rig.acts['mod1'].position(), rig.acts['mod2'].position()])
    assert s[0] == pytest.approx(1.0, abs=0.02) and s[1] == pytest.approx(-0.5, abs=0.02)
    assert all(l.iterations > 5 and len(l.trace) == l.iterations for l in a.loops)
    assert all(abs(o[2]) <= 0.5 + 1e-12 for o in orders)
    assert set(a.variables()) >= {'S.error', 'S2.error', 'iterations'}


def test_decoupled_converges_faster_than_one_motor_each():
    """Same loop gain; the coupled plant with one motor per signal needs more iterations."""
    def iterations_to_converge(weights):
        rig = Rig(max_step=0.5, max_excursion=10.0)
        rig.frame(s=0.0, s2=0.0)
        a = rig.core.add_automation(_coupled(weights))
        assert rig.core.start(a.id)[0]
        for _ in range(600):
            _drive(rig, a, steps=1)
            s = J @ np.array([rig.acts['mod1'].position(), rig.acts['mod2'].position()])
            if abs(s[0] - 1.0) < 0.01 and abs(s[1] + 0.5) < 0.01:
                return a.iterations
        return 10 ** 6

    assert iterations_to_converge(_decoupled_weights()) < iterations_to_converge([{'M1': 1.0}, {'M2': 1.0}])


def test_human_action_stops_the_coupled_pid():
    rig = Rig(max_step=0.5)
    rig.frame(s=0.0, s2=0.0)
    a = rig.core.add_automation(_coupled(_decoupled_weights()))
    assert rig.core.start(a.id)[0]
    _drive(rig, a, steps=20)
    rig.core.broker.manual_move_rel('M2', 0.1, 'jog')
    assert a.state == AutoState.OFF and 'manual action' in a.reason


def test_validation_and_config_round_trip():
    rig = Rig()
    rig.frame(s=0.0, s2=0.0)
    a = _coupled(_decoupled_weights())
    a.loops[1].signal_role = 'S'
    assert any('same signal' in e for e in a.validate(rig.core))
    cfg = {'id': 'P', 'motors': ['M1', 'M2'], 'settings': {'max_step': 0.3, 'period_s': 0.2},
           'loops': [{'signal': 'S', 'kp': 0.7, 'sign': '-', 'weights': {'M1': 1.0, 'M2': -0.4}},
                     {'signal': 'S2', 'kp': 0.2, 'weights': {'M2': 1.0}}]}
    p = PIDAutomation.from_config(normalize_pid(cfg))
    assert p.signal_roles() == ['S', 'S2'] and p.loops[0].settings.gains()[0] == pytest.approx(-0.7)
    assert p.loops[1].weights == {'M1': 0.0, 'M2': 1.0} and p.loops[0].settings.max_step == 0.3
    live = normalize_pid(cfg)
    live['loops'][0]['setpoint'] = 2.5
    live['loops'][0]['weights'] = {'M1': 9.0, 'M2': 9.0}
    p.configure(live, structure=False)               # engaged: gains / setpoints only, never the weights
    assert p.loops[0].settings.setpoint == 2.5 and p.loops[0].weights['M1'] == 1.0


def test_schema2_pids_and_groups_are_migrated():
    v2 = {'schema_version': 2, 'pids': [
        {'id': 'A', 'label': 'Near', 'signal': 'S', 'motors': {'M1': 1.0, 'M2': -0.5}, 'group': 'G1',
         'settings': {'setpoint': 1.0, 'kp': 0.4, 'sign_kp': '-', 'max_step': 0.7, 'deadband': 0.1},
         'calibration': {'M1': {'g': 1.0, 'stderr': 0.01, 'ok': True}}},
        {'id': 'Solo', 'signal': 'S3', 'motors': {'M3': 1.0}, 'settings': {'kp': 0.3}},
        {'id': 'B', 'label': 'Far', 'signal': 'S2', 'motors': {'M2': 1.0}, 'group': 'G1', 'settings': {'kp': 0.2}}]}
    p = normalize(v2)
    assert p['schema_version'] == 3 and [x['id'] for x in p['pids']] == ['A', 'Solo']
    a = p['pids'][0]
    assert [l['signal'] for l in a['loops']] == ['S', 'S2'] and a['motors'] == ['M1', 'M2']
    assert a['loops'][0]['sign'] == '-' and a['loops'][0]['setpoint'] == 1.0 and a['settings']['max_step'] == 0.7
    assert a['loops'][1]['weights'] == {'M1': 0.0, 'M2': 1.0} and a['label'] == 'Near + Far'
    assert a['calibration'] == {'S': {'M1': {'g': 1.0, 'stderr': 0.01, 'ok': True}}}
    assert p['pids'][1]['loops'][0]['weights'] == {'M3': 1.0}
    assert normalize(p) == p
