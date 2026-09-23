"""PID tab (cahier des charges T5.5): several independent PIDs, each regulating one 0D signal with a
linear combination of motors. Shows the state (ACTIVE / PAUSED / HELD + reason), the error, the
P / I / D contributions and the displacement of every motor versus its S4 envelope.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import pyqtgraph as pg
from qtpy import QtCore, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.states import AutoState, ENGAGED_STATES
from ..core.roles import sanitize_role_name
from ..engines.pid import PIDSettings
from ..presets.schema import default_pid
from .widgets import (EnvelopeGauge, StateBadge, ask_yes_no, fmt, info, reset_rows, set_combo_items,
                      styled_button, warn)

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp


def pid_params() -> list:
    return [
        {'title': 'Nom affiché :', 'name': 'label', 'type': 'str', 'value': '',
         'tip': 'Nom court, aussi utilisé pour le bouton de l’onglet Alignement'},
        {'title': 'Bouton dans Alignement :', 'name': 'show_in_alignment', 'type': 'bool', 'value': False,
         'tip': 'Affiche un bouton sous les moteurs de l’onglet Alignement : un clic démarre, un autre arrête '
                '(sans confirmation), un clic sur un PID gelé le reprend'},
        {'title': 'Stabilisation :', 'name': 'in_stabilization', 'type': 'bool', 'value': True,
         'tip': 'Démarré / arrêté par les boutons « Activer / Désactiver la stabilisation » (Vue d’ensemble)'},
        {'title': 'Consigne :', 'name': 'setpoint', 'type': 'float', 'value': 0.0,
         'tip': 'Valeur visée du signal (unités du signal)'},
        {'title': 'Mode consigne :', 'name': 'setpoint_mode', 'type': 'list', 'limits': ['fixed', 'capture'],
         'value': 'fixed', 'tip': 'capture : la consigne prend la valeur actuelle du signal à chaque démarrage'},
        {'title': 'Kp :', 'name': 'kp', 'type': 'float', 'value': 0.1, 'min': 0.0,
         'tip': 'Gain proportionnel (unités moteur par unité de signal). Typique : 0.05 – 1'},
        {'title': 'Signe Kp :', 'name': 'sign_kp', 'type': 'list', 'limits': ['+', '-'], 'value': '+',
         'tip': 'Une erreur de signe fait diverger la boucle : les enveloppes S4/S5 limitent alors les dégâts'},
        {'title': 'Ki :', 'name': 'ki', 'type': 'float', 'value': 0.01, 'min': 0.0,
         'tip': 'Gain intégral (par seconde). Anti-windup : |Ki·intégrale| ≤ pas max'},
        {'title': 'Signe Ki :', 'name': 'sign_ki', 'type': 'list', 'limits': ['+', '-'], 'value': '+'},
        {'title': 'Kd :', 'name': 'kd', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': 'Gain dérivé (s). Souvent 0 à 2 Hz avec un signal bruité'},
        {'title': 'Signe Kd :', 'name': 'sign_kd', 'type': 'list', 'limits': ['+', '-'], 'value': '+'},
        {'title': 'Pas max / itération :', 'name': 'max_step', 'type': 'float', 'value': 1.0, 'min': 0.0,
         'tip': 'Borne chaque moteur APRÈS pondération (le max_step du rôle, S3, s’applique en plus)'},
        {'title': 'Période (s) :', 'name': 'period_s', 'type': 'float', 'value': 0.5, 'min': 0.05,
         'tip': '0.5 s = 2 Hz. Une itération est sautée si un moteur bouge encore (pas d’empilement)'},
        {'title': 'Bande morte (erreur) :', 'name': 'deadband', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': '|erreur| inférieure : aucun mouvement, intégrale gelée'},
        {'title': 'Attente après mvt (s) :', 'name': 'settle_s', 'type': 'float', 'value': 0.0, 'min': 0.0,
         'tip': 'N’utilise qu’une mesure reçue au moins ce temps après la fin du dernier mouvement'},
        {'title': 'Confirmer chaque mvt :', 'name': 'confirm_moves', 'type': 'bool', 'value': False,
         'tip': 'Boîte de dialogue avant chaque déplacement (le PID est en PAUSE pendant la question)'},
    ]


class SinglePIDWidget(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, app: 'LaserControlApp', cfg: dict, parent=None):
        super().__init__(parent)
        self.app = app
        self.pid_id = cfg['id']
        self._loading = False
        self._motor_gauges: Dict[str, EnvelopeGauge] = {}
        self._motor_labels: Dict[str, QtWidgets.QLabel] = {}
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)

        # 1. signal + motors
        left = QtWidgets.QGroupBox('1. Signal et combinaison de moteurs')
        ll = QtWidgets.QVBoxLayout(left)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Signal (rôle 0D) :'))
        self.combo_signal = QtWidgets.QComboBox()
        self.combo_signal.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.combo_signal.currentTextChanged.connect(self._on_changed)
        row.addWidget(self.combo_signal, 1)
        ll.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Moteur (rôle)', 'Poids', 'Δ engag.', 'Enveloppe'])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.table.setColumnWidth(1, 90)
        self.table.horizontalHeaderItem(2).setToolTip('Déplacement depuis l’engagement (centre de l’enveloppe S4)')
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._on_changed)
        ll.addWidget(self.table)
        main.addWidget(left, 4)

        # 2. settings + actions
        center = QtWidgets.QGroupBox('2. Réglages')
        cl = QtWidgets.QVBoxLayout(center)
        self.settings = Parameter.create(name=f'pid_{self.pid_id}', type='group', children=pid_params())
        self.tree = ParameterTree()
        self.tree.setParameters(self.settings, showTop=False)
        self.tree.setMinimumWidth(340)
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 190)          # the value column must stay visible
        self.settings.sigTreeStateChanged.connect(self._on_changed)
        cl.addWidget(self.tree, 1)
        self.state = StateBadge('', font_pt=11)
        cl.addWidget(self.state)
        self.reason = QtWidgets.QLabel('')
        self.reason.setWordWrap(True)
        self.reason.setStyleSheet('color: #c0392b;')
        cl.addWidget(self.reason)
        grid = QtWidgets.QGridLayout()
        self.btn_start = styled_button('▶ Démarrer', '#27ae60')
        self.btn_stop = styled_button('⏹ Arrêter', '#7f8c8d', 'Arrête le PID (aucun mouvement)')
        self.btn_resume = styled_button('⏯ Reprendre', '#2980b9',
                                        'Reprise manuelle après un gel de sécurité (O10-A), intégrale remise à zéro')
        self.btn_capture = styled_button('📍 Consigne = valeur actuelle', None)
        self.btn_ref = styled_button('↺ Nouvelle référence d’enveloppe', None,
                                     'L’enveloppe S4 est recentrée sur les positions actuelles')
        grid.addWidget(self.btn_start, 0, 0)
        grid.addWidget(self.btn_stop, 0, 1)
        grid.addWidget(self.btn_resume, 1, 0, 1, 2)
        grid.addWidget(self.btn_capture, 2, 0, 1, 2)
        grid.addWidget(self.btn_ref, 3, 0, 1, 2)
        cl.addLayout(grid)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(lambda: self.app.core.stop(self.auto_id, 'arrêt manuel'))
        self.btn_resume.clicked.connect(self._on_resume)
        self.btn_capture.clicked.connect(self._on_capture)
        self.btn_ref.clicked.connect(self._on_reset_ref)
        main.addWidget(center, 3)

        # 3. live
        right = QtWidgets.QGroupBox('3. Suivi')
        rl = QtWidgets.QVBoxLayout(right)
        self.values = QtWidgets.QLabel('')
        self.values.setStyleSheet('font-family: Consolas, monospace;')
        self.values.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        rl.addWidget(self.values)
        self.plot = pg.PlotWidget()
        self.plot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel('bottom', 'temps (s)')
        self.plot.addLegend()
        self.curve_pv = self.plot.plot(pen=pg.mkPen('#2ecc71', width=2), name='mesure')
        self.curve_sp = self.plot.plot(pen=pg.mkPen('#e74c3c', width=1.5, style=QtCore.Qt.DashLine), name='consigne')
        rl.addWidget(self.plot, 1)
        main.addWidget(right, 4)
        self.set_hardware()
        self.load_config(cfg)

    @property
    def auto_id(self) -> str:
        return f'PID:{self.pid_id}'

    # ------------------------------------------------------------------ config <-> UI
    def set_hardware(self):
        cfg = self.get_config() if self.table.rowCount() or self.combo_signal.count() else None
        set_combo_items(self.combo_signal, [''] + sorted(self.app.core.roles.signals),
                        cfg['signal'] if cfg else None)
        weights = cfg['motors'] if cfg else {}
        self._fill_motors(weights)

    def _fill_motors(self, weights: Dict[str, float]):
        self._loading = True
        roles = sorted(self.app.core.roles.actuators)
        for r in weights:
            if r not in roles:
                roles.append(r)            # keep a role of the preset missing on this machine
        reset_rows(self.table, len(roles))
        self._motor_gauges = {}
        self._motor_labels = {}
        for i, role in enumerate(roles):
            it = QtWidgets.QTableWidgetItem(role if self.app.core.roles.is_actuator(role) else f'⚠ {role}')
            it.setData(QtCore.Qt.UserRole, role)
            it.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            w = weights.get(role, 0.0)
            it.setCheckState(QtCore.Qt.Checked if w else QtCore.Qt.Unchecked)
            self.table.setItem(i, 0, it)
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(-1000, 1000)
            sp.setDecimals(4)
            sp.setSingleStep(0.1)
            sp.setValue(w if w else 1.0)
            sp.setFocusPolicy(QtCore.Qt.ClickFocus)
            sp.valueChanged.connect(self._on_changed)
            self.table.setCellWidget(i, 1, sp)
            lbl = QtWidgets.QLabel('')
            self.table.setCellWidget(i, 2, lbl)
            g = EnvelopeGauge()
            self.table.setCellWidget(i, 3, g)
            self._motor_gauges[role] = g
            self._motor_labels[role] = lbl
        self._loading = False

    def get_config(self) -> dict:
        motors = {}
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            if it is not None and it.checkState() == QtCore.Qt.Checked:
                motors[it.data(QtCore.Qt.UserRole)] = float(self.table.cellWidget(i, 1).value())
        s = {c.name(): c.value() for c in self.settings.children()}
        settings = PIDSettings.from_dict(s).to_dict()
        return {'id': self.pid_id, 'label': s.get('label', ''), 'in_stabilization': bool(s.get('in_stabilization')),
                'show_in_alignment': bool(s.get('show_in_alignment')), 'signal': self.combo_signal.currentText(), 'motors': motors, 'settings': settings}

    def load_config(self, cfg: dict):
        self._loading = True
        try:
            set_combo_items(self.combo_signal, [''] + sorted(self.app.core.roles.signals), cfg.get('signal', ''))
            self._fill_motors(cfg.get('motors', {}) or {})
            self._loading = True
            st = dict(cfg.get('settings', {}) or {})
            st['label'] = cfg.get('label', '')
            st['in_stabilization'] = cfg.get('in_stabilization', True)
            st['show_in_alignment'] = cfg.get('show_in_alignment', False)
            for c in self.settings.children():
                if c.name() in st and st[c.name()] is not None:
                    try:
                        c.setValue(st[c.name()])
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
            a.set_settings(PIDSettings.from_dict(cfg['settings']))   # live gains/setpoint update
            a.label = cfg['label'] or f'PID {self.pid_id}'
            a.in_stabilization = cfg['in_stabilization']
        self.changed.emit()

    # ------------------------------------------------------------------ actions
    def _on_start(self):
        cfg = self.get_config()
        ok, msg = self.app.start_pid(cfg)
        if not ok:
            warn(self, f'PID {self.pid_id}', f'Démarrage impossible :\n\n' + msg.replace(' ; ', '\n'))

    def _on_resume(self):
        ok, msg = self.app.core.resume(self.auto_id)
        if not ok:
            warn(self, f'PID {self.pid_id}', f'Reprise refusée :\n\n' + msg.replace(' ; ', '\n'))

    def _on_capture(self):
        role = self.combo_signal.currentText()
        ch = self.app.core.roles.channel_for(role)
        ok, why = self.app.core.signals.freshness(ch)
        if not ok:
            warn(self, 'Consigne', f'Signal indisponible : {why}')
            return
        self.settings.child('setpoint').setValue(self.app.core.signals.value(ch))

    def _on_reset_ref(self):
        if ask_yes_no(self, 'Enveloppe', 'Recentrer l’enveloppe S4 de ce PID sur les positions actuelles ?'):
            self.app.core.reset_reference(self.auto_id)

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        a = self.app.core.get(self.auto_id)
        state = a.state if a is not None else AutoState.OFF
        label = self.settings.child('label').value() or self.pid_id
        self.state.set_state(state, a.reason if a else '', prefix=f'{label} : ')
        self.reason.setText(a.reason if a is not None and state in (AutoState.HELD, AutoState.PAUSED,
                                                                     AutoState.ERROR) else '')
        engaged = state in ENGAGED_STATES
        self.btn_start.setEnabled(not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        self.btn_resume.setEnabled(state in (AutoState.HELD,) or
                                   (state == AutoState.PAUSED and a is not None and a.pause_kind != 'confirm'))
        self.btn_ref.setEnabled(engaged)
        self.table.setEnabled(not engaged)
        self.combo_signal.setEnabled(not engaged)
        if a is None:
            self.values.setText('')
            return
        t = a.controller.last
        self.values.setText(
            f'mesure {fmt(t.pv, 6):>10}   consigne {fmt(t.setpoint, 6):>10}   erreur {fmt(t.error, 4):>9}\n'
            f'P {fmt(t.p, 4):>9}   I {fmt(t.i, 4):>9}   D {fmt(t.d, 4):>9}   sortie {fmt(t.output, 4):>9}\n'
            f'itérations {a.iterations}   sautées (moteur occupé) {a.skipped_busy}')
        core = self.app.core
        for role, g in self._motor_gauges.items():
            m = core.roles.module_for(role)
            pos = core.broker.position(m) if m else None
            ref = core.safety.reference(a.id, m) if m else None
            if engaged and m and ref is not None:
                lo, hi = core.safety.allowed_range(a.id, role, m, a.policy())
                g.set_values(lo if math.isfinite(lo) else None, hi if math.isfinite(hi) else None, pos, ref)
                self._motor_labels[role].setText(f'{pos - ref:+.4g}' if pos is not None else '—')
            else:
                g.set_values(None, None, pos, None)
                self._motor_labels[role].setText('')
        if a.trace:
            now = time.time()
            tr = list(a.trace)
            ts = [p[0] - now for p in tr]
            self.curve_pv.setData(ts, [p[1] for p in tr])
            self.curve_sp.setData(ts, [p[2] for p in tr])


class PIDTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.widgets: Dict[str, SinglePIDWidget] = {}
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = styled_button('➕ Ajouter un PID', None)
        self.btn_rename = styled_button('✎ Renommer', None)
        self.btn_remove = styled_button('❌ Supprimer', None)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_remove.clicked.connect(self._on_remove)
        for b in (self.btn_add, self.btn_rename, self.btn_remove):
            bar.addWidget(b)
        bar.addStretch()
        hint = QtWidgets.QLabel('<i>Les réglages sont enregistrés dans le preset (onglet Presets).</i>')
        bar.addWidget(hint)
        lay.addLayout(bar)
        self.tabs = QtWidgets.QTabWidget()
        lay.addWidget(self.tabs)

    def _add_widget(self, cfg: dict) -> SinglePIDWidget:
        w = SinglePIDWidget(self.app, cfg)
        self.widgets[cfg['id']] = w
        self.tabs.addTab(w, cfg.get('label') or cfg['id'])
        return w

    def _on_add(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'Nouveau PID', 'Identifiant (ex. TipTilt_H) :')
        if not ok or not name.strip():
            return
        pid_id = sanitize_role_name(name)
        if pid_id in self.widgets:
            warn(self, 'PID', f'{pid_id} existe déjà')
            return
        self._add_widget(default_pid(pid_id))
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.app.preset_dirty = True

    def _current(self) -> Optional[SinglePIDWidget]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, SinglePIDWidget) else None

    def _on_rename(self):
        w = self._current()
        if w is None:
            return
        label, ok = QtWidgets.QInputDialog.getText(self, 'Renommer', 'Nom affiché :',
                                                   text=w.settings.child('label').value() or w.pid_id)
        if ok:
            w.settings.child('label').setValue(label.strip())
            self.tabs.setTabText(self.tabs.currentIndex(), label.strip() or w.pid_id)

    def _on_remove(self):
        w = self._current()
        if w is None:
            return
        a = self.app.core.get(w.auto_id)
        if a is not None and a.state in ENGAGED_STATES:
            warn(self, 'PID', 'Arrêter le PID avant de le supprimer.')
            return
        if not ask_yes_no(self, 'Supprimer', f'Supprimer le PID {w.pid_id} du preset ?'):
            return
        self.app.core.remove_automation(w.auto_id)
        self.widgets.pop(w.pid_id, None)
        self.tabs.removeTab(self.tabs.currentIndex())
        w.deleteLater()
        self.app.preset_dirty = True

    def select(self, auto_id: str) -> bool:
        pid_id = auto_id.split(':', 1)[-1]
        w = self.widgets.get(pid_id)
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
        for cfg in preset.get('pids', []):
            self._add_widget(cfg)

    def write_to_preset(self, preset: dict):
        preset['pids'] = [w.get_config() for w in self.widgets.values()]

    def configs(self) -> List[dict]:
        return [w.get_config() for w in self.widgets.values()]

    def _ordered(self) -> List[SinglePIDWidget]:
        return [w for w in (self.tabs.widget(i) for i in range(self.tabs.count())) if isinstance(w, SinglePIDWidget)]

    def quick_items(self) -> List[dict]:
        """PIDs shown as buttons in the Alignment tab (cheap: read at every UI refresh)."""
        out = []
        for w in self._ordered():
            if w.settings.child('show_in_alignment').value():
                out.append({'id': w.auto_id, 'kind': 'pid', 'key': w.pid_id,
                            'label': w.settings.child('label').value() or w.pid_id, 'action': ''})
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
            if isinstance(wid, SinglePIDWidget):
                a = self.app.core.get(wid.auto_id)
                st = a.state.value if a is not None else 'OFF'
                label = wid.settings.child('label').value() or wid.pid_id
                self.tabs.setTabText(i, f'{label} [{st}]')
