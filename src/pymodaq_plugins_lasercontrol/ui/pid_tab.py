"""PID tab (specification T5.5): the SETTINGS of the PIDs (they are run from the Alignment tab).

* top: overview table of every PID (state, signals → motors, error, RMS error, envelope used, start / stop
  to try a setting);
* bottom: the selected PID, in a PyMoDAQ parameter tree (left) with its live plots (right).

A PID regulates one or several signals ("loops") with the motors ticked in *Motors*. With one signal it is
the classic PID (setpoint, sign, Kp… and a weight per motor). With two signals or more (near field + far
field, for instance) it is a **coupled PID**: *Calibration & decoupling* measures every motor on every
signal (the sensitivity matrix) and *Apply* gives each signal the combination of motors that moves only
that signal, with its sign and Kp — nothing is changed before *Apply*.
"""
from __future__ import annotations

import copy
import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.roles import sanitize_role_name
from ..core.states import AutoState, ENGAGED_STATES, STATE_COLORS, STATE_LABELS
from ..engines.calibration import CalibrationResult, suggest_pid_settings
from ..engines.decoupling import decouple, decoupling_proposal, format_matrix
from ..engines.pid import COMMON_KEYS, default_loop, error_stats
from ..presets.schema import default_pid, normalize_pid
from .widgets import StateBadge, ask_yes_no, fmt, reset_rows, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

OVERVIEW_RMS_S = 120.0
WINDOWS = (('30 s', 30.0), ('2 min', 120.0), ('10 min', 600.0), ('all', math.inf))
TERM_COLORS = {'p': '#3498db', 'i': '#9b59b6', 'd': '#95a5a6'}
GENERAL = ('label', 'show_in_alignment')
LOOP_PARAMS = ('signal', 'setpoint', 'setpoint_mode', 'sign', 'kp', 'ki', 'kd', 'deadband')
#: compatibility names of the single-signal PID (loop 0)
LEGACY = {'loop_sign': 'sign'}


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


def loop_title(k: int, loop: dict) -> str:
    return f"Signal {k + 1}: {loop.get('signal') or '(choose)'}"


def pid_tree(cfg: dict, signals: List[str], actuators: List[str]) -> list:
    """Children of the parameter tree of a PID (preset format ``cfg``)."""
    loops = cfg['loops']
    coupled = len(loops) > 1
    st = cfg['settings']
    motor_items = list(dict.fromkeys(actuators + cfg['motors']))
    loop_groups = []
    for k, l in enumerate(loops):
        weights = [{'title': f'{m}:', 'name': m, 'type': 'float', 'value': float(l['weights'].get(m, 0.0)),
                    'decimals': 4, 'step': 0.1,
                    'tip': 'Step of this motor per unit of output of this loop (the max step applies to the '
                           'largest one). Coupled PID: set by Calibration & decoupling › Apply'}
                   for m in cfg['motors']]
        children = [
            {'title': 'Signal:', 'name': 'signal', 'type': 'list', 'limits': [''] + signals, 'value': l['signal'],
             'tip': '0D signal role regulated by this loop'},
            {'title': 'Setpoint:', 'name': 'setpoint', 'type': 'float', 'value': l['setpoint'], 'decimals': 6,
             'tip': 'Target value of the signal (signal units). Also moved from the Alignment tab (SP axes)'},
            {'title': '📍 Setpoint = current value', 'name': 'capture', 'type': 'action'},
            {'title': 'Setpoint mode:', 'name': 'setpoint_mode', 'type': 'list', 'limits': ['fixed', 'capture'],
             'value': l['setpoint_mode'], 'tip': 'capture: the setpoint takes the value of the signal at each start'},
            {'title': 'Loop sign:', 'name': 'sign', 'type': 'list', 'limits': ['+', '-'], 'value': l['sign'],
             'tip': '+ if a + output of the loop increases the signal. A wrong sign makes the loop diverge '
                    '(the envelopes then hold it). Set by Calibration › Apply'},
            {'title': 'Kp:', 'name': 'kp', 'type': 'float', 'value': l['kp'], 'min': 0.0, 'decimals': 4, 'step': 0.05,
             'tip': 'Proportional gain. 0.5 / loop gain = half of the error corrected per iteration (Apply)'},
            {'title': 'Ki:', 'name': 'ki', 'type': 'float', 'value': l['ki'], 'min': 0.0, 'decimals': 4,
             'tip': 'Integral gain (per second). Often 0: the motors already integrate the steps'},
            {'title': 'Kd:', 'name': 'kd', 'type': 'float', 'value': l['kd'], 'min': 0.0, 'decimals': 4,
             'tip': 'Derivative gain (s), on the measurement. Often 0'},
            {'title': 'Deadband:', 'name': 'deadband', 'type': 'float', 'value': l['deadband'], 'min': 0.0,
             'decimals': 4, 'tip': 'Smaller |error|: no move, integral frozen. 2σ of the signal (Apply)'},
            {'title': 'Motor weights', 'name': 'weights', 'type': 'group', 'expanded': not coupled,
             'children': weights or [{'title': 'no motor: tick them in "Motors"', 'name': 'none', 'type': 'str',
                                      'value': '', 'readonly': True}]},
        ]
        if coupled:
            children.append({'title': '✖ Remove this signal', 'name': 'remove', 'type': 'action'})
        loop_groups.append({'title': loop_title(k, l), 'name': f'loop{k}', 'type': 'group', 'children': children})
    loop_groups.append({'title': '➕ Add a signal (coupled PID)', 'name': 'add', 'type': 'action',
                        'tip': 'A second signal regulated with the same motors (e.g. far field next to near field): '
                               'then measure and apply the decoupling'})
    return [
        {'title': 'General', 'name': 'general', 'type': 'group', 'children': [
            {'title': 'Display name:', 'name': 'label', 'type': 'str', 'value': cfg['label'],
             'tip': 'Short name, also on its button of the Alignment tab'},
            {'title': 'Button in Alignment:', 'name': 'show_in_alignment', 'type': 'bool',
             'value': bool(cfg['show_in_alignment']),
             'tip': 'A button of the Alignment tab starts / stops it (no confirmation)'}]},
        {'title': 'Motors', 'name': 'motors', 'type': 'checklist', 'limits': motor_items, 'value': list(cfg['motors']),
         'exclusive': False, 'delay': 0.1, 'expanded': False,
         'tip': 'Motors this PID may move (at least as many as signals for a coupled PID)'},
        {'title': 'Signals' + (' (coupled PID)' if coupled else ''), 'name': 'signals', 'type': 'group',
         'children': loop_groups},
        {'title': 'Calibration & decoupling' if coupled else 'Calibration', 'name': 'calib', 'type': 'group',
         'children': [
             {'title': 'Cycles per motor:', 'name': 'cycles', 'type': 'int', 'value': 3, 'min': 1, 'max': 20,
              'tip': 'Bipolar cycles per motor (4 moves each): more = smaller errors, longer'},
             {'title': 'Max remaining coupling (%):', 'name': 'max_coupling', 'type': 'float', 'value': 25.0,
              'min': 1.0, 'max': 100.0, 'visible': coupled,
              'tip': 'Apply is refused when the errors of the measured matrix allow more remaining coupling'},
             {'title': '🎯 Measure', 'name': 'measure', 'type': 'action',
              'tip': 'Moves each motor by small bipolar steps (the others still) and records every signal of the '
                     'PID; the motors come back to their start positions. Nothing changes before Apply'},
             {'title': 'Status:', 'name': 'status', 'type': 'str', 'value': '', 'readonly': True},
             {'title': 'Result:', 'name': 'result', 'type': 'text', 'value': '', 'readonly': True},
             {'title': '✔ Apply', 'name': 'apply', 'type': 'action',
              'tip': ('Coupled: each signal gets the combination of motors that moves only this signal, with its '
                      'sign, Kp (0.5 / loop gain), deadband (2σ) and the settle time' if coupled else
                      'Loop sign, Kp (0.5 / loop gain), deadband (2σ) and settle time from the measurement')},
         ]},
        {'title': 'Timing & limits', 'name': 'timing', 'type': 'group', 'expanded': False, 'children': [
            {'title': 'Max step / iteration:', 'name': 'max_step', 'type': 'float', 'value': st['max_step'],
             'min': 0.0, 'decimals': 4, 'tip': 'Largest step of a motor per iteration (the whole step vector is '
                                               'scaled, its direction kept); the max step of the role applies too'},
            {'title': 'Period (s):', 'name': 'period_s', 'type': 'float', 'value': st['period_s'], 'min': 0.05,
             'tip': '0.5 s = 2 Hz. An iteration is skipped while a motor still moves'},
            {'title': 'Settle after move (s):', 'name': 'settle_s', 'type': 'float', 'value': st['settle_s'],
             'min': 0.0, 'tip': 'Only uses a sample received at least this long after the end of the last move'},
            {'title': 'Confirm each move:', 'name': 'confirm_moves', 'type': 'bool', 'value': st['confirm_moves'],
             'tip': 'Dialog box before each move (the PID is PAUSED during the question)'}]},
    ]


class SinglePIDWidget(QtWidgets.QWidget):
    """Settings and live plots of one PID (automation ``PID:<id>``)."""
    changed = QtCore.Signal()

    def __init__(self, app: 'LaserControlApp', cfg: dict, parent=None):
        super().__init__(parent)
        self.app = app
        cfg = normalize_pid(cfg)
        self.pid_id = cfg['id']
        self.cfg = cfg
        self._loading = False
        self._rebuild_pending = False
        self._engaged_shown: Optional[bool] = None
        self._cal_seen = None
        self._cal_note = ''
        self._settings: Optional[Parameter] = None        # built when first shown / used (fast preset loading)
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)

        # settings -----------------------------------------------------------------------------
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        head = QtWidgets.QHBoxLayout()
        self.state = StateBadge('', font_pt=11)
        head.addWidget(self.state, 1)
        self.btn_start = styled_button('▶ Start', '#27ae60', 'Try the settings (the PID is run from Alignment)')
        self.btn_stop = styled_button('⏹ Stop', '#7f8c8d', 'Stops the PID (no move)')
        self.btn_resume = styled_button('⏯ Resume', '#2980b9', 'Resume after a safety hold or a pause')
        self.btn_ref = styled_button('↺', None, 'New envelope reference: the S4 envelope is re-centered on the '
                                                'current positions')
        for b in (self.btn_start, self.btn_stop, self.btn_resume, self.btn_ref):
            head.addWidget(b)
        ll.addLayout(head)
        self.reason = QtWidgets.QLabel('')
        self.reason.setWordWrap(True)
        self.reason.setStyleSheet('font-weight: bold;')
        ll.addWidget(self.reason)
        self.tree = ParameterTree()
        self.tree.setMinimumWidth(330)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 190)
        ll.addWidget(self.tree, 1)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_resume.clicked.connect(self._on_resume)
        self.btn_ref.clicked.connect(self._on_reset_ref)
        main.addWidget(left, 4)

        # live ------------------------------------------------------------------------------------
        right = QtWidgets.QGroupBox('Monitoring')
        rl = QtWidgets.QVBoxLayout(right)
        bar = QtWidgets.QHBoxLayout()
        self.loop_combo = QtWidgets.QComboBox()
        self.loop_combo.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.loop_combo.setToolTip('Signal shown in the plots')
        bar.addWidget(self.loop_combo)
        bar.addWidget(QtWidgets.QLabel('Window:'))
        self.window = QtWidgets.QComboBox()
        self.window.setFocusPolicy(QtCore.Qt.ClickFocus)
        for label, s in WINDOWS:
            self.window.addItem(label, s)
        self.window.setCurrentIndex(1)
        bar.addWidget(self.window)
        bar.addStretch()
        rl.addLayout(bar)
        self.values = QtWidgets.QLabel('')
        self.values.setStyleSheet('font-family: Consolas, monospace;')
        self.values.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        rl.addWidget(self.values)
        self.stats = QtWidgets.QLabel('')
        self.stats.setToolTip('Over the chosen window: RMS of the error, standard deviation of the measurement, '
                              'number of iterations and of moves')
        rl.addWidget(self.stats)
        self._plots_layout = rl
        self.glw = None                                 # plots created when first shown (fast preset loading)
        main.addWidget(right, 5)
        self._fill_loop_combo()

    def _ensure_plots(self):
        if self.glw is not None:
            return
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
        self._plots_layout.addWidget(self.glw, 1)

    @property
    def settings(self) -> Parameter:
        if self._settings is None:
            self._build_tree()
        return self._settings

    def showEvent(self, e):
        super().showEvent(e)
        self._ensure_plots()
        if self._settings is None:
            self._build_tree()

    @property
    def auto_id(self) -> str:
        return f'PID:{self.pid_id}'

    def label(self) -> str:
        return self.cfg['label'] or self.pid_id

    def loops(self) -> List[dict]:
        return self.cfg['loops']

    def motors(self) -> List[str]:
        return list(self.cfg['motors'])

    def motor_weights(self) -> Dict[str, float]:
        """Weights of the first signal (single-signal PID)."""
        return {r: w for r, w in self.cfg['loops'][0]['weights'].items() if w}

    # ------------------------------------------------------------------ parameters
    def loop_param(self, k: int, name: str) -> Parameter:
        return self.settings.child('signals', f'loop{k}', name)

    def _param(self, name: str) -> Parameter:
        """A parameter by its short name: general, common (timing) or of the first signal."""
        name = LEGACY.get(name, name)
        if name in GENERAL:
            return self.settings.child('general', name)
        if name in COMMON_KEYS:
            return self.settings.child('timing', name)
        return self.loop_param(0, name)

    def _build_tree(self):
        roles = self.app.core.roles
        expanded = self._expanded_state() if self._settings is not None else {}
        self._loading = True
        try:
            self._settings = Parameter.create(name=f'pid_{self.pid_id}', type='group',
                                             children=pid_tree(self.cfg, sorted(roles.signals), sorted(roles.actuators)))
            for path, exp in expanded.items():
                try:
                    self.settings.child(*path).setOpts(expanded=exp)
                except Exception:
                    pass
            self.tree.setParameters(self.settings, showTop=False)
            self.settings.sigTreeStateChanged.connect(self._on_tree_changed)
            self.settings.child('signals', 'add').sigActivated.connect(self._on_add_loop)
            for k in range(len(self.cfg['loops'])):
                g = self.settings.child('signals', f'loop{k}')
                g.child('capture').sigActivated.connect(lambda _=None, k=k: self._on_capture(k))
                if len(self.cfg['loops']) > 1:
                    g.child('remove').sigActivated.connect(lambda _=None, k=k: self._on_remove_loop(k))
            self.settings.child('calib', 'measure').sigActivated.connect(self._on_calibrate)
            self.settings.child('calib', 'apply').sigActivated.connect(self._on_apply_calibration)
        finally:
            self._loading = False
        self._engaged_shown = None
        self._show_calibration()
        self._fill_loop_combo()

    def _fill_loop_combo(self):
        cur = self.loop_combo.currentIndex()
        self.loop_combo.blockSignals(True)
        self.loop_combo.clear()
        for k, l in enumerate(self.cfg['loops']):
            self.loop_combo.addItem(l['signal'] or f'signal {k + 1}')
        self.loop_combo.setCurrentIndex(min(max(cur, 0), self.loop_combo.count() - 1))
        self.loop_combo.blockSignals(False)

    def _expanded_state(self) -> Dict[tuple, bool]:
        out = {}

        def walk(p, path):
            for c in p.children():
                if c.type() in ('group', 'checklist'):
                    out[path + (c.name(),)] = bool(c.opts.get('expanded', True))
                    walk(c, path + (c.name(),))
        walk(self.settings, ())
        return out

    def _schedule_rebuild(self):
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QtCore.QTimer.singleShot(0, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_pending = False
        self._rebuild_if_built()

    def _on_tree_changed(self, _root, changes):
        if self._loading:
            return
        structural = False
        for param, change, data in changes:
            if change != 'value':
                continue
            path = self.settings.childPath(param) or []
            if not path:
                continue
            if path[0] == 'general' and len(path) == 2:
                self.cfg[path[1]] = data
            elif path[0] == 'timing' and len(path) == 2:
                self.cfg['settings'][path[1]] = data
            elif path[0] == 'motors':
                motors = list(self.settings.child('motors').value())
                if motors != self.cfg['motors']:
                    self.cfg['motors'] = motors
                    for l in self.cfg['loops']:
                        l['weights'] = {m: float(l['weights'].get(m, 1.0 if len(self.cfg['loops']) == 1 else 0.0))
                                        for m in motors}
                    structural = True
            elif path[0] == 'signals' and len(path) >= 3 and path[1].startswith('loop'):
                k = int(path[1][4:])
                loop = self.cfg['loops'][k]
                if path[2] == 'weights' and len(path) == 4:
                    loop['weights'][path[3]] = float(data)
                elif path[2] in LOOP_PARAMS:
                    loop[path[2]] = data
                    if path[2] == 'signal':
                        self.settings.child('signals', path[1]).setOpts(title=loop_title(k, loop))
                        structural = True       # plots selector, calibration text
        self._on_changed()
        if structural:
            self._schedule_rebuild()

    # ------------------------------------------------------------------ config
    def get_config(self) -> dict:
        return normalize_pid(copy.deepcopy(self.cfg))

    def load_config(self, cfg: dict):
        self.cfg = normalize_pid(cfg)
        self.pid_id = self.cfg['id']
        self._rebuild_if_built()

    def set_hardware(self):
        self._rebuild_if_built()

    def _rebuild_if_built(self):
        if self._settings is not None:
            self._build_tree()
        else:
            self._fill_loop_combo()

    def _on_changed(self):
        self.app.preset_dirty = True
        a = self.app.core.get(self.auto_id)
        if a is not None and a.state in ENGAGED_STATES:
            a.configure(self.get_config(), structure=False)     # live gains / setpoints / common settings
        for k in range(len(self.cfg['loops'])):
            self.app.manual.notify_virtual(f'SP:{self.pid_id}' + (f'/{k + 1}' if k else ''))
        self.changed.emit()

    def _on_add_loop(self):
        if self._engaged():
            return
        loop = default_loop()
        loop['weights'] = {m: 0.0 for m in self.cfg['motors']}
        self.cfg['loops'].append(loop)
        self._on_changed()
        self._schedule_rebuild()

    def _on_remove_loop(self, k: int):
        if self._engaged() or len(self.cfg['loops']) < 2:
            return
        sig = self.cfg['loops'][k]['signal'] or f'signal {k + 1}'
        if not ask_yes_no(self, self.label(), f'Remove {sig} from this PID?'):
            return
        self.cfg['loops'].pop(k)
        self._on_changed()
        self._schedule_rebuild()

    def _engaged(self) -> bool:
        a = self.app.core.get(self.auto_id)
        return a is not None and a.state in ENGAGED_STATES

    # ------------------------------------------------------------------ actions
    def driver(self):
        return self.app.pid_driver(self.pid_id)

    def _on_start(self):
        ok, msg = self.app.start_pid(self.get_config())
        if not ok:
            warn(self, self.label(), 'Cannot start:\n\n' + msg.replace(' ; ', '\n'))

    def _on_stop(self):
        self.app.core.stop(self.auto_id, 'manual stop')

    def _on_resume(self):
        ok, msg = self.app.core.resume(self.auto_id)
        if not ok:
            warn(self, self.label(), 'Resume refused:\n\n' + msg.replace(' ; ', '\n'))

    def _on_reset_ref(self):
        if ask_yes_no(self, 'Envelope', 'Re-center the S4 envelope of this PID on the current positions?'):
            self.app.core.reset_reference(self.auto_id)

    def set_setpoint(self, k: int, value: float):
        """Setpoint of signal ``k`` (applied live), without building the tree when it is not shown."""
        if self._settings is not None:
            self.loop_param(k, 'setpoint').setValue(value)
        else:
            self.cfg['loops'][k]['setpoint'] = value
            self._on_changed()

    def _on_capture(self, k: int):
        ch = self.app.core.roles.channel_for(self.cfg['loops'][k]['signal'])
        ok, why = self.app.core.signals.freshness(ch)
        if not ok:
            warn(self, 'Setpoint', f'Signal unavailable: {why}')
            return
        self.loop_param(k, 'setpoint').setValue(self.app.core.signals.value(ch))

    # ------------------------------------------------------------------ calibration / decoupling
    def _calibration(self):
        return self.app.calibration(self.pid_id)

    def results_by(self) -> Dict[str, Dict[str, CalibrationResult]]:
        return {s: {m: CalibrationResult.from_dict(d) for m, d in rows.items()}
                for s, rows in self.cfg['calibration'].items()}

    def _on_calibrate(self):
        cal = self._calibration()
        if cal is not None and cal.state in ENGAGED_STATES:
            self.app.core.stop(cal.id, 'calibration stopped')
            return
        cfg = self.get_config()
        signals = [l['signal'] for l in cfg['loops']]
        if not cfg['motors'] or '' in signals:
            warn(self, self.label(), 'Choose the signal(s) and tick the motors first.')
            return
        if not ask_yes_no(self, 'Calibration',
                          f"Measure {', '.join(cfg['motors'])} on {', '.join(signals)}?\n\nEach motor moves by "
                          f"small steps (both directions, at most its max step) while the others stay still, then "
                          f"comes back to its start position. Nothing changes in the PID before Apply."):
            return
        ok, msg = self.app.start_calibration(cfg, cycles=int(self.settings.child('calib', 'cycles').value()))
        if not ok:
            warn(self, self.label(), 'Cannot start the calibration:\n\n' + msg.replace(' ; ', '\n'))

    def _collect_calibration(self, cal):
        """Keep the results of a finished / aborted measurement (in the preset: the sensitivity matrix)."""
        self._cal_seen = cal
        for sig, rows in cal.results_by.items():
            store = self.cfg['calibration'].setdefault(sig, {})
            for m, res in rows.items():
                store[m] = res.to_dict()
        self._cal_note = '' if cal.completed else f'measurement stopped: {cal.reason or "aborted"}'
        if cal.results_by:
            self.app.preset_dirty = True
        self._show_calibration()

    def _result_text(self) -> str:
        signals = [l['signal'] for l in self.cfg['loops']]
        results = self.results_by()
        if len(signals) > 1:
            motors = self.cfg['motors']
            res = decouple(results, signals, motors)
            current = {l['signal']: l['weights'] for l in self.cfg['loops']}
            return format_matrix(res, current)
        lines = []
        for role, res in results.get(signals[0], {}).items():
            if role not in self.cfg['motors']:
                continue
            d = self.cfg['calibration'][signals[0]][role]
            g = f'g = {res.g:.4g}' + (f' ± {res.stderr:.2g}' if math.isfinite(res.stderr) else '')
            head = f"{'●' if res.ok else '✖ refused:'} {role}: {g}"
            extra = [f'σ = {res.noise_sigma:.3g}'] if math.isfinite(res.noise_sigma) else []
            if math.isfinite(res.settle_s):
                extra.append(f'settle {res.settle_s:.2g} s')
            if d.get('date'):
                extra.append(str(d['date']).replace('T', ' '))
            lines.append(head + ('  (' + ', '.join(extra) + ')' if extra else ''))
            lines.extend(f'    ⚠ {w}' for w in res.warnings)
        return '\n'.join(lines)

    def _show_calibration(self):
        if self._settings is None:
            return
        text = self._result_text()
        p = self.settings.child('calib', 'result')
        self._loading = True
        try:
            p.setValue(text or 'No measurement yet.')
            if self._cal_note:
                self.settings.child('calib', 'status').setValue(self._cal_note)
        finally:
            self._loading = False

    def calibration_proposal(self) -> Tuple[Optional[Dict[str, dict]], List[str]]:
        """{signal: {'weights'?, 'sign_kp', 'kp', 'deadband', 'settle_s'}} or (None, reasons)."""
        signals = [l['signal'] for l in self.cfg['loops']]
        results = self.results_by()
        if len(signals) > 1:
            cmax = float(self.settings.child('calib', 'max_coupling').value()) / 100.0
            prop, _res, problems = decoupling_proposal(results, signals, self.cfg['motors'], cmax)
            return prop, problems
        loop = self.cfg['loops'][0]
        st, notes = suggest_pid_settings(results.get(loop['signal'], {}),
                                         {m: w for m, w in loop['weights'].items() if w})
        return ({loop['signal']: st} if st is not None else None), notes

    def apply_proposal(self, prop: Dict[str, dict]):
        """Write the proposal in the settings (tree and preset)."""
        settle = 0.0
        for k, loop in enumerate(self.cfg['loops']):
            p = prop.get(loop['signal'])
            if p is None:
                continue
            if 'weights' in p:
                for m in self.cfg['motors']:
                    loop['weights'][m] = float(p['weights'].get(m, 0.0))
            loop['sign'] = p['sign_kp']
            loop['kp'] = round(float(p['kp']), 4)
            loop['deadband'] = round(float(p['deadband']), 4)
            settle = max(settle, float(p['settle_s']))
        self.cfg['settings']['settle_s'] = round(settle, 3)
        self._on_changed()
        self._rebuild_if_built()

    def _on_apply_calibration(self):
        if self._engaged():
            warn(self, self.label(), 'Stop the PID first.')
            return
        prop, notes = self.calibration_proposal()
        if prop is None:
            warn(self, self.label(), 'No proposal:\n\n' + '\n'.join(notes or ['measure first']))
            return
        lines = []
        for loop in self.cfg['loops']:
            p = prop[loop['signal']]
            comb = (', '.join(f'{m}×{v:+.4g}' for m, v in p['weights'].items() if v) + '\n    ') if 'weights' in p else ''
            lines.append(f"{loop['signal']}: {comb}sign {loop['sign']} → {p['sign_kp']}, Kp {loop['kp']:.4g} → "
                         f"{p['kp']:.4g}, deadband {loop['deadband']:.3g} → {p['deadband']:.3g}")
        lines.append(f"Settle after move: {self.cfg['settings']['settle_s']:.3g} → "
                     f"{max(p['settle_s'] for p in prop.values()):.3g} s")
        if notes:
            lines += [''] + notes
        if not ask_yes_no(self, 'Apply', '\n'.join(lines) + '\n\nApply these settings?'):
            return
        self.apply_proposal(prop)

    def _refresh_calibration(self, state):
        cal = self._calibration()
        running = cal is not None and cal.state in ENGAGED_STATES
        if cal is not None and not running and cal is not self._cal_seen:
            self._collect_calibration(cal)
        measure = self.settings.child('calib', 'measure')
        if running:
            done, total = cal.progress()
            status = f'measuring {min(done + 1, total)}/{total}…'
            title = '⏹ Stop measurement'
        else:
            status = self._cal_note or ('measured' if self.cfg['calibration'] else 'not measured')
            title = '🎯 Measure'
        self._loading = True
        try:
            if measure.opts.get('title') != title:
                measure.setOpts(title=title)
            st = self.settings.child('calib', 'status')
            if st.value() != status:
                st.setValue(status)
        finally:
            self._loading = False

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        a = self.app.core.get(self.auto_id)
        state = a.state if a is not None else AutoState.OFF
        self.state.set_state(state, a.reason if a else '', prefix=f'{self.label()}: ')
        self.reason.setText(('⚠ ' + a.reason) if a is not None and state in (
            AutoState.HELD, AutoState.PAUSED, AutoState.ERROR) else '')
        engaged = state in ENGAGED_STATES
        self.btn_start.setEnabled(not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        self.btn_resume.setEnabled(state == AutoState.HELD or (state == AutoState.PAUSED and a.pause_kind != 'confirm'))
        self.btn_ref.setEnabled(engaged)
        if engaged != self._engaged_shown:
            self._engaged_shown = engaged
            # while it runs, only the live settings stay editable (not the signals, motors or weights)
            self.settings.child('motors').setOpts(enabled=not engaged)
            self.settings.child('signals', 'add').setOpts(enabled=not engaged)
            for k in range(len(self.cfg['loops'])):
                g = self.settings.child('signals', f'loop{k}')
                g.child('signal').setOpts(enabled=not engaged)
                g.child('weights').setOpts(enabled=not engaged)
                if len(self.cfg['loops']) > 1:
                    g.child('remove').setOpts(enabled=not engaged)
            for n in ('measure', 'apply'):
                self.settings.child('calib', n).setOpts(enabled=not engaged)
        self._refresh_calibration(state)
        k = max(self.loop_combo.currentIndex(), 0)
        if a is None or k >= len(a.loops):
            self.values.setText('')
            self.stats.setText('')
            return
        loop = a.loops[k]
        t = loop.controller.last
        self.values.setText(
            f'measure {fmt(t.pv, 6):>10}   setpoint {fmt(t.setpoint, 6):>10}   error {fmt(t.error, 4):>9}\n'
            f'P {fmt(t.p, 4):>9}   I {fmt(t.i, 4):>9}   D {fmt(t.d, 4):>9}   output {fmt(t.output, 4):>9}\n'
            f'iterations {loop.iterations}   skipped (motor busy) {a.skipped_busy}')
        self._update_plots(loop, state)

    def _update_plots(self, loop, state):
        # Only an ACTIVE PID scrolls with the clock. Stopped, paused, held: the plots are frozen on the
        # last iteration (time 0 = last point), so the information stays on screen.
        self._ensure_plots()
        live = state == AutoState.ACTIVE
        now = time.time() if live or not loop.trace else loop.trace[-1].t
        span = float(self.window.currentData())
        pts = [p for p in loop.trace if now - p.t <= span]
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
        for key, c in self.curve_terms.items():
            c.setData(ts, [getattr(p, key) for p in pts])


class PIDTab(QtWidgets.QWidget):
    """Overview table of every PID on top, settings of the selected one below."""
    COLS = ['', 'PID', 'State', 'Signals → motors', 'Error', f'RMS error ({OVERVIEW_RMS_S / 60:g} min)',
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
        self.btn_remove = styled_button('❌ Remove', None)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove.clicked.connect(self._on_remove)
        for b in (self.btn_add, self.btn_remove):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(QtWidgets.QLabel('<i>Settings only: the PIDs are run from the Alignment tab. '
                                       'Saved in the preset.</i>'))
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
        split.setSizes([120, 600])
        lay.addWidget(split, 1)

    # ------------------------------------------------------------------ list management
    def _add_widget(self, cfg: dict) -> SinglePIDWidget:
        w = SinglePIDWidget(self.app, cfg)
        self.widgets[w.pid_id] = w
        self._order.append(w.pid_id)
        self.stack.addWidget(w)
        self._rebuild_overview()
        if self._current() is None:
            self._select_id(w.pid_id)
        return w

    def remove_widget(self, pid_id: str):
        w = self.widgets.pop(pid_id, None)
        if w is None:
            return
        self.app.core.remove_automation(w.auto_id)
        self._order.remove(pid_id)
        self.stack.removeWidget(w)
        w.deleteLater()
        self._rebuild_overview()
        if self._order:
            self._select_id(self._order[0])
        else:
            self.stack.setCurrentWidget(self.empty)

    def _on_add(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'New PID', 'Identifier (e.g. Pointing_X):')
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

    def _on_remove(self):
        w = self._current()
        if w is None:
            return
        if w._engaged():
            warn(self, 'PID', 'Stop the PID before removing it.')
            return
        if not ask_yes_no(self, 'Remove', f'Remove PID {w.pid_id} from the preset?'):
            return
        self.remove_widget(w.pid_id)
        self.app.preset_dirty = True

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
            sigs = ' + '.join(l['signal'] or '?' for l in w.loops())
            what = f"{sigs} → {', '.join(w.motors()) or '—'}"
            err = rms = ''
            if a is not None and state in ENGAGED_STATES:
                err = ' / '.join(fmt(l.controller.last.error, 3) for l in a.loops)
                rms = ' / '.join(fmt(error_stats(list(l.trace), now - OVERVIEW_RMS_S)[0], 3) for l in a.loops)
            worst, worst_role = None, ''
            for role in w.motors():
                f = used_fraction(*envelope_used(self.app, a, role)[:3])
                if f is not None and (worst is None or f > worst):
                    worst, worst_role = f, role
            texts = [w.label() + ('  (coupled)' if len(w.loops()) > 1 else ''), STATE_LABELS.get(state, str(state)),
                     what, err, rms, f'{100 * worst:.0f} % ({worst_role})' if worst is not None else '']
            for c, text in enumerate(texts, start=1):
                it = self.overview.item(i, c)
                if it is not None and it.text() != text:
                    it.setText(text)
            st_item = self.overview.item(i, self.STATE_COL)
            if st_item is not None:
                st_item.setForeground(QtGui.QBrush(QtGui.QColor(STATE_COLORS.get(state, '#7f8c8d'))))
                st_item.setToolTip(a.reason if a is not None and a.reason else '')
            b = self.overview.cellWidget(i, self.ACTION_COL)
            if b is not None:
                if state == AutoState.HELD or (state == AutoState.PAUSED and a.pause_kind == 'manual'):
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
        return [{'id': self.widgets[p].auto_id, 'kind': 'pid', 'key': p, 'label': self.widgets[p].label(),
                 'action': ''} for p in self._order if self.widgets[p].cfg['show_in_alignment']]

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
