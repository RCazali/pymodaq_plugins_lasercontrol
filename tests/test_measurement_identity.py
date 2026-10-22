"""Camera results of computer B: one measurement published several times, acquisition time, frozen camera.

Computer B publishes at a fixed rate (2 Hz) whatever the analysis rate of each camera, with the frame number
and the age of each result (``<det>/<camera>/frame``, ``<det>/<camera>/age``): a result can be received
several times, and it was taken before it was received. Automations must use each measurement once, and
only a measurement taken after the end of their last move (+ settle)."""
import math

import pytest

from pymodaq_plugins_lasercontrol.core import AutoState
from pymodaq_plugins_lasercontrol.core.clock import FakeClock
from pymodaq_plugins_lasercontrol.core.signals import SignalSource
from pymodaq_plugins_lasercontrol.engines import PIDAutomation, PIDSettings

from conftest import Rig


def cam(frame, age, **values):
    out = {f'B/Cam1/{k}': v for k, v in values.items()}
    out.update({'B/Cam1/frame': frame, 'B/Cam1/age': age})
    return out


def test_a_republished_measurement_is_not_new_and_keeps_its_acquisition_time():
    clock = FakeClock()
    s = SignalSource(clock, max_age_s=1.0)
    s.ingest('B', cam(7, 0.2, x=1.0))
    first = s.latest('B/Cam1/x')
    assert first.seq == 1 and first.meas == 7 and first.t_acq == pytest.approx(clock.now() - 0.2)
    clock.advance(0.5)
    s.ingest('B', cam(7, 0.7, x=1.0))                  # same result, published again
    again = s.latest('B/Cam1/x')
    assert again.seq == 1 and again.t_acq == pytest.approx(first.t_acq)
    assert again.t == pytest.approx(clock.now())       # the reception time still moves (link alive)
    clock.advance(0.5)
    s.ingest('B', cam(9, 0.1, x=2.0))                  # frame 8 missed: a new measurement anyway
    new = s.latest('B/Cam1/x')
    assert new.seq == 2 and new.value == 2.0 and new.t_acq == pytest.approx(clock.now() - 0.1)


def test_a_frozen_camera_is_stale_although_computer_b_keeps_publishing():
    clock = FakeClock()
    s = SignalSource(clock, max_age_s=1.0)
    s.set_heartbeats({'B': 'B/Stats/frame_counter'})
    for k in range(12):                                # B alive (counter advancing), camera result frame 3
        s.ingest('B', {**cam(3, 0.1 + 0.25 * k, x=1.0), 'B/Stats/frame_counter': k})
        clock.advance(0.25)
    ok, why = s.freshness('B/Cam1/x')
    assert not ok and 'frozen' in why
    s.ingest('B', {**cam(4, 0.1, x=1.0), 'B/Stats/frame_counter': 12})
    assert s.is_fresh('B/Cam1/x')


def test_channels_without_frame_and_age_are_unchanged():
    clock = FakeClock()
    s = SignalSource(clock, max_age_s=1.0)
    for k in range(3):
        s.ingest('Det', {'Det/CamSim/frame': 5.0, 'Det/Mock0D/Mock_00': 1.0})   # 'frame' alone: a plain channel
        clock.advance(0.1)
    for key in ('Det/CamSim/frame', 'Det/Mock0D/Mock_00'):
        smp = s.latest(key)
        assert smp.seq == 3 and smp.meas is None and smp.t_acq == smp.t


def test_pid_uses_each_measurement_once_and_only_after_the_move():
    rig = Rig(max_step=1.0, max_excursion=50, rate_max=1000)
    roles = rig.core.roles
    roles.set_signal('X', 'det/cam/x')
    pid = rig.core.add_automation(PIDAutomation('P', 'X', {'M1': 1.0}, PIDSettings(
        setpoint=0.0, kp=0.5, sign_kp='+', max_step=1.0, period_s=0.2)))

    def deliver(frame, age, x=2.0, dt=0.25):
        rig.clock.advance(dt)
        rig.frame(**{'det/cam/x': x, 'det/cam/frame': frame, 'det/cam/age': age})
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        rig.core.tick()

    rig.frame(**{'det/cam/x': 2.0, 'det/cam/frame': 1, 'det/cam/age': 0.1})
    assert rig.core.start(pid.id)[0]
    deliver(1, 0.35)
    assert len(rig.moves()) == 1 and pid.iterations == 1
    for k in range(3):                                 # the same result received again: never a second move
        deliver(1, 0.6 + 0.25 * k)
    assert len(rig.moves()) == 1 and pid.iterations == 1
    assert pid.state == AutoState.ACTIVE
    deliver(2, 1.0)                                    # new frame, but taken before the end of the move
    assert len(rig.moves()) == 1 and pid.iterations == 1
    deliver(3, 0.05)                                   # taken after the move: used
    assert len(rig.moves()) == 2 and pid.iterations == 2


def test_pid_holds_on_a_frozen_camera_and_waits_for_a_new_measurement():
    rig = Rig(max_step=1.0, max_excursion=50, rate_max=1000)
    rig.core.roles.set_signal('X', 'det/cam/x')
    pid = rig.core.add_automation(PIDAutomation('P', 'X', {'M1': 1.0}, PIDSettings(
        setpoint=0.0, kp=0.5, sign_kp='+', max_step=1.0, period_s=0.2)))
    rig.frame(**{'det/cam/x': 2.0, 'det/cam/frame': 1, 'det/cam/age': 0.1})
    assert rig.core.start(pid.id)[0]
    age = 0.1
    for _ in range(12):                                # 3 s of the same frame
        age += 0.25
        rig.clock.advance(0.25)
        rig.frame(**{'det/cam/x': 2.0, 'det/cam/frame': 1, 'det/cam/age': age})
        for a in rig.acts.values():
            if a.moving:
                a.finish()
        rig.core.tick()
    assert pid.state == AutoState.HELD and 'frozen' in pid.reason
    n = len(rig.moves())
    assert n == 1 and not math.isnan(rig.core.signals.value('det/cam/x'))
