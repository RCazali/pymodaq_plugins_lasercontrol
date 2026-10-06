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
    assert set(a.tabs) == {'overview', 'alignment', 'xy', 'axes', 'pid', 'optimizer', 'routines', 'scan',
                           'recording', 'presets', 'config', 'log'}
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
    # one loop sign (parameter tree): applied live to the three terms
    w._param('loop_sign').setValue('-')
    assert pid.settings.gains()[0] < 0 and pid.settings.sign_ki == '-' and pid.settings.sign_kd == '-'
    w._param('loop_sign').setValue('+')
    assert pid.settings.gains()[0] > 0
    # while it runs, the motors and the signal cannot be changed
    assert not w.settings.child('motors').opts['enabled']
    # motors ticked in the tree (on a stopped PID): the weights follow
    assert list(w.motor_weights()) == ['Sim_OC_H']
    cfg = dict(w.get_config(), id='V', label='Centroid V')
    v = pid_tab._add_widget(cfg)
    v.settings.child('motors').setValue(['Sim_OC_H', 'Sim_OC_V'])
    run(0.2)
    assert v.get_config()['motors'] == ['Sim_OC_H', 'Sim_OC_V']
    assert v.get_config()['loops'][0]['weights'] == {'Sim_OC_H': 1.0, 'Sim_OC_V': 1.0}
    v.settings.child('motors').setValue(['Sim_OC_V'])
    run(0.2)
    assert v.get_config()['loops'][0]['weights'] == {'Sim_OC_V': 1.0}
    assert v.settings.child('signals', 'loop0', 'weights', 'Sim_OC_V').value() == 1.0
    pid_tab.select('PID:V')
    run(0.3)
    assert pid_tab.overview.rowCount() == 2 and pid_tab._current() is v
    pid_tab.remove_widget('V')
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
    # "Confirm maximum" settings: loss shown in %, stored as a fraction; the mode is selectable
    w = opt_tab.widgets['Jet']
    assert w.get_config()['config']['confirm_max_loss'] == pytest.approx(0.01)
    w.settings.child('confirm_group', 'confirm_max_loss').setValue(2.0)
    assert w.get_config()['config']['confirm_max_loss'] == pytest.approx(0.02)
    assert 'Confirm maximum (extremum seeking)' in w.settings.child('run_mode').opts['limits']
    w.settings.child('confirm_group', 'confirm_max_loss').setValue(1.0)
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
    """Calibration of a one-signal PID: results kept in the preset, proposal applied from the tree."""
    from pymodaq_plugins_lasercontrol.engines import CalibrationAutomation
    from pymodaq_plugins_lasercontrol.presets import normalize
    e, win, model, data = ext
    a = e.app
    for x in list(a.core.automations.values()):
        if x.state.value != 'OFF':
            a.core.stop(x.id, 'test')
    pid_tab = a.tabs['pid']
    w = pid_tab._add_widget({'id': 'C', 'label': 'Cal', 'in_stabilization': False, 'signal': 'Cam1_x',
                             'motors': {'Sim_OC_H': 1.0}, 'settings': {'setpoint': 0.0, 'kp': 0.01, 'period_s': 0.2}})
    a.tab_widget.setCurrentWidget(pid_tab)
    pid_tab.select('PID:C')
    run(0.3)
    assert 'No measurement' in w.settings.child('calib', 'result').value()
    assert not w.settings.child('calib', 'max_coupling').opts['visible']          # one signal: no decoupling
    cal = CalibrationAutomation('Cam1_x', ['Sim_OC_H'], cycles=1, samples=2, noise_samples=6, auto_id='CAL:C',
                                label='Calibration Cal')
    a.core.add_automation(cal)
    ok, msg = a.core.start(cal.id)
    assert ok, msg
    run(0.3)
    assert 'Stop measurement' in w.settings.child('calib', 'measure').opts['title']
    t0 = time.time()
    while time.time() - t0 < 90 and cal.state.value != 'OFF':
        run(0.2)
    assert cal.completed, cal.reason
    run(0.5)
    assert 'Sim_OC_H' in w.settings.child('calib', 'result').value()
    assert w.settings.child('calib', 'measure').opts['title'] == '🎯 Measure'
    stored = w.get_config()['calibration']['Cam1_x']['Sim_OC_H']
    assert stored['date'] and math.isfinite(stored['g'])
    assert normalize({'pids': [w.get_config()]})['pids'][0]['calibration']['Cam1_x']['Sim_OC_H'] == stored
    if stored['ok']:
        w._on_apply_calibration()
        assert w._param('kp').value() == pytest.approx(0.5 / abs(stored['g']), abs=1e-4)
        assert w._param('loop_sign').value() == ('+' if stored['g'] > 0 else '-')
    pid_tab.remove_widget('C')
    a.core.remove_automation(cal.id)


def test_06c_coupled_pid_decoupling_in_its_settings(ext):
    """A PID with two signals: measure (start / stop), then Apply gives each signal the combination of motors
    that moves only that signal; it runs as one automation, one order per motor."""
    from pymodaq_plugins_lasercontrol.engines import CalibrationAutomation, CalibrationResult
    e, win, model, data = ext
    a = e.app
    pid_tab = a.tabs['pid']
    w = pid_tab._add_widget({'id': 'DXY', 'label': 'Dec', 'in_stabilization': False,
                             'motors': ['Sim_OC_H', 'Sim_OC_V'], 'settings': {'period_s': 0.2},
                             'loops': [{'signal': 'Cam1_x', 'kp': 0.1, 'weights': {'Sim_OC_H': 1.0}},
                                       {'signal': 'Cam1_y', 'kp': 0.1, 'weights': {'Sim_OC_V': 1.0}}]})
    pid_tab.select('PID:DXY')
    a.tab_widget.setCurrentWidget(pid_tab)
    run(0.4)
    assert w.settings.child('calib', 'max_coupling').opts['visible']
    assert w.settings.child('calib').opts['title'] == 'Calibration & decoupling'
    w._on_calibrate()                                   # really starts, then stopped by the user
    run(0.5)
    assert a.calibration('DXY').state.value == 'ACTIVE'
    assert a.calibration('DXY').signals == ['Cam1_x', 'Cam1_y']
    w._on_calibrate()
    run(0.5)
    assert a.calibration('DXY').state.value == 'OFF' and 'stopped' in w.settings.child('calib', 'status').value()
    # a finished measurement with a known coupled matrix (x = H + 0.5 V, y = −0.2 H + V)
    J = {'Cam1_x': {'Sim_OC_H': 1.0, 'Sim_OC_V': 0.5}, 'Cam1_y': {'Sim_OC_H': -0.2, 'Sim_OC_V': 1.0}}
    a.core.remove_automation('CAL:DXY')
    done = CalibrationAutomation('Cam1_x', ['Sim_OC_H', 'Sim_OC_V'], extra_signals=['Cam1_y'], auto_id='CAL:DXY')
    done.results_by = {s: {m: CalibrationResult(g=g, stderr=0.01, noise_sigma=0.1, settle_s=0.2, ok=True)
                           for m, g in row.items()} for s, row in J.items()}
    done.completed = True
    a.core.add_automation(done)
    run(0.5)
    assert 'Proposed weights' in w.settings.child('calib', 'result').value()
    w._on_apply_calibration()
    cfg = w.get_config()
    for k, (sig, other) in enumerate((('Cam1_x', 'Cam1_y'), ('Cam1_y', 'Cam1_x'))):
        weights = cfg['loops'][k]['weights']
        resp = {s: sum(J[s][m] * v for m, v in weights.items()) for s in J}
        assert abs(resp[other]) < 1e-3 * abs(resp[sig])        # moves only its own signal
        assert cfg['loops'][k]['kp'] == pytest.approx(0.5 / resp[sig], abs=1e-4)
        assert cfg['loops'][k]['sign'] == '+'
        assert w.loop_param(k, 'kp').value() == pytest.approx(cfg['loops'][k]['kp'])
    for k in range(2):
        w.loop_param(k, 'setpoint').setValue(a.core.signals.value(a.core.roles.channel_for(cfg['loops'][k]['signal'])))
        w.loop_param(k, 'deadband').setValue(0.5)
    assert 'SP:DXY/2' in a.manual.actuator_roles()                 # a setpoint axis per signal
    pid_tab._on_row_action('DXY')
    run(2.0)
    pid = a.core.get('PID:DXY')
    assert pid.state.value == 'ACTIVE', pid.reason
    assert all(l.iterations > 0 for l in pid.loops)
    assert '/' in pid_tab.overview.item(pid_tab._order.index('DXY'), 4).text()      # two errors
    a.core.stop(pid.id, 'test')
    pid_tab.remove_widget('DXY')
    a.core.remove_automation('CAL:DXY')


def test_06d_routines_tab_and_alignment_button(ext):
    """A routine programmed in the Routines tab (parameter tree), started from its Alignment button: start a
    PID, wait until it converges, pause it, resume it; the running step is marked; editing is locked."""
    from pymodaq_plugins_lasercontrol.engines.routine import Condition, Routine, Step
    e, win, model, data = ext
    a = e.app
    for x in list(a.core.automations.values()):
        if x.state.value != 'OFF':
            a.core.stop(x.id, 'test')
    tab = a.tabs['routines']
    w = a.tabs['pid'].widgets['H']
    w._param('setpoint').setValue(a.core.signals.value('SimB/Cam1/x'))
    w._param('deadband').setValue(0.3)
    r = Routine('UI', 'UI routine', steps=[
        Step('start', 'PID:H', until=[Condition('locked', 'PID:H', value=1.0, hold_s=1.0)], timeout_s=30),
        Step('pause', 'PID:H'),
        Step('none', until=[Condition('delay', value=1.0)]),
        Step('resume', 'PID:H', until=[Condition('locked', 'PID:H', value=1.0, hold_s=0.5, then='end')],
             timeout_s=30)])
    tab.load_from_preset({'routines': [r.to_dict()]})                # an old step routine: converted
    a.tab_widget.setCurrentWidget(tab)
    run(0.4)
    p = tab.routines[0]
    assert [n.action for n in p.main] == ['start', 'pause', 'none', 'resume']
    assert set(tab.view.items_) == {n.uid for n in p.main}          # one block per step in the diagram
    assert not tab.problems.isVisible()
    tab.select_block(p.main[0].uid)
    run(0.2)
    assert 'PID:H |err| ≤ 1 for 1 s' in tab.settings.child('block', 'until', 'c0').opts['title']
    tab.select_block(p.main[2].uid)
    run(0.2)
    tab.settings.child('block', 'until', 'c0', 'value').setValue(1.5)  # edit a condition in the tree
    assert p.main[2].until[0].value == 1.5
    tab.settings.child('block', 'until', 'c0', 'value').setValue(1.0)
    tab._on_add('step')                                              # after the selected step
    run(0.2)
    assert len(p.main) == 5 and tab.selected == p.main[3].uid
    tab._on_move(1)
    assert tab.selected == p.main[4].uid
    tab._on_delete()
    run(0.2)
    assert len(p.main) == 4 and p.main[3].action == 'resume'
    tab._on_add('every')                                              # a background block, then removed
    assert len(p.background) == 1 and tab.selected == p.background[0].uid
    tab._on_delete()
    assert not p.background
    a.tab_widget.setCurrentWidget(a.tabs['alignment'])
    run(0.6)
    panel = a.quick_panel
    assert 'RTN:UI' in panel.cards and panel.cards['RTN:UI'].band.text() == 'SEQ'
    panel._on_click('RTN:UI')
    rt = a.routine('UI')
    assert rt is not None and rt.state.value == 'ACTIVE', rt.reason if rt else ''
    pid = a.core.get('PID:H')
    paused = False
    t0 = time.time()
    while time.time() - t0 < 60 and rt.state.value != 'OFF':
        run(0.2)
        paused = paused or (pid.state.value == 'PAUSED' and pid.pause_kind == 'manual')
        if rt.state.value == 'ACTIVE':
            assert panel.cards['RTN:UI'].line.text().startswith(('▶', '⚠'))
    assert rt.outcome == 'done', (rt.reason, list(rt.history)[-4:])
    assert paused and pid.state.value == 'ACTIVE'                   # left running after a successful end
    run(0.4)
    assert 'Done' in panel.cards['RTN:UI'].line.text()
    a.tab_widget.setCurrentWidget(tab)
    run(0.4)
    assert 'Finished' in tab.status.text() and 'Pause PID:H' in tab.history.toPlainText()
    a.core.stop('PID:H', 'test')



def test_06e_settings_trees(ext):
    """Settings are edited in their tabs (parameter trees): Axes tab, signals of a PID, routine steps; a
    running routine cannot be edited; a second-signal setpoint is an Alignment axis."""
    from pymodaq_plugins_lasercontrol.engines.routine import Condition, Routine, Step
    e, win, model, data = ext
    a = e.app
    align = a.tabs['alignment']
    saved = align.get_config()
    # --- Axes tab: the tree edits the axis of the Alignment tab (nothing editable left in Alignment)
    a.tab_widget.setCurrentWidget(a.tabs['axes'])
    align.axis_list.setCurrentRow(0)
    run(0.3)
    h = align.axis_params.child('h')
    h.child('on').setValue(True)
    h.child('actuator').setValue('Sim_OC_H')
    h.child('rel_step').setValue(2.5)
    h.child('slow').setValue(0.25)
    align.axis_params.child('axis', 'name').setValue('Test axis')
    run(0.8)                                              # applied after a short delay
    ax = align.get_config()['axes'][0]
    assert ax['name'] == 'Test axis' and ax['h']['actuator'] == 'Sim_OC_H'
    assert ax['h']['rel_step'] == 2.5 and ax['h']['slow'] == 0.25
    a.tab_widget.setCurrentWidget(align)
    align.select_axis(0)
    run(0.3)
    spins = [w for w in align.detail.findChildren(QtWidgets.QDoubleSpinBox)]
    assert len(spins) == 1 or all(s.toolTip() == 'Absolute target' for s in spins)   # only "go to" is typed
    labels = [l.text() for l in align.detail.findChildren(QtWidgets.QLabel)]
    assert '2.5' in labels                                                           # the − / + step, read-only
    align.chk_hide.setChecked(True)
    assert align.get_config()['hide_unassigned'] is True
    align.chk_hide.setChecked(False)
    # --- PID: add then remove a second signal; its setpoint is the axis SP:<id>/2
    pid_tab = a.tabs['pid']
    w = pid_tab._add_widget({'id': 'T2', 'label': 'Two signals', 'in_stabilization': False, 'signal': 'Cam1_x',
                             'motors': {'Sim_OC_H': 1.0}})
    a.tab_widget.setCurrentWidget(pid_tab)
    pid_tab.select('PID:T2')
    run(0.3)
    w._on_add_loop()
    run(0.2)
    assert len(w.loops()) == 2 and w.settings.child('signals').opts['title'] == 'Signals (coupled PID)'
    w.loop_param(1, 'signal').setValue('Cam1_y')
    run(0.2)
    assert w.get_config()['loops'][1]['signal'] == 'Cam1_y'
    assert w.get_config()['loops'][1]['weights'] == {'Sim_OC_H': 0.0}          # to be set by the decoupling
    assert 'SP:T2/2' in a.manual.actuator_roles()
    assert a.manual.move_abs('SP:T2/2', 3.0)
    run(0.3)
    assert w.get_config()['loops'][1]['setpoint'] == 3.0 and w.loop_param(1, 'setpoint').value() == 3.0
    ok, msg = a.start_pid(w.get_config())
    assert not ok and 'no motor' in msg                                          # Cam1_y has no weight yet
    w._on_remove_loop(1)
    run(0.2)
    assert len(w.loops()) == 1 and 'SP:T2/2' not in a.manual.actuator_roles()
    pid_tab.remove_widget('T2')
    # --- Routines: change an action, add / remove a condition, locked while running
    tab = a.tabs['routines']
    r = Routine('E', 'Edit test', steps=[Step('none', until=[Condition('delay', value=30.0)])])
    tab.load_from_preset({'routines': [r.to_dict()]})
    a.tab_widget.setCurrentWidget(tab)
    run(0.3)
    p = tab.routines[0]
    node = p.main[0]
    tab.select_block(node.uid)
    run(0.2)
    tab.settings.child('block', 'action').setValue('start')
    run(0.2)
    assert node.action == 'start' and tab.settings.child('block', 'target').value() == ''   # '— choose —'
    tab.settings.child('block', 'target').setValue('PID:H')
    run(0.2)
    assert node.target == 'PID:H'
    tab.settings.child('block', 'until', 'add').activate()
    run(0.2)
    assert len(node.until) == 2
    tab.settings.child('block', 'until', 'c1', 'kind').setValue('signal')
    run(0.2)
    c1 = tab.settings.child('block', 'until', 'c1')
    assert c1.child('op') is not None and c1.child('hold_s') is not None
    c1.child('remove').activate()
    run(0.2)
    assert len(node.until) == 1
    # a locked condition with one tolerance per PID
    tab.settings.child('block', 'until', 'c0', 'kind').setValue('locked')
    run(0.2)
    tab.settings.child('block', 'until', 'c0', 'tolerances', 'PID:H').setValue(0.4)
    assert node.until[0].pids() == {'PID:H': 0.4}
    tab.settings.child('block', 'until', 'c0', 'kind').setValue('delay')
    run(0.2)
    tab.settings.child('block', 'until', 'c0', 'value').setValue(30.0)
    tab.settings.child('block', 'action').setValue('none')                       # wait only: 30 s
    run(0.2)
    # a FOR EACH with a ramp inside: positions per PID, the ramp takes the position of the loop
    tab.selected = None
    tab._on_add('foreach')
    fe = p.main[-1]
    tab.settings.child('block', 'pids').setValue(['PID:H'])
    run(0.3)
    tab.settings.child('block', 'positions', 'add').activate()
    run(0.2)
    assert len(fe.positions) == 2 and set(fe.positions[1].setpoints) == {'PID:H'}
    name = [k for k, v in tab._pos_names.items() if v == ('PID:H', 0)][-1]
    tab.settings.child('block', 'positions', 'pos1', name).setValue(2.5)
    assert fe.positions[1].setpoints['PID:H'] == [2.5]
    tab.select_block(fe.body[0].uid)
    run(0.2)
    assert tab.settings.child('block', 'target_info') is not None
    tab.select_block(fe.uid)
    tab._on_delete()
    assert len(p.main) == 1 and tab.view.items_.keys() == {node.uid}
    tab._on_run()
    run(0.5)
    assert a.routine('E').state.value == 'ACTIVE'
    assert tab.settings.child('routine', 'label').opts['readonly']                # locked while it runs
    assert not tab.add_buttons['step'].isEnabled()
    assert tab.view._run[0] == node.uid and tab.view.items_[node.uid].marker.text() == '▶'
    tab._on_stop()
    run(0.5)
    assert not tab.settings.child('routine', 'label').opts['readonly']
    a.core.remove_automation('RTN:E')
    tab.load_from_preset({'routines': []})
    align.load_config(saved)


def test_06f_operator_question_in_alignment(ext):
    """A routine waiting for the operator (e.g. HA maximum not confirmed): a banner in Alignment with
    Continue / Check again / Stop; nothing goes on before the answer."""
    from pymodaq_plugins_lasercontrol.engines.program import Cond, Node, Program
    e, win, model, data = ext
    a = e.app
    prog = Program('Q', 'Question test', main=[
        Node(action='note', text='before'), Node(until=[Cond('delay', value=0.5)]),
        Node(action='note', text='after')])
    ok, msg = a.start_routine(prog.to_dict())
    assert ok, msg
    rt = a.routine('Q')
    a.tab_widget.setCurrentWidget(a.tabs['alignment'])
    run(0.4)
    panel = a.quick_panel
    assert not panel.banner.isVisible()
    rt.ask(a.core, rt.main, 'HA maximum not confirmed after 5 probes')       # as a "confirm" step would
    run(0.5)
    assert panel.banner.isVisible() and 'HA maximum not confirmed' in panel.banner_text.text()
    assert panel.banner_text.text().startswith('⚠')
    node = rt.main.node
    run(1.0)
    assert rt.main.node is node and rt.state.value == 'ACTIVE'               # waits for the answer
    panel.banner_buttons['check'].click()                                     # the step runs again
    run(0.5)
    assert rt.question is None and not panel.banner.isVisible()
    rt.ask(a.core, rt.main, 'still not confirmed')
    run(0.4)
    panel.banner_buttons['continue'].click()
    run(1.5)
    assert rt.outcome == 'done', (rt.reason, list(rt.history)[-4:])
    ok, msg = a.start_routine(prog.to_dict())
    assert ok, msg
    rt = a.routine('Q')
    run(0.3)
    rt.ask(a.core, rt.main, 'third question')
    run(0.4)
    panel.banner_buttons['stop'].click()
    run(0.4)
    assert rt.outcome == 'stopped' and rt.state.value == 'OFF' and not panel.banner.isVisible()
    a.core.remove_automation('RTN:Q')


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
    t0 = time.time()
    ok, msg = a.apply_preset(p)
    assert time.time() - t0 < 1.0                          # loading must not freeze the interface
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
