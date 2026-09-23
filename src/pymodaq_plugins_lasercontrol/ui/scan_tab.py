"""1D scan tab (T4.4): scan of one motor role on the broker, several 0D signals recorded, CSV export."""
from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING, Dict

import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from ..core.states import AutoState, ENGAGED_STATES
from ..engines.scan import Scan1DAutomation, scan_positions
from .widgets import CheckList, StateBadge, dspin, ispin, set_combo_items, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

COLORS = ['#00e676', '#ff9100', '#29b6f6', '#ab47bc', '#ff1744', '#ffff00']


class ScanTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.scan: Scan1DAutomation = None
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._last_results = []
        self._last_meta = {}
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)
        left = QtWidgets.QVBoxLayout()
        form = QtWidgets.QFormLayout()
        self.combo_role = QtWidgets.QComboBox()
        self.combo_role.setFocusPolicy(QtCore.Qt.ClickFocus)
        form.addRow('Moteur (rôle) :', self.combo_role)
        self.relative = QtWidgets.QCheckBox('relatif à la position actuelle')
        self.relative.setChecked(True)
        self.relative.setToolTip('Recommandé : le scan se fait autour de la position de départ')
        form.addRow('', self.relative)
        self.start = dspin(-10.0, tip='début du scan')
        self.stop = dspin(10.0, tip='fin du scan')
        self.step = dspin(1.0, 1e-9, 1e9, tip='pas du scan (> 0)')
        self.dwell = dspin(0.2, 0, 3600, 3, tip='attente après chaque mouvement (s)')
        self.samples = ispin(1, 1, 1000, 'trames moyennées par point')
        form.addRow('Début :', self.start)
        form.addRow('Fin :', self.stop)
        form.addRow('Pas :', self.step)
        form.addRow('Attente (s) :', self.dwell)
        form.addRow('Trames / point :', self.samples)
        self.ret = QtWidgets.QCheckBox('retour à la position initiale à la fin')
        self.ret.setChecked(True)
        form.addRow('', self.ret)
        self.check_int = QtWidgets.QCheckBox('geler si intensité sous le seuil (S2)')
        form.addRow('', self.check_int)
        left.addLayout(form)
        left.addWidget(QtWidgets.QLabel('<b>Signaux enregistrés</b>'))
        self.channels = CheckList()
        left.addWidget(self.channels, 1)
        self.info = QtWidgets.QLabel('')
        self.info.setWordWrap(True)
        left.addWidget(self.info)
        row = QtWidgets.QHBoxLayout()
        self.btn_start = styled_button('▶ Démarrer le scan', '#27ae60')
        self.btn_stop = styled_button('⏹ Arrêter', '#7f8c8d', 'Arrête le scan sur place (aucun retour)')
        self.btn_save = styled_button('💾 CSV', None, 'Enregistrer les données du dernier scan')
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.btn_save)
        left.addLayout(row)
        self.state = StateBadge('', font_pt=10)
        left.addWidget(self.state)
        main.addLayout(left, 1)
        self.plot = pg.PlotWidget(title='Scan 1D')
        self.plot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel('bottom', 'position')
        self.legend = self.plot.addLegend()
        main.addWidget(self.plot, 2)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(lambda: self.app.core.stop('SCAN', 'arrêt manuel'))
        self.btn_save.clicked.connect(self._on_save)
        for w in (self.start, self.stop, self.step):
            w.valueChanged.connect(self._update_info)
        self.set_hardware()

    def set_hardware(self):
        set_combo_items(self.combo_role, sorted(self.app.core.roles.actuators))
        checked = self.channels.checked()
        self.channels.set_items(sorted(self.app.core.roles.signals), checked)
        self._update_info()

    def _update_info(self):
        try:
            n = len(scan_positions(self.start.value(), self.stop.value(), self.step.value()))
            self.info.setText(f'{n} points')
        except ValueError as e:
            self.info.setText(str(e))

    # ------------------------------------------------------------------ preset
    def load_from_preset(self, preset: dict):
        sc = preset.get('scan', {})
        set_combo_items(self.combo_role, sorted(self.app.core.roles.actuators), sc.get('role') or None)
        self.start.setValue(float(sc.get('start', -10)))
        self.stop.setValue(float(sc.get('stop', 10)))
        self.step.setValue(float(sc.get('step', 1)))
        self.dwell.setValue(float(sc.get('dwell', 0.2)))
        self.samples.setValue(int(sc.get('samples', 1)))
        self.relative.setChecked(bool(sc.get('relative', True)))
        self.ret.setChecked(bool(sc.get('return_to_start', True)))
        self.check_int.setChecked(bool(sc.get('check_intensity', False)))
        self.channels.set_items(sorted(self.app.core.roles.signals), sc.get('channels', []))

    def write_to_preset(self, preset: dict):
        preset['scan'] = {'role': self.combo_role.currentText(), 'start': self.start.value(),
                          'stop': self.stop.value(), 'step': self.step.value(), 'dwell': self.dwell.value(),
                          'samples': self.samples.value(), 'channels': self.channels.checked(),
                          'relative': self.relative.isChecked(), 'return_to_start': self.ret.isChecked(),
                          'check_intensity': self.check_int.isChecked()}

    # ------------------------------------------------------------------ actions
    def _on_start(self):
        role = self.combo_role.currentText()
        if not role:
            warn(self, 'Scan', 'Choisir un moteur.')
            return
        old = self.app.core.get('SCAN')
        if old is not None and old.state in ENGAGED_STATES:
            return
        try:
            scan = Scan1DAutomation(role, self.start.value(), self.stop.value(), self.step.value(),
                                    self.dwell.value(), self.samples.value(), self.channels.checked(),
                                    self.relative.isChecked(), self.ret.isChecked(), self.check_int.isChecked())
        except ValueError as e:
            warn(self, 'Scan', str(e))
            return
        if old is not None:
            self.app.core.remove_automation('SCAN')
        self.app.core.add_automation(scan)
        ok, msg = self.app.core.start(scan.id)
        if not ok:
            warn(self, 'Scan', 'Démarrage impossible :\n\n' + msg.replace(' ; ', '\n'))
            return
        self.scan = scan
        self._last_meta = {'role': role, 'module': self.app.core.roles.module_for(role),
                           'start': self.start.value(), 'stop': self.stop.value(), 'step': self.step.value(),
                           'dwell': self.dwell.value(), 'samples': self.samples.value(),
                           'relative': self.relative.isChecked(), 'origin': scan.origin,
                           'preset': self.app.preset.get('name'), 'started': _dt.datetime.now().isoformat()}
        self.plot.clear()
        self.legend = self.plot.addLegend()
        self._curves = {}
        for i, ch in enumerate(scan.channels):
            self._curves[ch] = self.plot.plot(pen=pg.mkPen(COLORS[i % len(COLORS)], width=2), symbol='o',
                                              symbolSize=5, symbolBrush=COLORS[i % len(COLORS)], name=ch)
        self.plot.setLabel('bottom', f'{role} ({self.app.core.broker.units(self.app.core.roles.module_for(role))})')

    def _on_save(self):
        results = self.scan.results if self.scan is not None else []
        if not results:
            warn(self, 'Scan', 'Aucune donnée de scan.')
            return
        stamp = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        default = str(self.app.paths.recordings / f"scan_{self._last_meta.get('role', 'scan')}_{stamp}.csv")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Enregistrer le scan', default, 'CSV (*.csv)')
        if not path:
            return
        chans = list(self.scan.channels)
        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                for k, v in self._last_meta.items():
                    f.write(f'# {k}: {v}\n')
                w = csv.writer(f)
                w.writerow(['position'] + chans)
                for x, readings in results:
                    w.writerow([x] + [readings.get(c, '') for c in chans])
            self.app.core.events.publish('scan', 'SCAN', f'Scan enregistré : {Path(path).name}')
        except Exception as e:
            warn(self, 'Scan', f'Écriture impossible : {e}')

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        a = self.app.core.get('SCAN')
        state = a.state if a is not None else AutoState.OFF
        self.state.set_state(state, a.reason if a else '', prefix='Scan : ')
        engaged = state in ENGAGED_STATES
        self.btn_start.setEnabled(not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        if self.scan is not None and self.scan.results:
            xs = [r[0] for r in self.scan.results]
            for ch, curve in self._curves.items():
                curve.setData(xs, [r[1].get(ch, float('nan')) for r in self.scan.results])
            done, total = self.scan.progress()
            self.info.setText(f'{done}/{total} points')
