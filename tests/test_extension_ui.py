"""End-to-end test of the extension UI with real PyMoDAQ control modules and the LaserSim plugins
(offscreen Qt). Exercises: roles creation, safety limits, PID start from the PID tab, one-click
stabilisation, human preemption from the Alignment keyboard, optimiser pass, scans, recording,
presets save/load, E-STOP, closing."""
import math
import time
from pathlib import Path

import pytest

pytest.importorskip('pymodaq')

from pmd_helpers import app, wait, make_move, make_viewer, FakeDashboard  # noqa: E402
from qtpy import QtCore, QtWidgets  # noqa: E402

pytestmark = pytest.mark.pymodaq


def run(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app().processEvents()
        time.sleep(0.01)


@pytest.fixture(scope='module')
def ext(tmp_path_factory):
    from pymodaq_gui.utils.dock import DockArea
    from pymodaq_plugins_lasercontrol.hardware.sim import LaserSimModel
    import pymodaq_plugins_lasercontrol.ui.app as app_mod
    import pymodaq_plugins_lasercontrol.ui.widgets as widgets_mod
    app()
    data = tmp_path_factory.mktemp('lc_data')
    # isolate every file of the test in a temporary data folder
    orig_resolve = app_mod.resolve_paths
    app_mod.resolve_paths = lambda config, machine, override=None: orig_resolve(config, 'test', data)
    # dialogs: always "Yes", no modal popups
    yes = QtWidgets.QMessageBox.Yes
    QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: yes)
    QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: None)
    QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: None)
    mv_h = make_move('Sim OC_H', 'OC_H')
    mv_v = make_move('Sim OC_V', 'OC_V')
    mv_j = make_move('Sim JET_X', 'JET_X')
    det = make_viewer('SimB')
    det.settings.child('detector_settings', 'dropout_rate_per_min').setValue(0.0)
    det.settings.child('detector_settings', 'noise').setValue(0.2)
    model = LaserSimModel.instance()
    dash = FakeDashboard([mv_h, mv_v, mv_j], [det])
    win = QtWidgets.QMainWindow()
    area = DockArea()
    win.setCentralWidget(area)
    from pymodaq_plugins_lasercontrol.extensions.laser_control import LaserControl
    e = LaserControl(area, dashboard=dash)
    win.show()
    run(0.5)
    yield e, win, model, data
    if not e.app._closing:
        win.close()
        run(0.3)
    e.app.core.broker.stop_grab()
    det.stop_grab()
    run(0.5)
    for m in (mv_h, mv_v, mv_j, det):
        m.quit_fun()
    run(1.0)
    app_mod.resolve_paths = orig_resolve


def test_01_nothing_started_and_tabs(ext):
    e, win, model, data = ext
    a = e.app
    assert set(a.tabs) == {'overview', 'alignment', 'xy', 'pid', 'optimizer', 'scan', 'recording', 'presets', 'config'}
    assert a.core.global_state.value == 'IDLE'
    assert all(x.state.value == 'OFF' for x in a.core.automations.values())      # S9
    for i in range(a.tab_widget.count()):
        a.tab_widget.setCurrentIndex(i)
        run(0.25)
    assert a.core.broker.modules() == ['Sim OC_H', 'Sim OC_V', 'Sim JET_X']


def test_02_roles_and_safety_from_config_tab(ext):
    e, win, model, data = ext
    a = e.app
    a.start_detectors()
    assert wait(lambda: 'SimB/Cam1/x' in a.core.signals.channels(), 10)
    cfg_tab = a.tabs['config']
    cfg_tab._auto_roles()
    cfg_tab._save_roles()
    assert (data / 'roles_test.toml').exists()
    roles = a.core.roles
    assert roles.module_for('Sim_OC_H') == 'Sim OC_H'
    assert roles.channel_for('Cam1_x') == 'SimB/Cam1/x'
    # heartbeat + safety limits + intensity guard, through the widgets
    from pymodaq_plugins_lasercontrol.core.safety import SafetyConfig, ActuatorLimits, IntensityGuardConfig
    roles.heartbeats['SimB'] = 'SimB/Stats/frame_counter'
    a.apply_roles(roles, save=True)
    safety = SafetyConfig(max_data_age_s=1.5,
                          intensity=IntensityGuardConfig(enabled=True, role='Laser_power', threshold=0.2),
                          actuators={r: ActuatorLimits(max_step=1.0, max_excursion=30.0, rate_max=500.0)
                                     for r in ('Sim_OC_H', 'Sim_OC_V', 'Sim_JET_X')})
    cfg_tab.load_safety(safety)
    cfg_tab._on_safety_changed()
    assert a.core.safety.config.limits('Sim_OC_H').max_step == 1.0
    assert a.core.safety.config.intensity.role == 'Laser_power'
    a.tab_widget.setCurrentWidget(cfg_tab)
    run(0.5)


def test_03_pid_from_tab_and_stabilisation(ext):
    e, win, model, data = ext
    a = e.app
    pid_tab = a.tabs['pid']
    pid_tab._add_widget({'id': 'H', 'label': 'Centroïde H', 'in_stabilization': True, 'signal': 'Cam1_x',
                         'motors': {'Sim_OC_H': 1.0},
                         'settings': {'setpoint': 0.0, 'kp': 0.5, 'ki': 0.05, 'max_step': 1.0, 'period_s': 0.2}})
    a.tab_widget.setCurrentWidget(a.tabs['overview'])
    run(0.5)
    assert 'Centroïde H' in a.tabs['overview'].summary.text()
    a.activate_stabilization()
    pid = a.core.get('PID:H')
    assert pid is not None and pid.state.value == 'ACTIVE', (pid.state, pid.reason)
    run(6.0)
    assert pid.state.value == 'ACTIVE', pid.reason
    assert pid.iterations > 5
    assert abs(a.core.signals.value('SimB/Cam1/x')) < 2.0
    a.tab_widget.setCurrentWidget(pid_tab)
    run(0.5)
    assert 'measure' in pid_tab.widgets['H'].values.text()


def test_04_alignment_keyboard_preempts_pid(ext):
    e, win, model, data = ext
    a = e.app
    pid = a.core.get('PID:H')
    assert pid.state.value == 'ACTIVE'
    align = a.tabs['alignment']
    cfg = align.get_config()
    cfg['axes'][0]['h']['actuator'] = 'Sim_OC_H'
    cfg['axes'][0]['h']['mode'] = 'step'
    cfg['axes'][0]['h']['slow'] = 0.5
    cfg['axes'][0]['h']['invert'] = False
    align.load_config(cfg)
    a.tab_widget.setCurrentWidget(align)
    run(0.3)
    align.select_axis(0)
    pos0 = a.core.broker.position('Sim OC_H')
    ev = QtWidgets.QApplication.instance()
    press = QtCore.QEvent.KeyPress
    from qtpy import QtGui
    align.handle_key_press(QtGui.QKeyEvent(press, QtCore.Qt.Key_D, QtCore.Qt.NoModifier, 'd'))
    run(0.3)
    align.handle_key_release(QtGui.QKeyEvent(QtCore.QEvent.KeyRelease, QtCore.Qt.Key_D, QtCore.Qt.NoModifier, 'd'))
    run(1.0)
    assert pid.state.value == 'OFF'                       # D5: stopped by the human action
    assert 'manual action' in pid.reason
    assert a.core.broker.position('Sim OC_H') > pos0 + 0.2
    assert any('manual action' in ev.message for ev in a.core.events.history)


def test_05_optimizer_one_pass(ext):
    e, win, model, data = ext
    a = e.app
    opt_tab = a.tabs['optimizer']
    cfg = {'id': 'Jet', 'label': 'Optim jet',
           'config': {'interval_min': 10.0, 'dwell': 0.0, 'max_iterations': 2, 'run_mode': 'Joint (all motors at once)',
                      'probe_samples': 1, 'noise_samples': 3},
           'motors': [{'role': 'Sim_JET_X', 'max_step': 1.0, 'probe_step': 0.5, 'order': 0}],
           'signals': [{'role': 'XUV_flux', 'mode': 'Maximize', 'weight': 1.0, 'p': 2.0, 'target_value': 0.0,
                        'auto_target': True}]}
    opt_tab.load_from_preset({'optimizers': [cfg]})
    a.tab_widget.setCurrentWidget(opt_tab)
    ok, msg = a.start_optimizer(opt_tab.config_of('OPT:Jet'), single_shot=True)
    assert ok, msg
    opt = a.optimizer('Jet')
    assert opt.label == 'Optim jet'
    t0 = time.time()
    while time.time() - t0 < 40 and opt.state.value != 'OFF':
        run(0.2)
    assert opt.state.value == 'OFF', (opt.phase, opt.reason)
    assert opt.pass_count == 1
    assert len(opt.history) >= 1
    # a second optimisation lives next to the first one
    opt_tab.add(dict(cfg, id='Jet2', label='Optim jet bis'))
    assert [c['id'] for c in opt_tab.configs()] == ['Jet', 'Jet2']
    opt_tab.tabs.setCurrentWidget(opt_tab.widgets['Jet2'])
    a.core.remove_automation('OPT:Jet2')
    opt_tab.widgets.pop('Jet2').deleteLater()
    opt_tab.tabs.removeTab(opt_tab.tabs.currentIndex())


def test_05b_alignment_buttons(ext):
    """PID / optimisation buttons of the Alignment tab: one click, no confirmation."""
    e, win, model, data = ext
    a = e.app
    a.tabs['pid'].widgets['H'].settings.child('show_in_alignment').setValue(True)
    a.tabs['optimizer'].widgets['Jet'].settings.child('show_in_alignment').setValue(True)
    a.tab_widget.setCurrentWidget(a.tabs['alignment'])
    run(0.6)
    panel = a.quick_panel
    assert panel.isVisible()
    assert list(panel.cards) == ['PID:H', 'OPT:Jet']
    assert panel.cards['PID:H'].name.text() == 'Centroïde H'
    panel._on_click('PID:H')
    pid = a.core.get('PID:H')
    assert pid.state.value == 'ACTIVE', pid.reason
    panel._on_click('PID:H')                   # debounced: a double click does not stop it
    assert pid.state.value == 'ACTIVE'
    run(0.8)
    assert 'Active' in panel.cards['PID:H'].line.text()
    panel._on_click('PID:H')
    assert pid.state.value == 'OFF'
    run(0.6)
    panel._on_click('OPT:Jet')                 # 'one pass' by default
    opt = a.optimizer('Jet')
    assert opt.state.value in ('ACTIVE', 'ARMED'), opt.reason
    t0 = time.time()
    while time.time() - t0 < 40 and opt.state.value != 'OFF':
        run(0.2)
    assert opt.state.value == 'OFF' and opt.pass_count == 2
    # refusals are written under the buttons (and in the journal), never in a dialog
    a.core.emergency_stop('test')
    run(0.6)
    panel._on_click('PID:H')
    assert pid.state.value == 'OFF'
    assert panel.message.isVisible() and 'EMERGENCY' in panel.message.text()
    a.core.reset_estop()
    a.tabs['optimizer'].widgets['Jet'].settings.child('show_in_alignment').setValue(False)
    run(0.4)
    assert list(panel.cards) == ['PID:H']


def test_06_scan_tab(ext):
    e, win, model, data = ext
    a = e.app
    scan_tab = a.tabs['scan']
    scan_tab.load_from_preset({'scan': {'role': 'Sim_JET_X', 'start': -1.0, 'stop': 1.0, 'step': 1.0, 'dwell': 0.0,
                                        'samples': 1, 'channels': ['XUV_flux'], 'relative': True,
                                        'return_to_start': True}})
    a.tab_widget.setCurrentWidget(scan_tab)
    scan_tab._on_start()
    t0 = time.time()
    while time.time() - t0 < 30 and a.core.get('SCAN').state.value != 'OFF':
        run(0.2)
    scan = a.core.get('SCAN')
    assert scan.completed and len(scan.results) == 3


def test_07_recording_and_presets(ext):
    e, win, model, data = ext
    a = e.app
    a.toggle_recording(True)
    run(1.5)
    a.core.set_ml_state('ML_ON')
    a.core.add_note('essai UI')
    path = a.recorder.current_path
    a.toggle_recording(False)
    assert path is not None and path.exists()
    from pymodaq_plugins_lasercontrol.recording import load_session
    s = load_session(path)
    assert any(ev['message'] == 'essai UI' for ev in s.events)
    assert s.attrs['machine'] == a.machine
    a.save_preset('UI test preset')
    assert a.store.exists('UI test preset')
    p = a.store.load('UI test preset')
    assert p['pids'][0]['id'] == 'H'
    assert p['safety']['actuators']['Sim_OC_H']['max_step'] == 1.0
    assert p['alignment']['axes'][0]['h']['actuator'] == 'Sim_OC_H'
    ok, msg = a.apply_preset(p)
    assert ok, msg
    assert a.core.get('PID:H').state.value == 'OFF'       # S9: loading never starts anything


def test_08_estop_and_close(ext):
    e, win, model, data = ext
    a = e.app
    ok, msg = a.start_pid(a.tabs['pid'].configs()[0])
    assert ok, msg
    run(1.0)
    a.emergency_stop('test')
    assert a.core.estop and a.core.get('PID:H').state.value == 'OFF'
    ok, _ = a.start_pid(a.tabs['pid'].configs()[0])
    assert not ok
    a.core.reset_estop()
    win.close()
    run(0.5)
    assert a._closing
    assert (data / 'session_test.json').exists()
