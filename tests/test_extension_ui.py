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
    assert set(a.tabs) == {'overview', 'alignment', 'xy', 'pid', 'optimizer', 'scan', 'recording', 'presets', 'config',
                           'log'}
    # the journal is a tab: its title counts the warnings not seen yet, cleared when it is shown
    from pymodaq_plugins_lasercontrol.core.events import EventKind, Level
    log_index = a.tab_widget.indexOf(a.tabs['log'])
    a.tab_widget.setCurrentWidget(a.tabs['overview'])
    run(0.4)
    a.tabs['log'].mark_seen()                        # startup warnings (no roles yet)
    a.core.events.publish(EventKind.INFO, 'TEST', 'something odd', Level.WARNING)
    run(0.5)
    assert '(1 ⚠)' in a.tab_widget.tabText(log_index)
    a.tab_widget.setCurrentIndex(log_index)
    run(0.3)
    assert '⚠' not in a.tab_widget.tabText(log_index)
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
    pid_tab._add_widget({'id': 'H', 'label': 'Centroid H', 'in_stabilization': True, 'signal': 'Cam1_x',
                         'motors': {'Sim_OC_H': 1.0},
                         'settings': {'setpoint': 0.0, 'kp': 0.5, 'ki': 0.05, 'max_step': 1.0, 'period_s': 0.2}})
    a.tab_widget.setCurrentWidget(a.tabs['overview'])
    run(0.5)
    assert 'Centroid H' in a.tabs['overview'].summary.text()
    a.activate_stabilization()
    pid = a.core.get('PID:H')
    assert pid is not None and pid.state.value == 'ACTIVE', (pid.state, pid.reason)
    run(6.0)
    assert pid.state.value == 'ACTIVE', pid.reason
    assert pid.iterations > 5
    assert abs(a.core.signals.value('SimB/Cam1/x')) < 2.0
    a.tab_widget.setCurrentWidget(pid_tab)
    run(0.5)
    w = pid_tab.widgets['H']
    assert 'measure' in w.values.text()
    # overview table: one row, state and RMS error filled, action button = Stop
    assert pid_tab.overview.rowCount() == 1
    assert pid_tab.overview.item(0, pid_tab.STATE_COL).text() == 'Active'
    assert pid_tab.overview.item(0, 5).text() not in ('', '—')
    assert 'Stop' in pid_tab.overview.cellWidget(0, pid_tab.ACTION_COL).text()
    assert 'frozen' not in w.stats.text()
    assert w._param('kp').value() == 0.5 and w._param('setpoint').value() == 0.0
    assert 'RMS error' in w.stats.text()
    # one loop sign: inverting it is applied live to the three terms
    w._on_invert()
    assert pid.settings.gains()[0] < 0 and pid.settings.sign_ki == '-' and pid.settings.sign_kd == '-'
    w._on_invert()
    assert pid.settings.gains()[0] > 0
    # only the used motors are listed; add / remove motors (on a stopped PID)
    assert list(w.motor_weights()) == ['Sim_OC_H']
    cfg = dict(w.get_config(), id='V', label='Centroid V')
    v = pid_tab._add_widget(cfg)
    v.combo_add.setCurrentText('Sim_OC_V')
    v._on_add_motor()
    assert v.get_config()['motors'] == {'Sim_OC_H': 1.0, 'Sim_OC_V': 1.0}
    v._on_remove_motor('Sim_OC_H')
    assert v.get_config()['motors'] == {'Sim_OC_V': 1.0}
    assert 'Sim_OC_H' in [v.combo_add.itemText(i) for i in range(v.combo_add.count())]
    pid_tab.select('PID:V')
    run(0.3)
    assert pid_tab.overview.rowCount() == 2 and pid_tab._current() is v
    pid_tab.widgets.pop('V')
    pid_tab._order.remove('V')
    pid_tab.stack.removeWidget(v)
    pid_tab.select('PID:H')


def test_03b_alignment_drives_the_pid_setpoint(ext):
    """A PID setpoint (SP:<id>) is an axis of the Alignment tab: the keyboard moves the setpoint, the PID
    keeps running (not a human action on a motor) and follows it."""
    e, win, model, data = ext
    a = e.app
    pid = a.core.get('PID:H')
    assert pid.state.value == 'ACTIVE'
    assert 'SP:H' in a.manual.actuator_roles()
    align = a.tabs['alignment']
    saved = align.get_config()
    cfg = align.get_config()
    cfg['axes'][0]['h'].update(actuator='SP:H', mode='auto', slow=0.5, fast=2.0, invert=False)
    align.load_config(cfg)
    a.tab_widget.setCurrentWidget(align)
    run(0.3)
    align.select_axis(0)
    from qtpy import QtGui
    align.handle_key_press(QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_D, QtCore.Qt.NoModifier, 'd'))
    run(0.45)                                               # held ~4 steps of 0.5 (one step / 100 ms)
    align.handle_key_release(QtGui.QKeyEvent(QtCore.QEvent.KeyRelease, QtCore.Qt.Key_D, QtCore.Qt.NoModifier, 'd'))
    run(0.3)
    sp = pid.settings.setpoint
    assert 1.0 <= sp <= 3.0, sp
    assert a.tabs['pid'].widgets['H']._param('setpoint').value() == pytest.approx(sp)
    assert pid.state.value == 'ACTIVE'                      # moving a setpoint does not stop the PID
    run(4.0)
    assert abs(a.core.signals.value('SimB/Cam1/x') - sp) < 1.0     # the PID followed the new setpoint
    a.manual.move_abs('SP:H', 0.0)
    run(0.3)
    assert pid.settings.setpoint == 0.0
    align.load_config(saved)


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
    a.tabs['pid'].widgets['H']._param('show_in_alignment').setValue(True)
    a.tabs['optimizer'].widgets['Jet'].settings.child('show_in_alignment').setValue(True)
    a.tab_widget.setCurrentWidget(a.tabs['alignment'])
    run(0.6)
    panel = a.quick_panel
    assert panel.isVisible()
    assert list(panel.cards) == ['PID:H', 'OPT:Jet']
    assert panel.cards['PID:H'].name.text() == 'Centroid H'
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


def test_06b_pid_calibration_from_the_tab(ext):
    """The PID tab collects a finished calibration, keeps it in the preset and applies the proposal."""
    from pymodaq_plugins_lasercontrol.engines import CalibrationAutomation
    from pymodaq_plugins_lasercontrol.presets import normalize
    e, win, model, data = ext
    a = e.app
    for x in list(a.core.automations.values()):
        if x.state.value != 'OFF':
            a.core.stop(x.id, 'test')
    pid_tab = a.tabs['pid']
    w = pid_tab._add_widget({'id': 'C', 'label': 'Cal', 'in_stabilization': False, 'signal': 'Cam1_x',
                             'motors': {'Sim_OC_H': 1.0},
                             'settings': {'setpoint': 0.0, 'kp': 0.01, 'period_s': 0.2}})
    a.tab_widget.setCurrentWidget(pid_tab)
    pid_tab.select('PID:C')
    run(0.3)
    assert 'No calibration' in w.cal_text.text() and not w.btn_apply_cal.isEnabled()
    cal = CalibrationAutomation('Cam1_x', ['Sim_OC_H'], cycles=1, samples=2, noise_samples=6, auto_id='CAL:C',
                                label='Calibration Cal')
    a.core.add_automation(cal)
    ok, msg = a.core.start(cal.id)
    assert ok, msg
    run(0.3)
    assert 'Stop calibration' in w.btn_calibrate.text() and not w.btn_apply_cal.isEnabled()
    t0 = time.time()
    while time.time() - t0 < 90 and cal.state.value != 'OFF':
        run(0.2)
    assert cal.completed, cal.reason
    run(0.5)
    assert 'Sim_OC_H' in w.cal_text.text() and w.btn_calibrate.text() == '🎯 Calibrate'
    stored = w.get_config()['calibration']['Sim_OC_H']
    assert stored['date'] and math.isfinite(stored['g'])
    assert normalize({'pids': [w.get_config()]})['pids'][0]['calibration']['Sim_OC_H'] == stored
    if stored['ok']:
        assert w.btn_apply_cal.isEnabled()
        w._on_apply_calibration()
        assert w._param('kp').value() == pytest.approx(0.5 / abs(stored['g']), abs=1e-4)      # the field keeps 4 decimals
        assert w._param('loop_sign').value() == ('+' if stored['g'] > 0 else '-')
    pid_tab.widgets.pop('C')
    pid_tab._order.remove('C')
    pid_tab.stack.removeWidget(w)
    w.deleteLater()
    pid_tab._rebuild_overview()
    a.core.remove_automation(cal.id)


def test_06c_decoupling_window(ext):
    """Decouple PIDs: refusals, measurement start / stop, then a finished measurement (known matrix) gives
    each PID the combination that moves only its own signal, with the loop settings of that combination."""
    from pymodaq_plugins_lasercontrol.engines import CalibrationAutomation, CalibrationResult
    e, win, model, data = ext
    a = e.app
    pid_tab = a.tabs['pid']
    wx = pid_tab._add_widget({'id': 'DX', 'label': 'Dec x', 'in_stabilization': False, 'signal': 'Cam1_x',
                              'motors': {'Sim_OC_H': 1.0}, 'settings': {'kp': 0.1, 'period_s': 0.2}})
    wy = pid_tab._add_widget({'id': 'DY', 'label': 'Dec y', 'in_stabilization': False, 'signal': 'Cam1_y',
                              'motors': {'Sim_OC_V': 1.0}, 'settings': {'kp': 0.1, 'period_s': 0.2}})
    ok, msg = a.start_decoupling([wx.get_config()])
    assert not ok and 'two PIDs' in msg
    ok, msg = a.start_decoupling([wx.get_config(), dict(wy.get_config(), signal='Cam1_x')])
    assert not ok and 'own signal' in msg
    pid_tab.open_decoupling()
    dlg = pid_tab.decoupling_dialog
    dlg.set_selected(['DX', 'DY'])
    run(0.4)
    assert dlg.btn_measure.isEnabled() and not dlg.btn_apply.isEnabled()
    dlg._on_measure()                                   # really starts, then stopped by the user
    run(0.5)
    assert a.decoupling().state.value == 'ACTIVE' and 'Stop measurement' in dlg.btn_measure.text()
    dlg._on_measure()
    run(0.5)
    assert a.decoupling().state.value == 'OFF' and 'stopped' in dlg.report.toPlainText()
    assert a.core.position('Sim_OC_H') is not None
    # a finished measurement with a known coupled matrix (x = H + 0.5 V, y = −0.2 H + V)
    J = {'Cam1_x': {'Sim_OC_H': 1.0, 'Sim_OC_V': 0.5}, 'Cam1_y': {'Sim_OC_H': -0.2, 'Sim_OC_V': 1.0}}
    a.core.remove_automation('DEC')
    done = CalibrationAutomation('Cam1_x', ['Sim_OC_H', 'Sim_OC_V'], extra_signals=['Cam1_y'], auto_id='DEC')
    done.results_by = {s: {m: CalibrationResult(g=g, stderr=0.01, noise_sigma=0.1, settle_s=0.2, ok=True)
                           for m, g in row.items()} for s, row in J.items()}
    done.completed = True
    a.core.add_automation(done)
    dlg._run = [('DX', 'Cam1_x'), ('DY', 'Cam1_y')]
    run(0.5)
    assert dlg.result is not None and dlg.result.ok, dlg.report.toPlainText()
    assert 'coupling now' in dlg.report.toPlainText() and dlg.btn_apply.isEnabled()
    dlg._on_apply()
    for w, sig, other in ((wx, 'Cam1_x', 'Cam1_y'), (wy, 'Cam1_y', 'Cam1_x')):
        weights = w.get_config()['motors']
        assert set(weights) == {'Sim_OC_H', 'Sim_OC_V'}
        resp = {s: sum(J[s][m] * v for m, v in weights.items()) for s in J}
        assert abs(resp[other]) < 1e-3 * abs(resp[sig])        # moves only its own signal
        assert w._param('kp').value() == pytest.approx(0.5 / resp[sig], abs=1e-4)
        assert w._param('loop_sign').value() == '+'
        assert set(w.get_config()['calibration']) == {'Sim_OC_H', 'Sim_OC_V'}
    dlg.close()
    a.core.remove_automation('DEC')
    for w in (wx, wy):
        pid_tab.widgets.pop(w.pid_id)
        pid_tab._order.remove(w.pid_id)
        pid_tab.stack.removeWidget(w)
        w.deleteLater()
    pid_tab._rebuild_overview()


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
