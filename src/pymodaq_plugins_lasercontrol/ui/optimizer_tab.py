"""Drift optimiser tab: historical settings and modes (behind the pluggable interface T4.5),
periodic passes, 'Optimize now', 'Full optimization', passive live monitor (T4.3) and cost plot.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List

import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.states import AutoState, ENGAGED_STATES
from ..engines.optimizer import DEFAULT_OPT_CONFIG, RUN_MODES
from .widgets import StateBadge, ask_yes_no, fmt, info, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

MONITOR_COLORS = ['#00c2ff', '#ff9100', '#7cff00', '#ff4dd2', '#ffd23f', '#a276ff']


def opt_params() -> list:
    d = DEFAULT_OPT_CONFIG
    return [
        {'title': 'Intervalle entre passes (min) :', 'name': 'interval_min', 'type': 'float', 'value': d['interval_min'],
         'min': 0.05},
        {'title': 'Temps d’attente après mouvement (s) :', 'name': 'dwell', 'type': 'float', 'value': d['dwell'],
         'min': 0.0},
        {'title': 'Itérations max / passe :', 'name': 'max_iterations', 'type': 'int', 'value': d['max_iterations'],
         'min': 1},
        {'title': 'Mode :', 'name': 'run_mode', 'type': 'list', 'limits': RUN_MODES, 'value': d['run_mode']},
        {'title': 'Gradient / Newton-lite', 'name': 'gradient_group', 'type': 'group', 'children': [
            {'title': 'Échantillons par point :', 'name': 'gradient_samples', 'type': 'int',
             'value': d['gradient_samples'], 'min': 1},
            {'title': 'Amortissement :', 'name': 'damping_factor', 'type': 'float', 'value': d['damping_factor'],
             'min': 0.0},
        ]},
        {'title': 'Critères d’arrêt', 'name': 'stop_group', 'type': 'group', 'children': [
            {'title': 'Patience (itérations) :', 'name': 'stall_patience', 'type': 'int',
             'value': d['stall_patience'], 'min': 1},
            {'title': 'Échelle de bande morte :', 'name': 'deadband_scale', 'type': 'float',
             'value': d['deadband_scale'], 'min': 0.001},
            {'title': 'Facteur de sécurité :', 'name': 'safety_factor', 'type': 'float', 'value': d['safety_factor'],
             'min': 0.1},
            {'title': 'Reculs consécutifs max :', 'name': 'max_backoffs', 'type': 'int', 'value': d['max_backoffs'],
             'min': 1},
        ]},
        {'title': 'Optimisation complète', 'name': 'full_opt_group', 'type': 'group', 'children': [
            {'title': 'Itérations :', 'name': 'full_opt_iterations', 'type': 'int', 'value': d['full_opt_iterations'],
             'min': 1},
            {'title': 'Patience :', 'name': 'full_opt_stall_patience', 'type': 'int',
             'value': d['full_opt_stall_patience'], 'min': 1},
            {'title': 'Échelle de bande morte :', 'name': 'full_opt_deadband_scale', 'type': 'float',
             'value': d['full_opt_deadband_scale'], 'min': 0.0001},
        ]},
        {'title': 'Mesure', 'name': 'measure_group', 'type': 'group', 'children': [
            {'title': 'Trames pour le bruit :', 'name': 'noise_samples', 'type': 'int', 'value': d['noise_samples'],
             'min': 2},
            {'title': 'Trames par point :', 'name': 'probe_samples', 'type': 'int', 'value': d['probe_samples'],
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
}

MOTOR_COLS = ['Moteur (rôle)', 'Pas max', 'Pas de sonde', 'Ordre']
SIGNAL_COLS = ['Actif', 'Signal (rôle)', 'Mode', 'Poids', 'Exposant p', 'Cible', 'Auto']


class OptimizerTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self._loading = False
        self._monitor_curves: Dict[str, pg.PlotDataItem] = {}
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)

        left_w = QtWidgets.QWidget()
        left = QtWidgets.QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        self.settings = Parameter.create(name='optimizer', type='group', children=opt_params())
        self.tree = ParameterTree()
        self.tree.setParameters(self.settings, showTop=False)
        self.tree.setMinimumHeight(260)
        self.settings.sigTreeStateChanged.connect(self._on_changed)
        left.addWidget(QtWidgets.QLabel('<b>1. Réglages</b>'))
        left.addWidget(self.tree)
        left.addWidget(QtWidgets.QLabel('<b>2. Moteurs</b> <i>(« Ordre » : mode séquentiel seulement)</i>'))
        self.table_motors = QtWidgets.QTableWidget(0, len(MOTOR_COLS))
        self.table_motors.setHorizontalHeaderLabels(MOTOR_COLS)
        self.table_motors.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table_motors.verticalHeader().setVisible(False)
        self.table_motors.setMinimumHeight(110)
        self.table_motors.itemChanged.connect(self._on_changed)
        left.addWidget(self.table_motors)
        left.addWidget(QtWidgets.QLabel('<b>3. Signaux objectifs</b>'))
        self.table_signals = QtWidgets.QTableWidget(0, len(SIGNAL_COLS))
        self.table_signals.setHorizontalHeaderLabels(SIGNAL_COLS)
        self.table_signals.verticalHeader().setVisible(False)
        self.table_signals.setMinimumHeight(130)
        self.table_signals.itemChanged.connect(self._on_changed)
        left.addWidget(self.table_signals)
        self.btn_capture = styled_button('📍 Cibles = valeurs actuelles', None,
                                         'Pour chaque signal coché en mode « Target Point » : cible = dernière '
                                         'valeur reçue, « Auto » décoché (cible fixe).')
        self.btn_capture.clicked.connect(self._on_capture)
        left.addWidget(self.btn_capture)
        btns = QtWidgets.QGridLayout()
        self.btn_start = styled_button('▶ Optimiseur périodique', '#27ae60')
        self.btn_stop = styled_button('⏹ Arrêter', '#7f8c8d')
        self.btn_now = styled_button('⚡ Optimiser maintenant', '#2980b9',
                                     'Une passe tout de suite (réveille l’optimiseur périodique s’il tourne)')
        self.btn_full = styled_button('🚀 Optimisation complète', '#b71c1c',
                                      'Recherche agressive en une passe (sonde au pas max). Optimiseur '
                                      'périodique arrêté requis.')
        self.btn_resume = styled_button('⏯ Reprendre', '#2980b9', 'Reprise manuelle après un gel de sécurité')
        btns.addWidget(self.btn_start, 0, 0)
        btns.addWidget(self.btn_stop, 0, 1)
        btns.addWidget(self.btn_now, 1, 0)
        btns.addWidget(self.btn_resume, 1, 1)
        btns.addWidget(self.btn_full, 2, 0, 1, 2)
        left.addLayout(btns)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(lambda: self.app.core.stop('OPT', 'arrêt manuel'))
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
        top.addWidget(QtWidgets.QLabel('⏱ prochaine passe :'))
        top.addWidget(self.countdown)
        top.addStretch()
        right.addLayout(top)
        self.status = QtWidgets.QLabel('')
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        mon = QtWidgets.QGroupBox('Moniteur passif des signaux objectifs (dérive)')
        ml = QtWidgets.QVBoxLayout(mon)
        self.monitor = pg.PlotWidget()
        self.monitor.setFocusPolicy(QtCore.Qt.NoFocus)
        self.monitor.showGrid(x=True, y=True, alpha=0.25)
        self.monitor.setLabel('bottom', 'temps (s)')
        self.monitor_legend = self.monitor.addLegend()
        ml.addWidget(self.monitor)
        right.addWidget(mon, 2)
        cost = QtWidgets.QGroupBox('Convergence (coût par itération)')
        cl = QtWidgets.QVBoxLayout(cost)
        self.cost_plot = pg.PlotWidget()
        self.cost_plot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.cost_plot.showGrid(x=True, y=True, alpha=0.25)
        self.cost_plot.setLabel('bottom', 'itération')
        self.cost_plot.setLabel('left', 'coût f(x)')
        self.cost_curve = self.cost_plot.plot(pen=pg.mkPen('#ff9100', width=2), symbol='o', symbolSize=5)
        cl.addWidget(self.cost_plot)
        right.addWidget(cost, 2)
        main.addLayout(right, 1)
        self.set_hardware()

    # ------------------------------------------------------------------ tables
    def set_hardware(self, cfg: dict = None):
        cfg = cfg or self.get_config()
        self._loading = True
        try:
            motors = {m['role']: m for m in cfg.get('motors', [])}
            roles = sorted(self.app.core.roles.actuators)
            roles += [r for r in motors if r not in roles]
            self.table_motors.setRowCount(len(roles))
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
                sp.valueChanged.connect(self._on_changed)
                self.table_motors.setCellWidget(i, 3, sp)
            sigs = {s['role']: s for s in cfg.get('signals', [])}
            sroles = sorted(self.app.core.roles.signals)
            sroles += [r for r in sigs if r not in sroles]
            self.table_signals.setRowCount(len(sroles))
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
            self.table_signals.resizeColumnsToContents()
        finally:
            self._loading = False

    # ------------------------------------------------------------------ config
    def get_config(self) -> dict:
        cfg = {}
        for key, path in FLAT_KEYS.items():
            try:
                cfg[key] = self.settings.child(*path).value()
            except Exception:
                pass
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
        return {'config': cfg, 'motors': motors, 'signals': signals}

    def load_from_preset(self, preset: dict):
        opt = preset.get('optimizer', {})
        self._loading = True
        try:
            for key, path in FLAT_KEYS.items():
                if key in opt.get('config', {}):
                    try:
                        self.settings.child(*path).setValue(opt['config'][key])
                    except Exception:
                        pass
        finally:
            self._loading = False
        self.set_hardware({'motors': opt.get('motors', []), 'signals': opt.get('signals', [])})

    def write_to_preset(self, preset: dict):
        cfg = self.get_config()
        cfg['config']['full_optimization'] = False
        preset['optimizer'] = cfg

    def _on_changed(self, *args):
        if self._loading:
            return
        self.app.preset_dirty = True
        opt = self.app.optimizer
        if opt is not None and opt.state in ENGAGED_STATES:
            cfg = self.get_config()
            opt.configure(config=cfg['config'], signals=cfg['signals'])   # motors: only when stopped

    # ------------------------------------------------------------------ actions
    def _check(self, cfg) -> bool:
        if not cfg['motors']:
            warn(self, 'Optimiseur', 'Cocher au moins UN moteur.')
            return False
        if not cfg['signals']:
            warn(self, 'Optimiseur', 'Cocher au moins UN signal objectif.')
            return False
        return True

    def _on_start(self):
        cfg = self.get_config()
        if self._check(cfg):
            ok, msg = self.app.start_optimizer(cfg, single_shot=False)
            if not ok:
                warn(self, 'Optimiseur', 'Démarrage impossible :\n\n' + msg.replace(' ; ', '\n'))

    def _on_now(self):
        opt = self.app.optimizer
        if opt is not None and opt.state == AutoState.ACTIVE and not opt.single_shot:
            if not opt.trigger_now(self.app.core.clock.now()):
                info(self, 'Optimiseur', 'Une passe est déjà en cours.')
            return
        if opt is not None and opt.state in ENGAGED_STATES:
            info(self, 'Optimiseur', 'L’optimiseur est déjà engagé.')
            return
        cfg = self.get_config()
        if self._check(cfg):
            ok, msg = self.app.start_optimizer(cfg, single_shot=True)
            if not ok:
                warn(self, 'Optimiseur', 'Démarrage impossible :\n\n' + msg.replace(' ; ', '\n'))

    def _on_full(self):
        opt = self.app.optimizer
        if opt is not None and opt.state in ENGAGED_STATES:
            warn(self, 'Occupé', 'Arrêter d’abord l’optimiseur (sinon les deux se disputeraient les moteurs).')
            return
        cfg = self.get_config()
        if not self._check(cfg):
            return
        if not ask_yes_no(self, 'Optimisation complète',
                          'Recherche agressive : sonde au pas max de chaque moteur (borné), plus d’itérations, '
                          'bande morte réduite. Les moteurs bougent plus qu’une passe normale.\n\nContinuer ?'):
            return
        cfg['config']['full_optimization'] = True
        ok, msg = self.app.start_optimizer(cfg, single_shot=True)
        if not ok:
            warn(self, 'Optimiseur', 'Démarrage impossible :\n\n' + msg.replace(' ; ', '\n'))

    def _on_resume(self):
        ok, msg = self.app.core.resume('OPT')
        if not ok:
            warn(self, 'Optimiseur', 'Reprise refusée :\n\n' + msg.replace(' ; ', '\n'))

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
                warn(self, 'Cible', f'{role} : {why}')
                continue
            self.table_signals.cellWidget(r, 5).setValue(self.app.core.signals.value(ch))
            self.table_signals.cellWidget(r, 6).setChecked(False)
            n += 1
        if n == 0:
            info(self, 'Cible', 'Aucun signal coché en mode « Target Point ».')

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        opt = self.app.optimizer
        state = opt.state if opt is not None else AutoState.OFF
        self.state.set_state(state, opt.reason if opt else '', prefix='Optimiseur : ')
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
            self.countdown.setText('EN COURS' if opt.in_pass else '--:--')
        else:
            m, s = divmod(int(left), 60)
            self.countdown.setText(f'{m:02d}:{s:02d}')
        extra = f' · passes : {opt.pass_count} · itération : {opt.iteration}'
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
