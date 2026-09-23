"""Helpers to create real PyMoDAQ control modules headless (offscreen Qt) for integration tests."""
import os
import time
import warnings

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
warnings.filterwarnings('ignore')

from qtpy import QtWidgets  # noqa: E402

_app = None


def app():
    global _app
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _app


def wait(pred=lambda: False, timeout=10.0, step=0.01):
    a = app()
    t0 = time.time()
    while time.time() - t0 < timeout:
        a.processEvents()
        if pred():
            return True
        time.sleep(step)
    a.processEvents()
    return pred()


def make_move(title, axis, parent=None, speed=200.0):
    from pymodaq.control_modules.daq_move import DAQ_Move
    mv = DAQ_Move(parent, title=title)
    mv.actuator = 'LaserSim'
    try:
        mv.settings.child('move_settings', 'multiaxes', 'axis').setValue(axis)
    except Exception:
        for p in mv.settings.child('move_settings').children():
            if p.name() == 'multiaxes':
                p.child('axis').setValue(axis)
    mv.settings.child('move_settings', 'speed').setValue(speed)
    inits = []
    mv.init_signal.connect(lambda s: inits.append(s))
    mv.init_hardware(True)
    assert wait(lambda: inits, 20), f'{title} not initialized'
    assert inits[-1], f'{title} init failed'
    wait(timeout=0.3)
    return mv


def make_viewer(title, parent=None, period_ms=100):
    from pymodaq.control_modules.daq_viewer import DAQ_Viewer
    det = DAQ_Viewer(parent, title=title)
    try:
        det.daq_type = 'DAQ0D'
    except Exception:
        pass
    det.detector = 'LaserSim'
    inits = []
    det.init_signal.connect(lambda s: inits.append(s))
    det.init_hardware(True)
    assert wait(lambda: inits, 20), f'{title} not initialized'
    assert inits[-1], f'{title} init failed'
    try:
        det.settings.child('detector_settings', 'wait_time').setValue(period_ms)
    except Exception:
        pass
    wait(timeout=0.3)
    return det


class FakeDashboard:
    """Just what CustomExt needs (the extension never uses more than the modules lists)."""

    def __init__(self, actuators, detectors, preset='test_preset'):
        from pathlib import Path
        self.actuators_modules = list(actuators)
        self.detector_modules = list(detectors)
        self.preset_file = Path(f'{preset}.xml')
        self.experiment_manager = None
        self.state_manager = None
        self.splash_sc = None
        self.extension_windows = []
