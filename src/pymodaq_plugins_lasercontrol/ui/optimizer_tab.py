"""Optimization tab: several named optimizations (e.g. "Optim HA", "Optim OC"), each with the
historical settings and modes (behind the pluggable interface T4.5), periodic passes, 'Optimize now',
'Full optimization', passive live monitor (T4.3) and cost plot. Each one can have a button in the
Alignment tab ('Button in Alignment').
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.states import AutoState, ENGAGED_STATES
from ..core.roles import sanitize_role_name
from ..engines.optimizer import DEFAULT_OPT_CONFIG, RUN_MODES
from ..presets.schema import OPT_BUTTON_ACTION_LABELS, default_optimizer
from .widgets import StateBadge, ask_yes_no, fit_table_height, fmt, info, reset_rows, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

MONITOR_COLORS = ['#00c2ff', '#ff9100', '#7cff00', '#ff4dd2', '#ffd23f', '#a276ff']


def opt_params() -> list:
    d = DEFAULT_OPT_CONFIG
    return [
        {'title': 'Display name:', 'name': 'label', 'type': 'str', 'value': '',
         'tip': 'Short name, also used for its button in the jog window (e.g. Optim HA)'},
        {'title': 'Button in the jog window:', 'name': 'show_in_alignment', 'type': 'bool', 'value': False,
         'tip': 'Shows a button on top of the jog window: one click starts, another one stops '
                '(no confirmation)'},
        {'title': 'Button action:', 'name': 'button_action', 'type': 'list',
         'limits': {v: k for k, v in OPT_BUTTON_ACTION_LABELS.items()}, 'value': 'single',
         'tip': 'one pass: stops by itself at the end · periodic: one pass every N min until stopped · '
                'full: aggressive search in one pass'},
        {'title': 'Interval between passes (min):', 'name': 'interval_min', 'type': 'float', 'value': d['interval_min'],
         'min': 0.05},
        {'title': 'Dwell after move (s):', 'name': 'dwell', 'type': 'float', 'value': d['dwell'],
         'min': 0.0},
        {'title': 'Max iterations / pass:', 'name': 'max_iterations', 'type': 'int', 'value': d['max_iterations'],
         'min': 1},
        {'title': 'Mode:', 'name': 'run_mode', 'type': 'list', 'limits': RUN_MODES, 'value': d['run_mode']},
        {'title': 'Gradient / Newton-lite', 'name': 'gradient_group', 'type': 'group', 'children': [
            {'title': 'Samples per point:', 'name': 'gradient_samples', 'type': 'int',
             'value': d['gradient_samples'], 'min': 1},
            {'title': 'Damping:', 'name': 'damping_factor', 'type': 'float', 'value': d['damping_factor'],
             'min': 0.0},
        ]},
        {'title': 'Stop criteria', 'name': 'stop_group', 'type': 'group', 'expanded': False, 'children': [
            {'title': 'Patience (iterations):', 'name': 'stall_patience', 'type': 'int',
             'value': d['stall_patience'], 'min': 1},
            {'title': 'Deadband scale:', 'name': 'deadband_scale', 'type': 'float',
             'value': d['deadband_scale'], 'min': 0.001},
            {'title': 'Safety factor:', 'name': 'safety_factor', 'type': 'float', 'value': d['safety_factor'],
             'min': 0.1},
            {'title': 'Max consecutive back-offs:', 'name': 'max_backoffs', 'type': 'int', 'value': d['max_backoffs'],
             'min': 1},
        ]},
        {'title': 'Full optimization', 'name': 'full_opt_group', 'type': 'group', 'expanded': False, 'children': [
            {'title': 'Iterations:', 'name': 'full_opt_iterations', 'type': 'int', 'value': d['full_opt_iterations'],
             'min': 1},
            {'title': 'Patience:', 'name': 'full_opt_stall_patience', 'type': 'int',
             'value': d['full_opt_stall_patience'], 'min': 1},
            {'title': 'Deadband scale:', 'name': 'full_opt_deadband_scale', 'type': 'float',
             'value': d['full_opt_deadband_scale'], 'min': 0.0001},
        ]},
        {'title': 'Confirm maximum (extremum seeking)', 'name': 'confirm_group', 'type': 'group', 'expanded': False,
         'children': [
             {'title': 'Max tries per motor:', 'name': 'confirm_tries', 'type': 'int', 'value': d['confirm_tries'],
              'min': 1, 'tip': 'probes (+ − − + around the centre) before giving up: then a routine asks the operator'},
             {'title': 'Tolerated loss (%):', 'name': 'confirm_max_loss', 'type': 'float',
              'value': 100 * d['confirm_max_loss'], 'min': 0.0, 'max': 50.0,
              'tip': '"at the maximum" = the estimated signal lost by not being exactly at the top is below this '
                     '(the top itself moves: a strict "gradient = 0" would never be reached)'},
             {'title': 'Pattern repeats:', 'name': 'confirm_cycles', 'type': 'int', 'value': d['confirm_cycles'],
              'min': 1, 'max': 6, 'tip': 'more = more robust to slow changes of the signal, longer'},
             {'title': 'Frames per plateau:', 'name': 'confirm_samples', 'type': 'int', 'value': d['confirm_samples'],
              'min': 1},
             {'title': 'Significance (σ):', 'name': 'confirm_z', 'type': 'float', 'value': d['confirm_z'], 'min': 1.0,
              'tip': 'a gradient is significant above this many standard errors'},
         ]},
        {'title': 'Measurement', 'name': 'measure_group', 'type': 'group', 'expanded': False, 'children': [
            {'title': 'Frames for the noise:', 'name': 'noise_samples', 'type': 'int', 'value': d['noise_samples'],
             'min': 2},
            {'title': 'Frames per point:', 'name': 'probe_samples', 'type': 'int', 'value': d['probe_samples'],
             'min': 1},
        ]},
    ]


FLAT_KEYS = {  # config key -> parameter path
    'interval_min': ('interval_min',), 'dwell': ('dwell',), 'max_iterations': ('max_iterations',),
    'run_mode': ('run_mode',), 'gradient_samples': ('gradient_group', 'gradient_samples'),
    'damping_factor': ('gradient_group', 'damping_factor'), 'stall_patience': ('stop_group', 'stall_patience'),
    'deadband_scale': ('stop_group', 'deadband_scale'), 'safety_factor': ('stop_group', 'safety_factor'),
    'max_backoffs': ('stop_group', 'max_backoffs'), 'full_opt_iterations': ('full_opt_group', 'full_opt_iterations'),
    'full_opt_stall_patience': ('full_opt_group', 'full_opt_stall_patience'),
    'full_opt_deadband_scale': ('full_opt_group', 'full_opt_deadband_scale'),
    'noise_samples': ('measure_group', 'noise_samples'), 'probe_samples': ('measure_group', 'probe_samples'),
    'confirm_tries': ('confirm_group', 'confirm_tries'), 'confirm_max_loss': ('confirm_group', 'confirm_max_loss'),
    'confirm_cycles': ('confirm_group', 'confirm_cycles'), 'confirm_samples': ('confirm_group', 'confirm_samples'),
    'confirm_z': ('confirm_group', 'confirm_z'),
}
#: shown in % in the tree, stored as a fraction
PERCENT_KEYS = ('confirm_max_loss',)

MOTOR_COLS = ['Motor (role)', 'Max step', 'Probe step', 'Order']
SIGNAL_COLS = ['Active', 'Signal (role)', 'Mode', 'Weight', 'Exponent p', 'Target', 'Auto']


class SingleOptimizerWidget(QtWidgets.QWidget):
    """One named optimisation (automation ``OPT:<id>``)."""

    def showEvent(self, e):
        super().showEvent(e)
        if not self._tree_filled:
            self._tree_filled = True
            self.tree.setParameters(self.settings, showTop=False)

    def __init__(self, app: 'LaserControlApp', cfg: dict, parent=None):
        super().__init__(parent)
        self.app = app
        self.opt_id = cfg['id']
        self._loading = False
        self._monitor_curves: Dict[str, pg.PlotDataItem] = {}
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)

        left_w = QtWidgets.QWidget()
        left = QtWidgets.QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        self.settings = Parameter.create(name='optimizer', type='group', children=opt_params())
        self.tree = ParameterTree()
        self._tree_filled = False                 # filled when first shown (fast preset loading)
        self.tree.setMinimumHeight(340)
        self.settings.sigTreeStateChanged.connect(self._on_changed)
        left.addWidget(QtWidgets.QLabel('<b>1. Settings</b>'))
        left.addWidget(self.tree)
        left.addWidget(QtWidgets.QLabel('<b>2. Motors</b> <i>("Order": sequential mode only)</i>'))
        self.table_motors = QtWidgets.QTableWidget(0, len(MOTOR_COLS))
        self.table_motors.setHorizontalHeaderLabels(MOTOR_COLS)
        self.table_motors.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table_motors.verticalHeader().setVisible(False)
        self.table_motors.setMinimumHeight(110)
        self.table_motors.itemChanged.connect(self._on_changed)
        left.addWidget(self.table_motors)
        left.addWidget(QtWidgets.QLabel('<b>3. Objective signals</b>'))
        self.table_signals = QtWidgets.QTableWidget(0, len(SIGNAL_COLS))
        self.table_signals.setHorizontalHeaderLabels(SIGNAL_COLS)
        sh = self.table_signals.horizontalHeader()
        sh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        sh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table_signals.verticalHeader().setVisible(False)
        self.table_signals.setMinimumHeight(130)
        self.table_signals.itemChanged.connect(self._on_changed)
        left.addWidget(self.table_signals)
        self.btn_capture = styled_button('📍 Targets = current values', None,
                                         'For each ticked signal in "Target Point" mode: target = last '
                                         'received value, "Auto" unticked (fixed target).')
        self.btn_capture.clicked.connect(self._on_capture)
        left.addWidget(self.btn_capture)
        btns = QtWidgets.QGridLayout()
        self.btn_start = styled_button('▶ Periodic optimizer', '#27ae60')
        self.btn_stop = styled_button('⏹ Stop', '#7f8c8d')
        self.btn_now = styled_button('⚡ Optimize now', '#2980b9',
                                     'One pass right away (wakes the periodic optimizer up if it is running)')
        self.btn_full = styled_button('🚀 Full optimization', '#b71c1c',
                                      'Aggressive search in one pass (probe at max step). The periodic '
                                      'optimizer must be stopped.')
        self.btn_resume = styled_button('⏯ Resume', '#2980b9', 'Manual resume after a safety hold')
        btns.addWidget(self.btn_start, 0, 0)
        btns.addWidget(self.btn_stop, 0, 1)
        btns.addWidget(self.btn_now, 1, 0)
        btns.addWidget(self.btn_resume, 1, 1)
        btns.addWidget(self.btn_full, 2, 0, 1, 2)
        left.addLayout(btns)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(lambda: self.app.core.stop(self.auto_id, 'manual stop'))
        self.btn_now.clicked.connect(self._on_now)
        self.btn_full.clicked.connect(self._on_full)
        self.btn_resume.clicked.connect(self._on_resume)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_w)
        main.addWidget(scroll, 1)

        right = QtWidgets.QVBoxLayout()
        top = QtWidgets.QHBoxLayout()
        self.state = StateBadge('', font_pt=11)
        top.addWidget(self.state)
        self.countdown = QtWidgets.QLabel('--:--')
        f = self.countdown.font()
        f.setPointSize(16)
        f.setBold(True)
        self.countdown.setFont(f)
        top.addWidget(QtWidgets.QLabel('⏱ next pass:'))
        top.addWidget(self.countdown)
        top.addStretch()
        right.addLayout(top)
        self.status = QtWidgets.QLabel('')
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        mon = QtWidgets.QGroupBox('Passive monitor of the objective signals (drift)')
        ml = QtWidgets.QVBoxLayout(mon)
        self.monitor = pg.PlotWidget()
        self.monitor.setFocusPolicy(QtCore.Qt.NoFocus)
        self.monitor.showGrid(x=True, y=True, alpha=0.25)
        self.monitor.setLabel('bottom', 'time (s)')
        self.monitor_legend = self.monitor.addLegend()
        ml.addWidget(self.monitor)
        right.addWidget(mon, 2)
        cost = QtWidgets.QGroupBox('Convergence (cost per iteration)')
        cl = QtWidgets.QVBoxLayout(cost)
        self.cost_plot = pg.PlotWidget()
        self.cost_plot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.cost_plot.showGrid(x=True, y=True, alpha=0.25)
        self.cost_plot.setLabel('bottom', 'iteration')
        self.cost_plot.setLabel('left', 'cost f(x)')
        self.cost_curve = self.cost_plot.plot(pen=pg.mkPen('#ff9100', width=2), symbol='o', symbolSize=5)
        cl.addWidget(self.cost_plot)
        right.addWidget(cost, 2)
        main.addLayout(right, 1)
        self.set_hardware()
        self.load_config(cfg)

    @property
    def auto_id(self) -> str:
        return f'OPT:{self.opt_id}'

    @property
    def automation(self):
        return self.app.optimizer(self.opt_id)

    def label(self) -> str:
        return self.settings.child('label').value() or self.opt_id

    # ------------------------------------------------------------------ tables
    def set_hardware(self, cfg: dict = None):
        cfg = cfg or self.get_config()
        self._loading = True
        try:
            motors = {m['role']: m for m in cfg.get('motors', [])}
            roles = sorted(self.app.core.roles.actuators)
            roles += [r for r in motors if r not in roles]
            reset_rows(self.table_motors, len(roles))
            for i, role in enumerate(roles):
                it = QtWidgets.QTableWidgetItem(role if self.app.core.roles.is_actuator(role) else f'⚠ {role}')
                it.setData(QtCore.Qt.UserRole, role)
                it.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
                it.setCheckState(QtCore.Qt.Checked if role in motors else QtCore.Qt.Unchecked)
                self.table_motors.setItem(i, 0, it)
                m = motors.get(role, {})
                for col, key, default, lo, hi in ((1, 'max_step', 0.1, 1e-6, 1e6), (2, 'probe_step', 0.02, 1e-6, 1e6)):
                    sp = QtWidgets.QDoubleSpinBox()
                    sp.setRange(lo, hi)
                    sp.setDecimals(5)
                    sp.setValue(float(m.get(key, default)))
                    sp.setFocusPolicy(QtCore.Qt.ClickFocus)
                    sp.valueChanged.connect(self._on_changed)
                    self.table_motors.setCellWidget(i, col, sp)
                sp = QtWidgets.QSpinBox()
                sp.setRange(0, 999)
                sp.setValue(int(m.get('order', i)))
                sp.setFocusPolicy(QtCore.Qt.ClickFocus)
                sp.valueChanged.connect(self._on_changed)
                self.table_motors.setCellWidget(i, 3, sp)
            sigs = {s['role']: s for s in cfg.get('signals', [])}
            sroles = sorted(self.app.core.roles.signals)
            sroles += [r for r in sigs if r not in sroles]
            reset_rows(self.table_signals, len(sroles))
            for i, role in enumerate(sroles):
                s = sigs.get(role, {})
                chk = QtWidgets.QTableWidgetItem('')
                chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
                chk.setCheckState(QtCore.Qt.Checked if role in sigs else QtCore.Qt.Unchecked)
                self.table_signals.setItem(i, 0, chk)
                name = QtWidgets.QTableWidgetItem(role if self.app.core.roles.is_signal(role) else f'⚠ {role}')
                name.setData(QtCore.Qt.UserRole, role)
                name.setFlags(QtCore.Qt.ItemIsEnabled)
                self.table_signals.setItem(i, 1, name)
                mode = QtWidgets.QComboBox()
                mode.addItems(['Maximize', 'Minimize', 'Target Point'])
                mode.setCurrentText(s.get('mode', 'Maximize'))
                mode.currentTextChanged.connect(self._on_changed)
                self.table_signals.setCellWidget(i, 2, mode)
                for col, key, default, lo, hi, dec in ((3, 'weight', 1.0, 0, 1000, 3), (4, 'p', 2.0, 1, 10, 2),
                                                        (5, 'target_value', 0.0, -1e12, 1e12, 6)):
                    sp = QtWidgets.QDoubleSpinBox()
                    sp.setRange(lo, hi)
                    sp.setDecimals(dec)
                    sp.setValue(float(s.get(key, default)))
                    sp.setFocusPolicy(QtCore.Qt.ClickFocus)
                    sp.valueChanged.connect(self._on_changed)
                    self.table_signals.setCellWidget(i, col, sp)
                auto = QtWidgets.QCheckBox()
                auto.setChecked(bool(s.get('auto_target', True)))
                auto.toggled.connect(self._on_changed)
                self.table_signals.setCellWidget(i, 6, auto)
        finally:
            self._loading = False
        # the left panel scrolls as a whole: the tables show all their rows (no nested scrolling)
        fit_table_height(self.table_motors)
        fit_table_height(self.table_signals)

    # ------------------------------------------------------------------ config
    def get_config(self) -> dict:
        """{'id', 'label', 'show_in_alignment', 'button_action', 'config', 'motors', 'signals'}"""
        cfg = {}
        for key, path in FLAT_KEYS.items():
            try:
                cfg[key] = self.settings.child(*path).value()
            except Exception:
                pass
        for key in PERCENT_KEYS:
            if key in cfg:
                cfg[key] = float(cfg[key]) / 100.0
        motors = []
        for r in range(self.table_motors.rowCount()):
            it = self.table_motors.item(r, 0)
            if it is not None and it.checkState() == QtCore.Qt.Checked:
                motors.append({'role': it.data(QtCore.Qt.UserRole),
                               'max_step': float(self.table_motors.cellWidget(r, 1).value()),
                               'probe_step': float(self.table_motors.cellWidget(r, 2).value()),
                               'order': int(self.table_motors.cellWidget(r, 3).value())})
        signals = []
        for r in range(self.table_signals.rowCount()):
            it = self.table_signals.item(r, 0)
            if it is not None and it.checkState() == QtCore.Qt.Checked:
                signals.append({'role': self.table_signals.item(r, 1).data(QtCore.Qt.UserRole),
                                'mode': self.table_signals.cellWidget(r, 2).currentText(),
                                'weight': float(self.table_signals.cellWidget(r, 3).value()),
                                'p': float(self.table_signals.cellWidget(r, 4).value()),
                                'target_value': float(self.table_signals.cellWidget(r, 5).value()),
                                'auto_target': bool(self.table_signals.cellWidget(r, 6).isChecked())})
        cfg['full_optimization'] = False
        return {'id': self.opt_id, 'label': self.settings.child('label').value(),
                'show_in_alignment': bool(self.settings.child('show_in_alignment').value()),
                'button_action': self.settings.child('button_action').value(),
                'config': cfg, 'motors': motors, 'signals': signals}

    def load_config(self, opt: dict):
        self._loading = True
        try:
            for key in ('label', 'show_in_alignment', 'button_action'):
                if opt.get(key) is not None:
                    try:
                        self.settings.child(key).setValue(opt[key])
                    except Exception:
                        pass
            for key, path in FLAT_KEYS.items():
                if key in opt.get('config', {}):
                    try:
                        v = opt['config'][key]
                        self.settings.child(*path).setValue(100.0 * float(v) if key in PERCENT_KEYS else v)
                    except Exception:
                        pass
        finally:
            self._loading = False
        self.set_hardware({'motors': opt.get('motors', []), 'signals': opt.get('signals', [])})

    def _on_changed(self, *args):
        if self._loading:
            return
        self.app.preset_dirty = True
        opt = self.automation
        if opt is not None and opt.state in ENGAGED_STATES:
            cfg = self.get_config()
            full = opt.config.get('full_optimization', False)
            opt.configure(config=cfg['config'], signals=cfg['signals'])   # motors: only when stopped
            opt.config['full_optimization'] = full

    # ------------------------------------------------------------------ actions
    def _check(self, cfg) -> bool:
        if not cfg['motors']:
            warn(self, 'Optimizer', 'Tick at least ONE motor.')
            return False
        if not cfg['signals']:
            warn(self, 'Optimizer', 'Tick at least ONE objective signal.')
            return False
        return True

    def _on_start(self):
        cfg = self.get_config()
        if self._check(cfg):
            ok, msg = self.app.start_optimizer(cfg, single_shot=False)
            if not ok:
                warn(self, self.label(), 'Cannot start:\n\n' + msg.replace(' ; ', '\n'))

    def _on_now(self):
        opt = self.automation
        if opt is not None and opt.state == AutoState.ACTIVE and not opt.single_shot:
            if not opt.trigger_now(self.app.core.clock.now()):
                info(self, 'Optimizer', 'A pass is already running.')
            return
        if opt is not None and opt.state in ENGAGED_STATES:
            info(self, 'Optimizer', 'The optimizer is already engaged.')
            return
        cfg = self.get_config()
        if self._check(cfg):
            ok, msg = self.app.start_optimizer(cfg, single_shot=True)
            if not ok:
                warn(self, self.label(), 'Cannot start:\n\n' + msg.replace(' ; ', '\n'))

    def _on_full(self):
        opt = self.automation
        if opt is not None and opt.state in ENGAGED_STATES:
            warn(self, 'Busy', 'Stop the optimizer first (otherwise both would fight over the motors).')
            return
        cfg = self.get_config()
        if not self._check(cfg):
            return
        if not ask_yes_no(self, 'Full optimization',
                          'Aggressive search: probe at the max step of each motor (bounded), more iterations, '
                          'reduced deadband. The motors move more than in a normal pass.\n\nContinue?'):
            return
        cfg['config']['full_optimization'] = True
        ok, msg = self.app.start_optimizer(cfg, single_shot=True)
        if not ok:
            warn(self, 'Optimizer', 'Cannot start:\n\n' + msg.replace(' ; ', '\n'))

    def _on_resume(self):
        ok, msg = self.app.core.resume(self.auto_id)
        if not ok:
            warn(self, 'Optimizer', 'Resume refused:\n\n' + msg.replace(' ; ', '\n'))

    def _on_capture(self):
        n = 0
        for r in range(self.table_signals.rowCount()):
            it = self.table_signals.item(r, 0)
            if it is None or it.checkState() != QtCore.Qt.Checked:
                continue
            if self.table_signals.cellWidget(r, 2).currentText() != 'Target Point':
                continue
            role = self.table_signals.item(r, 1).data(QtCore.Qt.UserRole)
            ch = self.app.core.roles.channel_for(role)
            ok, why = self.app.core.signals.freshness(ch)
            if not ok:
                warn(self, 'Target', f'{role}: {why}')
                continue
            self.table_signals.cellWidget(r, 5).setValue(self.app.core.signals.value(ch))
            self.table_signals.cellWidget(r, 6).setChecked(False)
            n += 1
        if n == 0:
            info(self, 'Target', 'No ticked signal in "Target Point" mode.')

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        opt = self.automation
        state = opt.state if opt is not None else AutoState.OFF
        self.state.set_state(state, opt.reason if opt else '', prefix=f'{self.label()}: ')
        engaged = state in ENGAGED_STATES
        self.btn_start.setEnabled(not engaged and not self.app.core.estop)
        self.btn_full.setEnabled(not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        self.btn_resume.setEnabled(state == AutoState.HELD)
        self.table_motors.setEnabled(not engaged)
        if opt is None:
            return
        now = self.app.core.clock.now()
        left = opt.seconds_to_next_pass(now) if state == AutoState.ACTIVE else None
        if left is None:
            self.countdown.setText('RUNNING' if opt.in_pass else '--:--')
        else:
            m, s = divmod(int(left), 60)
            self.countdown.setText(f'{m:02d}:{s:02d}')
        extra = f' · passes: {opt.pass_count} · iteration: {opt.iteration}'
        self.status.setText(f'<i>{opt.status}</i>{extra}' + (f'<br><b>{opt.reason}</b>' if opt.reason and
                                                                state != AutoState.ACTIVE else ''))
        if opt.history:
            it, cost = zip(*opt.history)
            self.cost_curve.setData(list(it), list(cost))
        else:
            self.cost_curve.setData([], [])
        # passive monitor (T4.3): latest values, no grab
        roles = [s['role'] for s in self.get_config()['signals']]
        if set(roles) != set(self._monitor_curves):
            self.monitor.clear()
            if self.monitor_legend is not None:
                try:
                    self.monitor_legend.scene().removeItem(self.monitor_legend)
                except Exception:
                    pass
            self.monitor_legend = self.monitor.addLegend()
            self._monitor_curves = {r: self.monitor.plot(pen=pg.mkPen(MONITOR_COLORS[i % len(MONITOR_COLORS)], width=2),
                                                         name=r) for i, r in enumerate(roles)}
        tnow = time.time()
        for role, curve in self._monitor_curves.items():
            h = self.app.core.signals.history(self.app.core.roles.channel_for(role))
            if h is None:
                continue
            t, v = h.arrays(since=tnow - 600)
            curve.setData(t - tnow, v, connect='finite')


class OptimizerTab(QtWidgets.QWidget):
    """Several named optimisations, one sub-tab each (same handling as the PID tab)."""

    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.widgets: Dict[str, SingleOptimizerWidget] = {}
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = styled_button('➕ Add optimization', None)
        self.btn_dup = styled_button('⧉ Duplicate', None, 'New optimization with the same settings')
        self.btn_rename = styled_button('✎ Rename', None)
        self.btn_remove = styled_button('❌ Remove', None)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_dup.clicked.connect(self._on_duplicate)
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_remove.clicked.connect(self._on_remove)
        for b in (self.btn_add, self.btn_dup, self.btn_rename, self.btn_remove):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(QtWidgets.QLabel('<i>Settings are saved in the preset (Presets tab).</i>'))
        lay.addLayout(bar)
        self.tabs = QtWidgets.QTabWidget()
        lay.addWidget(self.tabs)
        self.empty = QtWidgets.QLabel('No optimization in this preset: "➕ Add optimization".')
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setStyleSheet('color: gray; font-style: italic;')
        lay.addWidget(self.empty)
        self._update_empty()

    def _update_empty(self):
        self.empty.setVisible(not self.widgets)
        self.tabs.setVisible(bool(self.widgets))

    def _add_widget(self, cfg: dict) -> SingleOptimizerWidget:
        w = SingleOptimizerWidget(self.app, cfg)
        self.widgets[cfg['id']] = w
        self.tabs.addTab(w, cfg.get('label') or cfg['id'])
        self._update_empty()
        return w

    def _ask_id(self, title: str, default: str = '') -> Optional[str]:
        name, ok = QtWidgets.QInputDialog.getText(self, title, 'Identifier (e.g. Optim_HA):', text=default)
        if not ok or not name.strip():
            return None
        opt_id = sanitize_role_name(name)
        if opt_id in self.widgets:
            warn(self, 'Optimization', f'{opt_id} already exists')
            return None
        return opt_id

    def add(self, cfg: dict) -> SingleOptimizerWidget:
        w = self._add_widget(cfg)
        self.tabs.setCurrentWidget(w)
        self.app.preset_dirty = True
        return w

    def _on_add(self):
        opt_id = self._ask_id('New optimization')
        if opt_id:
            self.add(default_optimizer(opt_id))

    def _on_duplicate(self):
        w = self._current()
        if w is None:
            return
        opt_id = self._ask_id('Duplicate', w.opt_id + '_2')
        if opt_id:
            cfg = w.get_config()
            cfg.update({'id': opt_id, 'label': ''})
            self.add(cfg)

    def _current(self) -> Optional[SingleOptimizerWidget]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, SingleOptimizerWidget) else None

    def _on_rename(self):
        w = self._current()
        if w is None:
            return
        label, ok = QtWidgets.QInputDialog.getText(self, 'Rename', 'Display name:', text=w.label())
        if ok:
            w.settings.child('label').setValue(label.strip())

    def _on_remove(self):
        w = self._current()
        if w is None:
            return
        a = w.automation
        if a is not None and a.state in ENGAGED_STATES:
            warn(self, 'Optimization', 'Stop the optimization before removing it.')
            return
        if not ask_yes_no(self, 'Remove', f'Remove optimization {w.label()} from the preset?'):
            return
        self.app.core.remove_automation(w.auto_id)
        self.widgets.pop(w.opt_id, None)
        self.tabs.removeTab(self.tabs.currentIndex())
        w.deleteLater()
        self.app.preset_dirty = True
        self._update_empty()

    def select(self, auto_id: str) -> bool:
        w = self.widgets.get(auto_id.split(':', 1)[-1])
        if w is not None:
            self.tabs.setCurrentWidget(w)
            return True
        return False

    # ------------------------------------------------------------------ preset
    def load_from_preset(self, preset: dict):
        self.tabs.clear()
        for w in self.widgets.values():
            w.deleteLater()
        self.widgets = {}
        for cfg in preset.get('optimizers', []):
            self._add_widget(cfg)
        self._update_empty()

    def write_to_preset(self, preset: dict):
        preset['optimizers'] = [w.get_config() for w in self._ordered()]

    def configs(self) -> List[dict]:
        return [w.get_config() for w in self._ordered()]

    def _ordered(self) -> List[SingleOptimizerWidget]:
        return [w for w in (self.tabs.widget(i) for i in range(self.tabs.count()))
                if isinstance(w, SingleOptimizerWidget)]

    def quick_items(self) -> List[dict]:
        out = []
        for w in self._ordered():
            if w.settings.child('show_in_alignment').value():
                out.append({'id': w.auto_id, 'kind': 'optimizer', 'key': w.opt_id, 'label': w.label(),
                            'action': w.settings.child('button_action').value()})
        return out

    def config_of(self, auto_id: str) -> Optional[dict]:
        w = self.widgets.get(auto_id.split(':', 1)[-1])
        return w.get_config() if w is not None else None

    def set_hardware(self):
        for w in self.widgets.values():
            w.set_hardware()

    def refresh(self):
        w = self._current()
        if w is not None:
            w.refresh()
        for i in range(self.tabs.count()):
            wid = self.tabs.widget(i)
            if isinstance(wid, SingleOptimizerWidget):
                a = wid.automation
                st = a.state.value if a is not None else 'OFF'
                self.tabs.setTabText(i, f'{wid.label()} [{st}]')
