"""Integration with real PyMoDAQ control modules (DAQ_Move / DAQ_Viewer) and the LaserSim plugins."""
import math
import time

import pytest

pmd = pytest.importorskip('pymodaq')

from pmd_helpers import app, wait, make_move, make_viewer  # noqa: E402

from pymodaq_plugins_lasercontrol.core import (LaserCore, RoleMap, SafetyConfig, ActuatorLimits,  # noqa: E402
                                               IntensityGuardConfig, AutoState)
from pymodaq_plugins_lasercontrol.hardware.pymodaq_ports import DAQMovePort, DAQViewerPort  # noqa: E402
from pymodaq_plugins_lasercontrol.hardware.sim import LaserSimModel  # noqa: E402
from pymodaq_plugins_lasercontrol.engines import PIDAutomation, PIDSettings  # noqa: E402

pytestmark = pytest.mark.pymodaq


@pytest.fixture(scope='module')
def modules():
    app()
    model = LaserSimModel.instance()
    mv_h = make_move('Sim OC_H', 'OC_H')
    mv_v = make_move('Sim OC_V', 'OC_V')
    det = make_viewer('SimB')
    # the viewer pushes its settings to the model at init: set the test conditions afterwards
    det.settings.child('detector_settings', 'dropout_rate_per_min').setValue(0.0)
    det.settings.child('detector_settings', 'noise').setValue(0.2)
    wait(timeout=0.3)
    assert model.dropout_rate_per_min == 0.0
    yield model, mv_h, mv_v, det
    for m in (mv_h, mv_v, det):
        try:
            m.quit_fun()
        except Exception:
            pass
    wait(timeout=1.0)


def test_move_port_roundtrip(modules):
    model, mv_h, mv_v, det = modules
    port = DAQMovePort(mv_h)
    done, values = [], []
    port.on_done = lambda v: done.append(v)
    port.on_value = lambda v: values.append(v)
    port.request_position()
    assert wait(lambda: values, 10)
    p0 = port.position()
    assert p0 == pytest.approx(values[-1])
    assert p0 is not None and math.isfinite(p0)
    port.move_rel(2.0)
    assert wait(lambda: done, 10)
    assert done[-1] == pytest.approx(p0 + 2.0, abs=0.05)
    port.move_rel(-2.0)
    assert wait(lambda: len(done) >= 2, 10)
    assert not port.supports_native_jog() or callable(getattr(port, 'jog'))
    port.disconnect()


def test_viewer_port_channels_and_mute(modules):
    model, mv_h, mv_v, det = modules
    port = DAQViewerPort(det)
    frames = []
    port.on_frame = lambda v: frames.append(v)
    port.start_grab()
    assert wait(lambda: len(frames) >= 3, 10)
    keys = set(frames[-1])
    assert {'SimB/Cam1/x', 'SimB/Laser/power', 'SimB/Stats/frame_counter'} <= keys
    assert port.is_grabbing()
    model.muted = True
    wait(timeout=0.5)
    n = len(frames)
    wait(timeout=1.0)
    assert len(frames) <= n + 1                 # B silent: no more frames
    model.muted = False
    assert wait(lambda: len(frames) > n + 2, 10)  # and it comes back by itself
    port.stop_grab()
    wait(lambda: not port.is_grabbing(), 5)
    port.disconnect()


def test_core_pid_on_real_modules(modules):
    """PID on the broker with real DAQ_Moves and the LaserSim detector: moves are relative,
    bounded, never stacked, and the centroid is regulated."""
    model, mv_h, mv_v, det = modules
    roles = RoleMap(machine='sim')
    roles.set_actuator('OC_H', 'Sim OC_H')
    roles.set_actuator('OC_V', 'Sim OC_V')
    roles.set_signal('Cam1_x', 'SimB/Cam1/x')
    roles.set_signal('Power', 'SimB/Laser/power')
    roles.heartbeats = {'SimB': 'SimB/Stats/frame_counter'}
    cfg = SafetyConfig(max_data_age_s=1.5,
                       intensity=IntensityGuardConfig(enabled=True, role='Power', mode='relative', threshold=0.2),
                       actuators={'OC_H': ActuatorLimits(max_step=1.0, max_excursion=40, rate_max=200),
                                  'OC_V': ActuatorLimits(max_step=1.0, max_excursion=40, rate_max=200)})
    core = LaserCore(roles=roles, safety_config=cfg)
    core.broker.attach({'Sim OC_H': DAQMovePort(mv_h), 'Sim OC_V': DAQMovePort(mv_v)},
                       {'SimB': DAQViewerPort(det)})
    sent = []
    orig = core.broker.send_relative
    core.broker.send_relative = lambda owner, module, d: (sent.append((module, d, core.broker.is_busy(module))),
                                                           orig(owner, module, d))[1]
    core.broker.start_grab()
    assert wait(lambda: core.signals.is_fresh('SimB/Cam1/x'), 10)
    # 1) wrong sign (a classic tuning error): the loop diverges, the safety freezes it (S2/S4)
    bad = core.add_automation(PIDAutomation('BAD', 'Cam1_x', {'OC_H': -1.0},
                                            PIDSettings(setpoint=0.0, kp=0.6, ki=0.1, max_step=1.0, period_s=0.1)))
    ok, msg = core.start(bad.id)
    assert ok, msg
    t0 = time.time()
    while time.time() - t0 < 60 and bad.state == AutoState.ACTIVE:
        app().processEvents()
        core.tick()
        time.sleep(0.02)
    assert bad.state == AutoState.HELD, bad.reason
    ref = core.safety.reference(bad.id, 'Sim OC_H')
    assert abs(core.broker.position('Sim OC_H') - ref) <= 40 + 1e-6      # never beyond the envelope
    core.stop(bad.id)
    # back to the start point (manual, through the broker)
    ok, msg = core.broker.manual_move_abs('OC_H', ref, 'test')
    assert ok, msg
    assert wait(lambda: not core.broker.is_busy('Sim OC_H'), 20)
    core.safety.guard.captured_reference = None
    wait(timeout=1.0)
    # 2) correct sign: the centroid is regulated around 0
    pid = core.add_automation(PIDAutomation('X', 'Cam1_x', {'OC_H': 1.0},
                                            PIDSettings(setpoint=0.0, kp=0.6, ki=0.1, max_step=1.0, period_s=0.1)))
    sent.clear()
    ok, msg = core.start(pid.id)
    assert ok, msg
    t0 = time.time()
    while time.time() - t0 < 12:
        app().processEvents()
        core.tick()
        time.sleep(0.02)
    assert pid.state == AutoState.ACTIVE, pid.reason
    assert sent and all(abs(d) <= 1.0 + 1e-9 for _, d, _ in sent)
    assert not any(busy for _, _, busy in sent)            # never stacked on a busy motor
    x = core.signals.value('SimB/Cam1/x')
    assert abs(x) < 1.5, f'centroid not regulated: {x}'
    core.emergency_stop()
    assert pid.state == AutoState.OFF and core.estop
    core.broker.stop_grab()
    wait(timeout=0.5)
    core.broker.detach_all()
