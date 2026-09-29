"""PID tab (specification T5.5): several independent PIDs, each regulating one 0D signal with a
linear combination of motors.

* top: overview table of every PID (state, signal, error, RMS error, envelope used, start / stop),
* bottom: details of the selected PID: the motors it uses (and only those), the settings in two
  levels (essential / advanced, one loop sign) and the live plots (measurement + setpoint with the
  moves, error and P / I / D terms, time window, statistics).
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.events import EventKind, Level
from ..core.states import AutoState, ENGAGED_STATES, STATE_COLORS, STATE_LABELS
from ..core.roles import sanitize_role_name
from ..engines.pid import PIDSettings, error_stats
from ..presets.schema import default_pid
from .widgets import (EnvelopeGauge, StateBadge, ask_yes_no, fmt, reset_rows, set_combo_items, styled_button,
                      warn)

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

OVERVIEW_RMS_S = 120.0
WINDOWS = (('30 s', 30.0), ('2 min', 120.0), ('10 min', 600.0), ('all', math.inf))
TERM_COLORS = {'p': '#3498db', 'i': '#9b59b6', 'd': '#95a5a6'}
BIG_FONT_PT = 16


def pid_params() -> list:
    """Everything except the setpoint and Kp (large fields above the tree), in one collapsed group."""
    return [{'title': 'Advanced', 'name': 'advanced', 'type': 'group', 'expanded': False, 'children': [
        {'title': 'Display name:', 'name': 'label', 'type': 'str', 'value': '',
         'tip': 'Short name, also used for the button of the Alignment tab'},
        {'title': 'Button in Alignment:', 'name': 'show_in_alignment', 'type': 'bool', 'value': False,
         'tip': 'Shows a button under the motors of the Alignment tab: one click starts, another one stops '
                '(no confirmation), a click on a held PID resumes it'},
        {'title': 'Stabilization:', 'name': 'in_stabilization', 'type': 'bool', 'value': True,
         'tip': 'Started / stopped by the "Activate / Deactivate stabilization" buttons (Overview)'},
        {'title': 'Loop sign:', 'name': 'loop_sign', 'type': 'list', 'limits': ['+', '-'], 'value': '+',
         'tip': '+ if a + step of the motors increases the signal. A wrong sign makes the loop diverge: '
                'the S4/S5 envelopes then limit the damage and hold the PID'},
        {'title': 'Ki:', 'name': 'ki', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': 'Integral gain (per second). Often 0: the motor already integrates the steps. '
                'Anti-windup: |Ki·integral| ≤ max step'},
        {'title': 'Max step / iteration:', 'name': 'max_step', 'type': 'float', 'value': 1.0, 'min': 0.0,
         'tip': 'Bounds each motor AFTER weighting (the max_step of the role, S3, applies too)'},
        {'title': 'Period (s):', 'name': 'period_s', 'type': 'float', 'value': 0.5, 'min': 0.05,
         'tip': '0.5 s = 2 Hz. An iteration is skipped while a motor is still moving (no stacking)'},
        {'title': 'Setpoint mode:', 'name': 'setpoint_mode', 'type': 'list', 'limits': ['fixed', 'capture'],
         'value': 'fixed', 'tip': 'capture: the setpoint takes the current value of the signal at each start'},
        {'title': 'Kd:', 'name': 'kd', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': 'Derivative gain (s), on the measurement. Often 0 at 2 Hz with a noisy signal'},
        {'title': 'Deadband (error):', 'name': 'deadband', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': 'Smaller |error|: no move, integral frozen'},
        {'title': 'Settle after move (s):', 'name': 'settle_s', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': 'Only uses a sample received at least this long after the end of the last move'},
        {'title': 'Confirm each move:', 'name': 'confirm_moves', 'type': 'bool', 'value': False,
         'tip': 'Dialog box before each move (the PID is PAUSED during the question)'},
    ]}]


def envelope_used(app: 'LaserControlApp', a, role: str) -> Tuple[Optional[float], Optional[float],
                                                                   Optional[float], Optional[float]]:
    """(lo, hi, pos, ref) of the S4 envelope of ``role`` for the automation ``a`` (None when not engaged)."""
    core = app.core
    m = core.roles.module_for(role)
    pos = core.broker.position(m) if m else None
    if a is None or a.state not in ENGAGED_STATES or m is None:
        return None, None, pos, None
    ref = core.safety.reference(a.id, m)
    if ref is None:
        return None, None, pos, None
    lo, hi = core.safety.allowed_range(a.id, role, m, a.policy())
    return (lo if math.isfinite(lo) else None, hi if math.isfinite(hi) else None, pos, ref)


def used_fraction(lo, hi, pos) -> Optional[float]:
    """0 = center of the envelope, 1 = at its edge (same formula as the envelope gauge)."""
    if lo is None or hi is None or pos is None or hi <= lo:
        return None
    return abs((pos - lo) / (hi - lo) - 0.5) * 2


class SinglePIDWidget(QtWidgets.QWidget):
    """Details of one PID (automation ``PID:<id>``)."""
    changed = QtCore.Signal()

    def __init__(self, app: 'LaserControlApp', cfg: dict, parent=None):
        super().__init__(parent)
        self.app = app
        self.pid_id = cfg['id']
        self._loading = False
        self._motor_gauges: Dict[str, EnvelopeGauge] = {}
        self._motor_labels: Dict[str, QtWidgets.QLabel] = {}
        self._weights: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)

        # 1. signal + motors -------------------------------------------------------------
        left = QtWidgets.QGroupBox('1. Signal and motors')
        ll = QtWidgets.QVBoxLayout(left)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Signal (0D role):'))
        self.combo_signal = QtWidgets.QComboBox()
        self.combo_signal.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.combo_signal.currentTextChanged.connect(self._on_changed)
        row.addWidget(self.combo_signal, 1)
        ll.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Motor (role)', 'Weight', 'Δ engag.', 'Envelope', ''])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setColumnWidth(1, 90)
        self.table.horizontalHeaderItem(2).setToolTip('Displacement since engagement (center of the S4 envelope)')
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        ll.addWidget(self.table, 1)
        add_row = QtWidgets.QHBoxLayout()
        self.combo_add = QtWidgets.QComboBox()
        self.combo_add.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.btn_add_motor = styled_button('➕ Add motor', None, 'Add this motor to the combination (weight 1)')
        self.btn_add_motor.clicked.connect(self._on_add_motor)
        add_row.addWidget(self.combo_add, 1)
        add_row.addWidget(self.btn_add_motor)
        ll.addLayout(add_row)
        main.addWidget(left, 4)

        # 2. settings + actions -----------------------------------------------------------
        center = QtWidgets.QGroupBox('2. Settings')
        cl = QtWidgets.QVBoxLayout(center)
        self.settings = Parameter.create(name=f'pid_{self.pid_id}', type='group', children=pid_params())
        big = QtWidgets.QFormLayout()
        self._big: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        for name, title, tip, lo, dec, step in (
                ('setpoint', 'Setpoint', 'Target value of the signal (signal units)', -1e12, 6, 0.1),
                ('kp', 'Kp', 'Proportional gain (motor units per signal unit). Typical: 0.05 – 1', 0.0, 4, 0.05)):
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(lo, 1e12)
            sp.setDecimals(dec)
            sp.setSingleStep(step)
            sp.setKeyboardTracking(False)
            sp.setFocusPolicy(QtCore.Qt.ClickFocus)
            sp.setToolTip(tip)
            f = sp.font()
            f.setPointSize(BIG_FONT_PT)
            f.setBold(True)
            sp.setFont(f)
            sp.setMinimumHeight(36)
            sp.valueChanged.connect(self._on_changed)
            lbl = QtWidgets.QLabel(title)
            lbl.setFont(f)
            lbl.setToolTip(tip)
            big.addRow(lbl, sp)
            self._big[name] = sp
        cl.addLayout(big)
        self.tree = ParameterTree()
        self.tree.setParameters(self.settings, showTop=False)
        self.tree.setMinimumWidth(320)
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 170)          # the value column must stay visible
        self.settings.sigTreeStateChanged.connect(self._on_changed)
        cl.addWidget(self.tree, 1)
        self.state = StateBadge('', font_pt=11)
        cl.addWidget(self.state)
        self.reason = QtWidgets.QLabel('')
        self.reason.setWordWrap(True)
        self.reason.setStyleSheet('color: #c0392b;')
        cl.addWidget(self.reason)
        grid = QtWidgets.QGridLayout()
        self.btn_start = styled_button('▶ Start', '#27ae60')
        self.btn_stop = styled_button('⏹ Stop', '#7f8c8d', 'Stops the PID (no move)')
        self.btn_resume = styled_button('⏯ Resume', '#2980b9',
                                        'Manual resume after a safety hold (O10-A), integral reset')
        self.btn_invert = styled_button('⇄ Invert sign', None, 'Inverts the loop sign (applied live)')
        self.btn_capture = styled_button('📍 Setpoint = current value', None)
        self.btn_ref = styled_button('↺ New envelope reference', None,
                                     'The S4 envelope is re-centered on the current positions')
        grid.addWidget(self.btn_start, 0, 0)
        grid.addWidget(self.btn_stop, 0, 1)
        grid.addWidget(self.btn_resume, 1, 0)
        grid.addWidget(self.btn_invert, 1, 1)
        grid.addWidget(self.btn_capture, 2, 0, 1, 2)
        grid.addWidget(self.btn_ref, 3, 0, 1, 2)
        cl.addLayout(grid)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(lambda: self.app.core.stop(self.auto_id, 'manual stop'))
        self.btn_resume.clicked.connect(self._on_resume)
        self.btn_invert.clicked.connect(self._on_invert)
        self.btn_capture.clicked.connect(self._on_capture)
        self.btn_ref.clicked.connect(self._on_reset_ref)
        main.addWidget(center, 3)

        # 3. live ----------------------------------------------------------------------------
        right = QtWidgets.QGroupBox('3. Monitoring')
        rl = QtWidgets.QVBoxLayout(right)
        self.values = QtWidgets.QLabel('')
        self.values.setStyleSheet('font-family: Consolas, monospace;')
        self.values.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        rl.addWidget(self.values)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel('Window:'))
        self.window = QtWidgets.QComboBox()
        self.window.setFocusPolicy(QtCore.Qt.ClickFocus)
        for label, s in WINDOWS:
            self.window.addItem(label, s)
        self.window.setCurrentIndex(1)
        bar.addWidget(self.window)
        self.stats = QtWidgets.QLabel('')
        self.stats.setToolTip('Over the chosen window: RMS of the error, standard deviation of the measurement, '
                              'number of iterations and of moves')
        bar.addWidget(self.stats, 1)
        rl.addLayout(bar)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setFocusPolicy(QtCore.Qt.NoFocus)
        self.plot_pv = self.glw.addPlot(row=0, col=0)
        self.plot_err = self.glw.addPlot(row=1, col=0)
        self.plot_err.setXLink(self.plot_pv)
        for p in (self.plot_pv, self.plot_err):
            p.showGrid(x=True, y=True, alpha=0.25)
            p.getAxis('left').setWidth(60)
            p.hideButtons()
        self.plot_pv.getAxis('bottom').setStyle(showValues=False)
        self.plot_err.setLabel('bottom', 'time (s)')
        self.plot_pv.addLegend(offset=(6, 2))
        self.plot_err.addLegend(offset=(6, 2))
        self.curve_pv = self.plot_pv.plot(pen=pg.mkPen('#2ecc71', width=2), name='measurement')
        self.curve_sp = self.plot_pv.plot(pen=pg.mkPen('#e74c3c', width=1.5, style=QtCore.Qt.DashLine),
                                          name='setpoint')
        self.curve_moves = self.plot_pv.plot(pen=None, symbol='t1', symbolSize=8, symbolPen=None,
                                             symbolBrush='#f39c12', name='move')
        self.curve_err = self.plot_err.plot(pen=pg.mkPen('#e67e22', width=2), name='error')
        self.curve_terms = {k: self.plot_err.plot(pen=pg.mkPen(c, width=1, style=QtCore.Qt.DotLine),
                                                  name=k.upper()) for k, c in TERM_COLORS.items()}
        self.plot_err.addItem(pg.InfiniteLine(angle=0, pen=pg.mkPen((127, 127, 127, 120), width=1)))
        rl.addWidget(self.glw, 1)
        main.addWidget(right, 5)
        self.load_config(cfg)

    @property
    def auto_id(self) -> str:
        return f'PID:{self.pid_id}'

    def label(self) -> str:
        return self._param('label').value() or self.pid_id

    def _param(self, name: str):
        """Setpoint and Kp: large fields; everything else: the 'Advanced' group of the tree."""
        return self._big[name] if name in self._big else self.settings.child('advanced', name)

    # ------------------------------------------------------------------ motors
    def motor_weights(self) -> Dict[str, float]:
        return {r: float(sp.value()) for r, sp in self._weights.items()}

    def set_hardware(self):
        set_combo_items(self.combo_signal, [''] + sorted(self.app.core.roles.signals))
        self._fill_motors(self.motor_weights())

    def _fill_motors(self, weights: Dict[str, float]):
        was_loading = self._loading
        self._loading = True
        roles = [r for r, w in weights.items() if w]
        reset_rows(self.table, len(roles))
        self._motor_gauges, self._motor_labels, self._weights = {}, {}, {}
        for i, role in enumerate(roles):
            known = self.app.core.roles.is_actuator(role)
            it = QtWidgets.QTableWidgetItem(role if known else f'⚠ {role}')
            it.setFlags(QtCore.Qt.ItemIsEnabled)
            if not known:
                it.setToolTip('role not declared on this machine')
            self.table.setItem(i, 0, it)
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(-1000, 1000)
            sp.setDecimals(4)
            sp.setSingleStep(0.1)
            sp.setValue(float(weights[role]))
            sp.setFocusPolicy(QtCore.Qt.ClickFocus)
            sp.valueChanged.connect(self._on_changed)
            self.table.setCellWidget(i, 1, sp)
            lbl = QtWidgets.QLabel('')
            self.table.setCellWidget(i, 2, lbl)
            g = EnvelopeGauge()
            self.table.setCellWidget(i, 3, g)
            rm = QtWidgets.QToolButton()
            rm.setText('✕')
            rm.setToolTip('Remove this motor from the combination')
            rm.setFocusPolicy(QtCore.Qt.NoFocus)
            rm.clicked.connect(lambda _=False, r=role: self._on_remove_motor(r))
            self.table.setCellWidget(i, 4, rm)
            self._weights[role] = sp
            self._motor_gauges[role] = g
            self._motor_labels[role] = lbl
        free = [r for r in sorted(self.app.core.roles.actuators) if r not in self._weights]
        self.combo_add.clear()
        self.combo_add.addItems(free)
        self.btn_add_motor.setEnabled(bool(free))
        self._loading = was_loading

    def _on_add_motor(self):
        role = self.combo_add.currentText()
        if not role:
            return
        w = self.motor_weights()
        w[role] = 1.0
        self._fill_motors(w)
        self._on_changed()

    def _on_remove_motor(self, role: str):
        w = self.motor_weights()
        w.pop(role, None)
        self._fill_motors(w)
        self._on_changed()

    # ------------------------------------------------------------------ config <-> UI
    def get_config(self) -> dict:
        s = {name: self._param(name).value()
             for name in ('setpoint', 'kp', 'ki', 'max_step', 'period_s', 'setpoint_mode', 'kd', 'deadband',
                          'settle_s', 'confirm_moves')}
        sign = self._param('loop_sign').value()
        s.update(sign_kp=sign, sign_ki=sign, sign_kd=sign)
        return {'id': self.pid_id, 'label': self._param('label').value(),
                'in_stabilization': bool(self._param('in_stabilization').value()),
                'show_in_alignment': bool(self._param('show_in_alignment').value()),
                'signal': self.combo_signal.currentText(),
                'motors': {r: w for r, w in self.motor_weights().items() if w},
                'settings': PIDSettings.from_dict(s).to_dict()}

    def load_config(self, cfg: dict):
        self._loading = True
        try:
            set_combo_items(self.combo_signal, [''] + sorted(self.app.core.roles.signals), cfg.get('signal', ''))
            self._fill_motors(cfg.get('motors', {}) or {})
            st = PIDSettings.from_dict(cfg.get('settings', {}) or {})
            if len({st.sign_kp, st.sign_ki, st.sign_kd}) > 1:   # legacy per-term signs: keep the Kp one
                self.app.core.events.publish(
                    EventKind.INFO, self.auto_id,
                    f"{cfg.get('label') or self.pid_id}: different Kp / Ki / Kd signs in the preset, "
                    f"unified to the Kp sign ({st.sign_kp})", Level.WARNING)
            values = dict(label=cfg.get('label', ''), in_stabilization=cfg.get('in_stabilization', True),
                          show_in_alignment=cfg.get('show_in_alignment', False), loop_sign=st.sign_kp,
                          setpoint=st.setpoint, kp=st.kp, ki=st.ki, max_step=st.max_step, period_s=st.period_s,
                          setpoint_mode=st.setpoint_mode, kd=st.kd, deadband=st.deadband, settle_s=st.settle_s,
                          confirm_moves=st.confirm_moves)
            for name, v in values.items():
                try:
                    self._param(name).setValue(v)
                except Exception:
                    pass
        finally:
            self._loading = False

    def _on_changed(self, *args):
        if self._loading:
            return
        self.app.preset_dirty = True
        a = self.app.core.get(self.auto_id)
        if a is not None and a.state in ENGAGED_STATES:
            cfg = self.get_config()
            a.set_settings(PIDSettings.from_dict(cfg['settings']))   # live gains/setpoint/sign update
            a.label = cfg['label'] or f'PID {self.pid_id}'
            a.in_stabilization = cfg['in_stabilization']
        self.changed.emit()

    # ------------------------------------------------------------------ actions
    def _on_start(self):
        ok, msg = self.app.start_pid(self.get_config())
        if not ok:
            warn(self, self.label(), 'Cannot start:\n\n' + msg.replace(' ; ', '\n'))

    def _on_resume(self):
        ok, msg = self.app.core.resume(self.auto_id)
        if not ok:
            warn(self, self.label(), 'Resume refused:\n\n' + msg.replace(' ; ', '\n'))

    def _on_invert(self):
        p = self._param('loop_sign')
        p.setValue('-' if p.value() == '+' else '+')

    def _on_capture(self):
        ch = self.app.core.roles.channel_for(self.combo_signal.currentText())
        ok, why = self.app.core.signals.freshness(ch)
        if not ok:
            warn(self, 'Setpoint', f'Signal unavailable: {why}')
            return
        self._param('setpoint').setValue(self.app.core.signals.value(ch))

    def _on_reset_ref(self):
        if ask_yes_no(self, 'Envelope', 'Re-center the S4 envelope of this PID on the current positions?'):
            self.app.core.reset_reference(self.auto_id)

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        a = self.app.core.get(self.auto_id)
        state = a.state if a is not None else AutoState.OFF
        self.state.set_state(state, a.reason if a else '', prefix=f'{self.label()}: ')
        self.reason.setText(a.reason if a is not None and state in (AutoState.HELD, AutoState.PAUSED,
                                                                     AutoState.ERROR) else '')
        engaged = state in ENGAGED_STATES
        self.btn_start.setEnabled(not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        self.btn_resume.setEnabled(state in (AutoState.HELD,) or
                                   (state == AutoState.PAUSED and a is not None and a.pause_kind != 'confirm'))
        self.btn_ref.setEnabled(engaged)
        self.table.setEnabled(not engaged)
        self.combo_add.setEnabled(not engaged)
        self.btn_add_motor.setEnabled(not engaged and self.combo_add.count() > 0)
        self.combo_signal.setEnabled(not engaged)
        for role, g in self._motor_gauges.items():
            lo, hi, pos, ref = envelope_used(self.app, a, role)
            g.set_values(lo, hi, pos, ref)
            self._motor_labels[role].setText(f'{pos - ref:+.4g}' if pos is not None and ref is not None else '')
        if a is None:
            self.values.setText('')
            self.stats.setText('')
            return
        t = a.controller.last
        self.values.setText(
            f'measure {fmt(t.pv, 6):>10}   setpoint {fmt(t.setpoint, 6):>10}   error {fmt(t.error, 4):>9}\n'
            f'P {fmt(t.p, 4):>9}   I {fmt(t.i, 4):>9}   D {fmt(t.d, 4):>9}   output {fmt(t.output, 4):>9}\n'
            f'iterations {a.iterations}   skipped (motor busy) {a.skipped_busy}')
        self._update_plots(a)

    def _update_plots(self, a):
        # Only an ACTIVE PID scrolls with the clock. Stopped, paused, held: the plots are frozen on the
        # last iteration (time 0 = last point), so the information stays on screen.
        live = a.state == AutoState.ACTIVE
        if live or not a.trace:
            now = time.time()
        else:
            now = a.trace[-1].t
        span = float(self.window.currentData())
        pts = [p for p in a.trace if now - p.t <= span]
        rms, std, n = error_stats(pts, -math.inf)
        moves = sum(1 for p in pts if p.moved > 0)
        self.stats.setText(f'RMS error {fmt(rms, 3)} · σ measure {fmt(std, 3)} · {n} it. · {moves} moves'
                           + ('' if live else '  (frozen: time 0 = last iteration)'))
        ts = [p.t - now for p in pts]
        self.curve_pv.setData(ts, [p.pv for p in pts], connect='finite')
        self.curve_sp.setData(ts, [p.setpoint for p in pts])
        mv = [p for p in pts if p.moved > 0]
        self.curve_moves.setData([p.t - now for p in mv], [p.pv for p in mv])
        self.curve_err.setData(ts, [p.error for p in pts], connect='finite')
        for k, c in self.curve_terms.items():
            c.setData(ts, [getattr(p, k) for p in pts])


class PIDTab(QtWidgets.QWidget):
    """Overview table of every PID on top, details of the selected one below."""
    COLS = ['', 'PID', 'State', 'Signal → motors', 'Error', f'RMS error ({OVERVIEW_RMS_S / 60:g} min)',
            'Envelope used']
    ACTION_COL, STATE_COL, ENVELOPE_COL = 0, 2, 6

    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.widgets: Dict[str, SinglePIDWidget] = {}
        self._order: List[str] = []
        self._signature = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = styled_button('➕ Add PID', None)
        self.btn_rename = styled_button('✎ Rename', None)
        self.btn_remove = styled_button('❌ Remove', None)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_remove.clicked.connect(self._on_remove)
        for b in (self.btn_add, self.btn_rename, self.btn_remove):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(QtWidgets.QLabel('<i>Settings are saved in the preset (Presets tab).</i>'))
        lay.addLayout(bar)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.setChildrenCollapsible(False)
        self.overview = QtWidgets.QTableWidget(0, len(self.COLS))
        self.overview.setHorizontalHeaderLabels(self.COLS)
        oh = self.overview.horizontalHeader()
        oh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        oh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.overview.verticalHeader().setVisible(False)
        self.overview.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.overview.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.overview.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.overview.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.overview.itemSelectionChanged.connect(self._on_row_selected)
        split.addWidget(self.overview)
        self.stack = QtWidgets.QStackedWidget()
        self.empty = QtWidgets.QLabel('No PID in this preset: "➕ Add PID".')
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setStyleSheet('color: gray; font-style: italic;')
        self.stack.addWidget(self.empty)
        split.addWidget(self.stack)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        split.setSizes([140, 600])
        lay.addWidget(split, 1)

    # ------------------------------------------------------------------ list management
    def _add_widget(self, cfg: dict) -> SinglePIDWidget:
        w = SinglePIDWidget(self.app, cfg)
        self.widgets[cfg['id']] = w
        self._order.append(cfg['id'])
        self.stack.addWidget(w)
        self._rebuild_overview()
        if self._current() is None:
            self._select_id(cfg['id'])
        return w

    def _on_add(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'New PID', 'Identifier (e.g. TipTilt_H):')
        if not ok or not name.strip():
            return
        pid_id = sanitize_role_name(name)
        if pid_id in self.widgets:
            warn(self, 'PID', f'{pid_id} already exists')
            return
        self._add_widget(default_pid(pid_id))
        self._select_id(pid_id)
        self.app.preset_dirty = True

    def _current(self) -> Optional[SinglePIDWidget]:
        w = self.stack.currentWidget()
        return w if isinstance(w, SinglePIDWidget) else None

    def _select_id(self, pid_id: str):
        w = self.widgets.get(pid_id)
        if w is None:
            return
        self.stack.setCurrentWidget(w)
        row = self._order.index(pid_id)
        if self.overview.currentRow() != row:
            self.overview.selectRow(row)

    def _on_row_selected(self):
        row = self.overview.currentRow()
        if 0 <= row < len(self._order):
            w = self.widgets[self._order[row]]
            self.stack.setCurrentWidget(w)
            w.refresh()

    def _on_rename(self):
        w = self._current()
        if w is None:
            return
        label, ok = QtWidgets.QInputDialog.getText(self, 'Rename', 'Display name:', text=w.label())
        if ok:
            w._param('label').setValue(label.strip())

    def _on_remove(self):
        w = self._current()
        if w is None:
            return
        a = self.app.core.get(w.auto_id)
        if a is not None and a.state in ENGAGED_STATES:
            warn(self, 'PID', 'Stop the PID before removing it.')
            return
        if not ask_yes_no(self, 'Remove', f'Remove PID {w.pid_id} from the preset?'):
            return
        self.app.core.remove_automation(w.auto_id)
        self.widgets.pop(w.pid_id, None)
        self._order.remove(w.pid_id)
        self.stack.removeWidget(w)
        w.deleteLater()
        self.app.preset_dirty = True
        self._rebuild_overview()
        if self._order:
            self._select_id(self._order[0])
        else:
            self.stack.setCurrentWidget(self.empty)

    def select(self, auto_id: str) -> bool:
        pid_id = auto_id.split(':', 1)[-1]
        if pid_id in self.widgets:
            self._select_id(pid_id)
            return True
        return False

    # ------------------------------------------------------------------ overview table
    def _rebuild_overview(self):
        cur = self._current()
        self.overview.blockSignals(True)
        reset_rows(self.overview, len(self._order))
        for i, pid_id in enumerate(self._order):
            for c in range(1, len(self.COLS)):
                self.overview.setItem(i, c, QtWidgets.QTableWidgetItem(''))
            b = QtWidgets.QPushButton('')
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.clicked.connect(lambda _=False, p=pid_id: self._on_row_action(p))
            self.overview.setCellWidget(i, self.ACTION_COL, b)
        if cur is not None and cur.pid_id in self._order:
            self.overview.selectRow(self._order.index(cur.pid_id))
        self.overview.blockSignals(False)
        self._signature = tuple(self._order)

    def _on_row_action(self, pid_id: str):
        """Same action as the Alignment button: start / stop / resume, no confirmation."""
        w = self.widgets.get(pid_id)
        if w is None:
            return
        ok, msg = self.app.quick_click(w.auto_id)
        if not ok and msg:
            warn(self, w.label(), msg.replace(' ; ', '\n'))
        self.refresh()

    def _refresh_overview(self):
        if self._signature != tuple(self._order):
            self._rebuild_overview()
        now = time.time()
        for i, pid_id in enumerate(self._order):
            w = self.widgets[pid_id]
            a = self.app.core.get(w.auto_id)
            state = a.state if a is not None else AutoState.OFF
            motors = ', '.join(f'{r}×{wt:+g}' for r, wt in w.motor_weights().items() if wt) or '—'
            err = rms = None
            if a is not None and state in ENGAGED_STATES:
                err = a.controller.last.error
                rms = error_stats(list(a.trace), now - OVERVIEW_RMS_S)[0]
            worst, worst_role = None, ''
            for role in w.motor_weights():
                f = used_fraction(*envelope_used(self.app, a, role)[:3])
                if f is not None and (worst is None or f > worst):
                    worst, worst_role = f, role
            texts = [w.label(), STATE_LABELS.get(state, str(state)),
                     f'{w.combo_signal.currentText() or "—"} → {motors}',
                     fmt(err, 4) if err is not None else '', fmt(rms, 3) if rms is not None else '',
                     f'{100 * worst:.0f} % ({worst_role})' if worst is not None else '']
            for c, text in enumerate(texts, start=1):
                it = self.overview.item(i, c)
                if it is not None and it.text() != text:
                    it.setText(text)
            st_item = self.overview.item(i, self.STATE_COL)
            if st_item is not None:
                st_item.setForeground(QtGui.QBrush(QtGui.QColor(STATE_COLORS.get(state, '#7f8c8d'))))
                st_item.setToolTip(a.reason if a is not None and a.reason else '')
            env_item = self.overview.item(i, self.ENVELOPE_COL)
            if env_item is not None:
                env_color = '#27ae60' if worst is None or worst < 0.6 else ('#f39c12' if worst < 0.9 else '#e74c3c')
                env_item.setForeground(QtGui.QBrush(QtGui.QColor(env_color)))
            b = self.overview.cellWidget(i, self.ACTION_COL)
            if b is not None:
                if state == AutoState.HELD:
                    text, bg = '⏯ Resume', '#2980b9'
                elif state in ENGAGED_STATES:
                    text, bg = '⏹ Stop', '#7f8c8d'
                else:
                    text, bg = '▶ Start', '#27ae60'
                if b.text() != text:
                    b.setText(text)
                    b.setStyleSheet(f'QPushButton {{ background-color: {bg}; color: white; font-weight: bold; '
                                    f'padding: 1px 8px; }}')
                b.setEnabled(not (self.app.core.estop and state not in ENGAGED_STATES))

    # ------------------------------------------------------------------ preset
    def load_from_preset(self, preset: dict):
        for w in self.widgets.values():
            self.stack.removeWidget(w)
            w.deleteLater()
        self.widgets = {}
        self._order = []
        self.stack.setCurrentWidget(self.empty)
        for cfg in preset.get('pids', []):
            self._add_widget(cfg)
        self._rebuild_overview()

    def write_to_preset(self, preset: dict):
        preset['pids'] = self.configs()

    def configs(self) -> List[dict]:
        return [self.widgets[p].get_config() for p in self._order]

    def quick_items(self) -> List[dict]:
        """PIDs shown as buttons in the Alignment tab (cheap: read at every UI refresh)."""
        out = []
        for p in self._order:
            w = self.widgets[p]
            if w._param('show_in_alignment').value():
                out.append({'id': w.auto_id, 'kind': 'pid', 'key': w.pid_id, 'label': w.label(), 'action': ''})
        return out

    def config_of(self, auto_id: str) -> Optional[dict]:
        w = self.widgets.get(auto_id.split(':', 1)[-1])
        return w.get_config() if w is not None else None

    def set_hardware(self):
        for w in self.widgets.values():
            w.set_hardware()

    def refresh(self):
        self._refresh_overview()
        w = self._current()
        if w is not None:
            w.refresh()
