"""The real PyMoDAQ dashboard loads the extension through the ``pymodaq.extensions`` entry point.

* PyMoDAQ 5.1: preset file + ``DashBoard.load_extension_from_name('Laser Control')``
* PyMoDAQ 5.2: experiment file + ``load_dashboard_with_experiment(..., 'LASERCONTROL')``

Two LaserSim motors and one LaserSim 0D viewer are declared in a temporary preset / experiment
(removed at the end). Checks: discovery, loading by the dashboard, ports on the dashboard modules,
a manual move through the broker, E-STOP, closing."""
import os
import time

import pytest

pytest.importorskip('pymodaq')

from pmd_helpers import app, wait  # noqa: E402
from qtpy import QtWidgets  # noqa: E402

pytestmark = pytest.mark.pymodaq

NAME = 'lc_pytest_dashboard'
MOVES = (('Sim OC_H', 'OC_H'), ('Sim OC_V', 'OC_V'))
DETECTOR = 'SimB'


def _is_52() -> bool:
    from pymodaq_plugins_lasercontrol.tools.sim_preset import is_pymodaq_52
    return is_pymodaq_52()


def run(seconds: float):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app().processEvents()
        time.sleep(0.01)


@pytest.fixture(scope='module')
def loaded(tmp_path_factory):
    import pymodaq_plugins_lasercontrol.ui.app as app_mod
    from pymodaq.dashboard import DashBoard
    app()
    data = tmp_path_factory.mktemp('lc_dash')
    cwd = os.getcwd()
    os.chdir(data)          # the 5.1 dashboard creates its default data folder relative to the cwd
    orig_resolve = app_mod.resolve_paths
    app_mod.resolve_paths = lambda config, machine, override=None: orig_resolve(config, 'dash', data)
    orig_check = getattr(DashBoard, 'check_update', None)
    DashBoard.check_update = lambda self, show=True: False          # no network in tests
    yes = QtWidgets.QMessageBox.Yes
    saved = (QtWidgets.QMessageBox.question, QtWidgets.QMessageBox.information, QtWidgets.QMessageBox.warning)
    QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: yes)
    QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: None)
    QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: None)
    from pymodaq_plugins_lasercontrol.tools.sim_preset import write_sim_config
    path = write_sim_config(NAME, MOVES, (DETECTOR,), overwrite=True)   # the office simulator tool
    if _is_52():
        from pymodaq.dashboard import load_dashboard_with_experiment
        from pymodaq_plugins_lasercontrol.extensions.laser_control import CLASS_NAME
        dashboard, extension, win = load_dashboard_with_experiment(NAME, CLASS_NAME.upper())
    else:
        from pymodaq_gui.utils.dock import DockArea
        win = QtWidgets.QMainWindow()
        area = DockArea()
        win.setCentralWidget(area)
        dashboard = DashBoard(area)
        dashboard.set_preset_mode(path)
        extension = dashboard.load_extension_from_name('Laser Control')
    run(1.0)
    yield dashboard, extension, data
    try:
        if extension is not None:
            extension.app.core.broker.stop_grab()
        run(0.5)
        dashboard.quit_fun()
        run(1.0)
    finally:
        os.chdir(cwd)
        app_mod.resolve_paths = orig_resolve
        if orig_check is not None:
            DashBoard.check_update = orig_check
        (QtWidgets.QMessageBox.question, QtWidgets.QMessageBox.information,
         QtWidgets.QMessageBox.warning) = saved
        for p in path.parent.glob(f'{NAME}*'):
            try:
                p.unlink()
            except OSError:
                pass


def test_extension_is_discovered():
    from pymodaq.extensions.utils import get_extensions
    exts = get_extensions()
    items = exts.values() if isinstance(exts, dict) else exts
    names = [getattr(e, 'name', None) or e.get('name') for e in items]
    assert 'Laser Control' in names


def test_dashboard_loads_the_extension(loaded):
    dashboard, ext, data = loaded
    from pymodaq_plugins_lasercontrol.extensions.laser_control import LaserControl
    assert isinstance(ext, LaserControl)
    a = ext.app
    assert sorted(a.core.broker.modules()) == sorted(t for t, _ in MOVES)
    assert list(a.core.broker.detectors) == [DETECTOR]
    assert wait(lambda: all(a.core.broker.position(t) is not None for t, _ in MOVES), 10)
    assert ext.has_action('estop') and ext.has_action('record')
    if _is_52():
        assert ext.has_action('show_dashboard')       # needed by DashBoard.load_extension (5.2)


def test_roles_move_and_estop(loaded):
    dashboard, ext, data = loaded
    a = ext.app
    a.start_detectors()
    assert wait(lambda: f'{DETECTOR}/Cam1/x' in a.core.signals.channels(), 15)
    a.tabs['config']._auto_roles()
    a.tabs['config']._save_roles()
    assert a.core.roles.module_for('Sim_OC_H') == 'Sim OC_H'
    assert a.core.roles.actuators['Sim_OC_H'].units      # filled from the actuator
    before = a.core.broker.position('Sim OC_H')
    assert a.manual.move_rel('Sim_OC_H', 2.0, 'test')
    assert wait(lambda: abs(a.core.broker.position('Sim OC_H') - before - 2.0) < 0.05, 15)
    a.emergency_stop('test')
    assert a.core.estop
    a.core.reset_estop()
    assert not a.core.estop
