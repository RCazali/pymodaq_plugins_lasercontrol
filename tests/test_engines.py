"""Engines: PID math, optimiser algorithms (ask/tell), scans, sequencer."""
import math
import random

import numpy as np
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.engines import (PIDAutomation, PIDController, PIDSettings, Scan1DAutomation,
                                                  XYScanAutomation, OptimizerAutomation, scan_positions,
                                                  snake_pattern)
from pymodaq_plugins_lasercontrol.engines.optimizer import (OptContext, Measure, MoveTo, Report, Status,
                                                            create_algorithm, CostModel)

from conftest import Rig


# --------------------------------------------------------------------------- PID math
def test_pid_signs_and_clamp_after_weighting():
    s = PIDSettings(setpoint=1.0, kp=2.0, sign_kp='-', ki=0.0, kd=0.0, max_step=0.5)
    c = PIDController(s)
    t = c.update(0.0, 0.5)             # error = +1, kp signed = -2
    assert t.output == pytest.approx(-2.0)
    steps = c.split(t.output, {'A': 1.0, 'B': 50.0, 'C': 0.0, 'D': -0.1})
    assert steps['A'] == pytest.approx(-0.5)      # clamped
    assert steps['B'] == pytest.approx(-0.5)      # large weight still clamped AFTER weighting
    assert 'C' not in steps
    assert steps['D'] == pytest.approx(0.2)


def test_pid_anti_windup_and_no_derivative_kick():
    s = PIDSettings(setpoint=0.0, kp=0.0, ki=1.0, kd=10.0, max_step=0.3)
    c = PIDController(s)
    t = c.update(-100.0, 0.5)
    assert t.d == 0.0                 # no derivative kick on the first iteration
    for _ in range(100):
        t = c.update(-100.0, 0.5)
    assert abs(t.i) <= 0.3 + 1e-12    # |ki * integral| <= max_step


def test_pid_deadband_freezes_integral():
    s = PIDSettings(setpoint=0.0, kp=1.0, ki=1.0, deadband=0.1, max_step=1.0)
    c = PIDController(s)
    c.update(-0.5, 0.5)
    integ = c.integral
    t = c.update(0.05, 0.5)
    assert t.in_deadband and t.output == 0.0 and c.integral == integ


def test_pid_settings_legacy_keys():
    s = PIDSettings.from_dict({'kp': 1, 'sample_rate': 0.25, 'confirm_before_move': True, 'sign_ki': '-'})
    assert s.period_s == 0.25 and s.confirm_moves is True and s.sign_ki == '-'


# --------------------------------------------------------------------------- closed loop on the rig
def test_pid_closed_loop_converges():
    rig = Rig(max_step=0.5, max_excursion=50, rate_max=1000)
    gain = 2.0                                         # signal = gain * motor position + offset
    rig.frame(s=3.0)
    pid = PIDAutomation('X', 'S', {'M1': 1.0}, PIDSettings(setpoint=0.0, kp=0.3, sign_kp='+', ki=0.05,
                                                             max_step=0.5, period_s=0.2))
    rig.core.add_automation(pid)
    ok, msg = rig.core.start(pid.id)
    assert ok, msg
    for _ in range(300):
        rig.clock.advance(0.05)
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        pos = rig.acts['mod1'].position()
        rig.frame(s=3.0 + gain * pos)
        rig.core.tick()
    assert pid.state == AutoState.ACTIVE
    assert abs(3.0 + gain * rig.acts['mod1'].position()) < 0.05


def test_pid_capture_setpoint_and_confirm_mode():
    rig = Rig()
    rig.frame(s=1.234)
    s = PIDSettings(setpoint_mode='capture', kp=0.5, max_step=1.0, confirm_moves=True)
    pid = rig.core.add_automation(PIDAutomation('C', 'S', {'M1': 1.0}, s))
    answers = []
    rig.core.confirm_handler = lambda a, text, cb: answers.append((a, text, cb))
    assert rig.core.start(pid.id)[0]
    assert pid.settings.setpoint == pytest.approx(1.234)
    rig.step(0.6, s=2.0)
    assert pid.state == AutoState.PAUSED and pid.pause_kind == 'confirm'
    assert answers and 'Δ' in answers[0][1]
    assert rig.moves() == []                           # nothing moves while the dialog is open
    answers[0][2](True)
    assert pid.state == AutoState.ACTIVE
    assert len(rig.moves()) == 1
    rig.step(0.6, s=2.0)
    answers[-1][2](False)                              # refused -> stopped (historical behaviour)
    assert pid.state == AutoState.OFF


# --------------------------------------------------------------------------- optimiser algorithms
def run_algorithm(mode, f, motors, config=None, full=False, seed=0):
    cfg = {'max_iterations': 20, 'stall_patience': 5, 'deadband_scale': 0.01, 'safety_factor': 5.0,
           'max_backoffs': 3, 'gradient_samples': 1, 'damping_factor': 1.0, 'full_optimization': full,
           'full_opt_iterations': 40, 'full_opt_stall_patience': 10}
    cfg.update(config or {})
    ctx = OptContext(motors=motors, config=cfg, deadband=0.001, rng=random.Random(seed))
    algo = create_algorithm(mode)
    algo.start(ctx)
    pos = {m['role']: 0.0 for m in motors}
    visited = []
    reports = []
    result = None
    for _ in range(5000):
        algo.observe(result)
        req = algo.propose()
        if req is None:
            break
        result = None
        if isinstance(req, (Measure, MoveTo)):
            pos = dict(req.offsets)
            visited.append(dict(pos))
            if isinstance(req, Measure):
                result = f(pos)
        elif isinstance(req, Report):
            reports.append(req.cost)
    return pos, visited, reports


@pytest.mark.parametrize('mode', ['Joint (all motors at once)', 'Sequential (one motor at a time)',
                                  'Nelder-Mead (simplex)', 'Gradient / Newton-lite (local descent)'])
def test_algorithms_improve_a_quadratic(mode):
    opt = {'A': 0.3, 'B': -0.2}
    f = lambda p: (p['A'] - opt['A']) ** 2 + (p['B'] - opt['B']) ** 2
    motors = [{'role': 'A', 'max_step': 0.5, 'probe_step': 0.1, 'order': 0},
              {'role': 'B', 'max_step': 0.5, 'probe_step': 0.1, 'order': 1}]
    final, visited, reports = run_algorithm(mode, f, motors)
    assert f(final) < f({'A': 0.0, 'B': 0.0})
    assert visited
    if mode.startswith(('Nelder', 'Gradient')):        # clamped to +/- max_step from the pass start
        assert all(abs(v) <= 0.5 + 1e-12 for p in visited for v in p.values())


def test_create_algorithm_accepts_legacy_labels():
    assert create_algorithm('Nelder-Mead (simplex)').name == 'nelder-mead'
    assert create_algorithm('gradient').name == 'gradient'
    assert create_algorithm('whatever').name == 'joint'


def test_cost_model_modes():
    cm = CostModel([{'role': 'P', 'mode': 'Maximize', 'weight': 1.0},
                    {'role': 'X', 'mode': 'Target Point', 'weight': 1.0, 'p': 2.0, 'auto_target': True}])
    cm.estimate({'P': [10, 10, 10], 'X': [1.0, 1.2, 0.8]}, 1.0)
    assert cm.deadband == pytest.approx(0.5)
    assert cm.cost({'P': 20.0, 'X': 1.0}) < cm.cost({'P': 10.0, 'X': 1.0})
    assert cm.cost({'P': 10.0, 'X': 1.0}) < cm.cost({'P': 10.0, 'X': 2.0})


def test_optimizer_automation_full_pass_on_rig():
    rig = Rig(max_step=0.2, max_excursion=5, rate_max=1000)
    best = 0.7
    opt = OptimizerAutomation({'dwell': 0.0, 'max_iterations': 15, 'run_mode': 'Nelder-Mead (simplex)',
                               'deadband_scale': 0.01, 'stall_patience': 4},
                              motors=[{'role': 'M1', 'max_step': 1.0, 'probe_step': 0.2, 'order': 0}],
                              signals=[{'role': 'S2', 'mode': 'Maximize', 'weight': 1.0}])
    opt.single_shot = True
    rig.core.add_automation(opt)
    rig.frame(s2=10.0)
    assert rig.core.start(opt.id)[0]
    rng = np.random.default_rng(0)
    for _ in range(3000):
        rig.clock.advance(0.05)
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        x = rig.acts['mod1'].position()
        rig.frame(s2=10.0 - 3 * (x - best) ** 2 + rng.normal(0, 0.01))
        rig.core.tick()
        if opt.state == AutoState.OFF:
            break
    assert opt.state == AutoState.OFF and opt.pass_count == 1
    assert abs(rig.acts['mod1'].position() - best) < 0.3
    assert all(abs(m[2]) <= 0.2 + 1e-12 for m in rig.moves())   # S3 chunks
    assert rig.core.ownership.owner_of('mod1') is None


# --------------------------------------------------------------------------- scans
def test_scan_positions_and_snake():
    assert scan_positions(0, 1, 0.25) == pytest.approx([0, 0.25, 0.5, 0.75, 1.0])
    assert scan_positions(1, 0, 0.5) == pytest.approx([1, 0.5, 0.0])
    pat = snake_pattern(0, 1, 1, 0, 1, 1)
    assert pat == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def test_scan1d_records_and_returns():
    rig = Rig(max_step=0.3, max_excursion=0.1, rate_max=0.1)    # scan ignores excursion/rate (explicit range)
    rig.frame(s=1.0)
    scan = Scan1DAutomation('M1', -1.0, 1.0, 0.5, dwell=0.0, samples=2, channels=['S'], relative=True,
                            return_to_start=True)
    rig.core.add_automation(scan)
    ok, msg = rig.core.start(scan.id)
    assert ok, msg
    for _ in range(1000):
        rig.clock.advance(0.05)
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        rig.frame(s=10 + rig.acts['mod1'].position())
        rig.core.tick()
        if scan.state == AutoState.OFF:
            break
    assert scan.completed
    xs = [r[0] for r in scan.results]
    assert xs == pytest.approx([-1.0, -0.5, 0.0, 0.5, 1.0])
    assert [r[1]['S'] for r in scan.results] == pytest.approx([9.0, 9.5, 10.0, 10.5, 11.0])
    assert rig.acts['mod1'].position() == pytest.approx(0.0)
    assert all(abs(m[2]) <= 0.3 + 1e-12 for m in rig.moves())


def test_xyscan_visits_pattern_and_stops_on_human_action():
    rig = Rig(max_step=1.0)
    rig.frame()
    xy = XYScanAutomation('g1', 'M1', 'M2', 0, 2, 1, 0, 1, 1, dwell=0.0)
    rig.core.add_automation(xy)
    assert rig.core.start(xy.id)[0]
    for _ in range(40):
        rig.step(0.05)
    assert xy.index >= 2
    ok, _ = rig.core.broker.manual_move_rel('M2', 0.1, 'jog')
    assert ok and xy.state == AutoState.OFF
