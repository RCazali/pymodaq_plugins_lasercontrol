"""Automatic calibration (docs/pid_roadmap.md § 1): pure fit, suggested settings, automation on the rig."""
import math
import random

import numpy as np
import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.engines.calibration import (CalibrationAutomation, CalibrationResult, Plateau,
                                                              fit_sensitivity, settling_time, suggest_pid_settings)

from conftest import Rig


def _plateaus(g, offsets, s0=10.0, drift=0.0, noise=0.0, seed=1, dt=1.0):
    rng = random.Random(seed)
    return [Plateau(t=i * dt, m=m, s=s0 + g * m + drift * i * dt + rng.gauss(0, noise), n=5)
            for i, m in enumerate(offsets)]


BIPOLAR = [0, .5, 0, -.5, 0] * 3


# --------------------------------------------------------------------------- pure math
def test_fit_recovers_gain_and_sign():
    for g in (2.0, -3.5):
        r = fit_sensitivity(_plateaus(g, BIPOLAR, noise=0.02), noise_sigma=0.05, samples=5)
        assert r.ok, r.warnings
        assert r.g == pytest.approx(g, rel=0.05)


def test_fit_is_robust_to_linear_drift():
    r = fit_sensitivity(_plateaus(2.0, BIPOLAR, drift=0.05, noise=0.01), noise_sigma=0.02, samples=5)
    assert r.ok
    assert r.g == pytest.approx(2.0, rel=0.05)
    assert r.drift == pytest.approx(0.05, rel=0.2)


def test_fit_refuses_insignificant_response():
    r = fit_sensitivity(_plateaus(0.0, BIPOLAR, noise=0.5), noise_sigma=0.5, samples=5)
    assert not r.ok
    assert any('not significant' in w for w in r.warnings)


def test_fit_refuses_nonlinear_response():
    pts = _plateaus(2.0, BIPOLAR)
    for p in pts:                                    # quadratic response: residual >> noise
        p.s += 40.0 * p.m ** 2
    r = fit_sensitivity(pts, noise_sigma=0.01, samples=5)
    assert not r.ok
    assert any('non-linear' in w for w in r.warnings)


def test_fit_accepts_linear_response_with_correlated_drift():
    """A slow random walk of the beam makes the plateaus scatter more than the frame noise: the
    response is still linear, so it is accepted (warning only), g within its error (seen on the
    simulator beam walk bench)."""
    rng = random.Random(7)
    walk, pts = 0.0, []
    for i, m in enumerate([0, .75, 0, -.75, 0] * 4):
        walk += rng.gauss(0, 0.12)
        pts.append(Plateau(t=i * 0.8, m=m, s=1.5 * m + walk + rng.gauss(0, 0.02), n=5))
    r = fit_sensitivity(pts, noise_sigma=0.05, samples=5)
    assert r.ok, r.warnings
    assert abs(r.g - 1.5) < 3 * r.stderr
    assert any('scatter' in w for w in r.warnings)
    assert not r.curvature_significant or r.curvature <= 0.3


def test_fit_warns_on_asymmetric_backlash():
    pts = _plateaus(2.0, BIPOLAR)
    for p in pts:                                    # the − steps respond at 1/3 of the + steps
        if p.m < 0:
            p.s = 10.0 + 0.6 * p.m
    r = fit_sensitivity(pts, noise_sigma=0.01, samples=5)
    assert r.asymmetry > 0.3
    assert any('asymmetric' in w for w in r.warnings)


def test_fit_needs_enough_plateaus_and_warns_about_settling():
    assert not fit_sensitivity([Plateau(0, 0, 1), Plateau(1, 1, 2)]).ok
    r = fit_sensitivity(_plateaus(2.0, BIPOLAR, noise=0.01), noise_sigma=0.02, samples=5, period_s=0.5, settle_s=2.0)
    assert any('settling time' in w for w in r.warnings)


def test_suggested_settings():
    good = fit_sensitivity(_plateaus(-4.0, BIPOLAR, noise=0.01), noise_sigma=0.1, samples=5, settle_s=0.3)
    out, notes = suggest_pid_settings({'M1': good}, {'M1': 1.0})
    assert out['sign_kp'] == '-'
    assert out['kp'] == pytest.approx(0.5 / 4.0, rel=0.05)
    assert out['deadband'] == pytest.approx(0.2)
    assert out['settle_s'] == pytest.approx(0.3)
    # weights: the loop gain is the weighted sum of the sensitivities
    out, _ = suggest_pid_settings({'M1': good}, {'M1': 2.0})
    assert out['kp'] == pytest.approx(0.5 / 8.0, rel=0.05)


def test_suggested_settings_refuses_bad_or_missing():
    bad = CalibrationResult(g=1.0, ok=False)
    out, notes = suggest_pid_settings({'M1': bad}, {'M1': 1.0})
    assert out is None and 'refused' in notes[0]
    out, notes = suggest_pid_settings({}, {'M1': 1.0})
    assert out is None and 'not calibrated' in notes[0]
    a = CalibrationResult(g=1.0, ok=True)
    b = CalibrationResult(g=1.0, ok=True)
    out, notes = suggest_pid_settings({'M1': a, 'M2': b}, {'M1': 1.0, 'M2': -1.0})
    assert out is None and 'cancel' in notes[0]


def test_settling_time():
    frames = [(0.0, 5.0), (0.1, 3.0), (0.2, 1.4), (0.3, 1.02), (0.4, 1.0), (0.5, 0.99)]
    assert settling_time(frames, 0.0, 1.0, 0.1) == pytest.approx(0.3)
    assert settling_time([(0.0, 1.0), (0.1, 1.0)], 0.0, 1.0, 0.1) == 0.0


def test_result_to_dict_is_what_the_preset_keeps():
    r = CalibrationResult(g=2.0, stderr=0.1, noise_sigma=0.05, settle_s=0.4, ok=True, warnings=['w'])
    d = r.to_dict('2026-01-01T00:00:00')
    assert d == {'g': 2.0, 'stderr': 0.1, 'sigma': 0.05, 'settle_s': 0.4, 'ok': True, 'warnings': ['w'],
                 'date': '2026-01-01T00:00:00'}
    back = CalibrationResult.from_dict(d)
    assert (back.g, back.stderr, back.noise_sigma, back.settle_s, back.ok) == (2.0, 0.1, 0.05, 0.4, True)
    assert not CalibrationResult.from_dict({}).ok


# --------------------------------------------------------------------------- automation on the rig
def _run(rig, cal, gain, noise=0.02, drift=0.0, seed=3, steps=6000, human_at=None):
    rng = random.Random(seed)
    t0 = rig.clock.now()
    for k in range(steps):
        rig.clock.advance(0.05)
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        pos = rig.acts['mod1'].position()
        rig.frame(s=10.0 + gain * pos + drift * (rig.clock.now() - t0) + rng.gauss(0, noise))
        rig.core.tick()
        if human_at is not None and k == human_at:
            rig.core.broker.manual_move_rel('M1', 0.1, 'jog')
        if cal.state == AutoState.OFF and k > 5:
            break


def test_calibration_measures_gain_and_returns_to_start():
    rig = Rig(max_step=0.5, max_excursion=1e-6, rate_max=0.001)   # explicit envelope: excursion/rate ignored
    rig.frame(s=10.0)
    cal = CalibrationAutomation('S', ['M1'], cycles=2, samples=3, noise_samples=10)
    rig.core.add_automation(cal)
    ok, msg = rig.core.start(cal.id)
    assert ok, msg
    _run(rig, cal, gain=2.0)
    assert cal.completed and cal.state == AutoState.OFF
    r = cal.results['M1']
    assert r.ok, r.warnings
    assert r.g == pytest.approx(2.0, rel=0.05)
    assert r.noise_sigma == pytest.approx(0.02, rel=0.6)
    assert rig.acts['mod1'].position() == pytest.approx(0.0)
    assert all(abs(m[2]) <= 0.5 + 1e-12 for m in rig.moves(('move_rel',)))


def test_calibration_negative_gain_with_drift():
    rig = Rig(max_step=0.5)
    rig.frame(s=10.0)
    cal = CalibrationAutomation('S', ['M1'], cycles=3, samples=3, noise_samples=10)
    rig.core.add_automation(cal)
    assert rig.core.start(cal.id)[0]
    _run(rig, cal, gain=-3.0, drift=0.02)
    r = cal.results['M1']
    assert r.ok, r.warnings
    assert r.g == pytest.approx(-3.0, rel=0.05)
    suggestion, _ = suggest_pid_settings(cal.results, {'M1': 1.0})
    assert suggestion['sign_kp'] == '-'


def test_calibration_doubles_a_too_small_step():
    rig = Rig(max_step=1.0)
    rig.frame(s=10.0)
    cal = CalibrationAutomation('S', ['M1'], cycles=2, samples=3, noise_samples=10)
    rig.core.add_automation(cal)
    assert rig.core.start(cal.id)[0]
    _run(rig, cal, gain=0.3, noise=0.05)          # first delta = 0.25 -> 0.075 change < 5 sigma
    assert cal.results['M1'].delta > 0.25
    assert cal.results['M1'].g == pytest.approx(0.3, rel=0.15)


def test_calibration_stops_on_human_action():
    rig = Rig(max_step=0.5)
    rig.frame(s=10.0)
    cal = CalibrationAutomation('S', ['M1'], cycles=2, samples=3, noise_samples=10)
    rig.core.add_automation(cal)
    assert rig.core.start(cal.id)[0]
    _run(rig, cal, gain=2.0, human_at=40)
    assert cal.state == AutoState.OFF and not cal.completed


def test_calibration_two_motors_in_sequence():
    rig = Rig(max_step=0.5)
    rig.frame(s=10.0)
    cal = CalibrationAutomation('S', ['M1', 'M2'], cycles=2, samples=3, noise_samples=10)
    rig.core.add_automation(cal)
    assert rig.core.start(cal.id)[0]
    rng = random.Random(5)
    for _ in range(12000):
        rig.clock.advance(0.05)
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        rig.frame(s=10.0 + 2.0 * rig.acts['mod1'].position() - 1.0 * rig.acts['mod2'].position()
                  + rng.gauss(0, 0.02))
        rig.core.tick()
        if cal.state == AutoState.OFF:
            break
    assert cal.completed
    assert cal.results['M1'].g == pytest.approx(2.0, rel=0.05)
    assert cal.results['M2'].g == pytest.approx(-1.0, rel=0.05)
    assert rig.acts['mod1'].position() == pytest.approx(0.0) and rig.acts['mod2'].position() == pytest.approx(0.0)


def test_calibration_validate_needs_max_step():
    rig = Rig(max_step=1.0)
    rig.core.safety.config.actuators['M1'].max_step = None
    cal = CalibrationAutomation('S', ['M1'])
    rig.core.add_automation(cal)
    ok, msg = rig.core.start(cal.id)
    assert not ok
