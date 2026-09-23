"""Recording tab (T5.6, L7): HDF5 session, periodic snapshots, bursts around events, B triggers."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from qtpy import QtCore, QtGui, QtWidgets

from ..recording import BURST_TRIGGERS, KIND_MANUAL
from .widgets import CheckList, dspin, styled_button

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp


def open_folder(path: Path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))


class RecordingTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self._loading = False
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QtWidgets.QHBoxLayout()
        self.btn_rec = styled_button('● Démarrer l’enregistrement', '#c0392b')
        self.btn_rec.clicked.connect(self._on_rec)
        top.addWidget(self.btn_rec)
        self.btn_snap = styled_button('📷 Snapshot manuel', None, 'Un snapshot immédiat dans le fichier')
        self.btn_snap.clicked.connect(lambda: self.app.recorder.snapshot_now(KIND_MANUAL))
        top.addWidget(self.btn_snap)
        self.btn_folder = styled_button('📂 Dossier', None)
        self.btn_folder.clicked.connect(lambda: open_folder(self.app.paths.recordings))
        top.addWidget(self.btn_folder)
        top.addStretch()
        lay.addLayout(top)
        self.status = QtWidgets.QLabel('')
        self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        g = QtWidgets.QGroupBox('Réglages (enregistrés dans le preset)')
        form = QtWidgets.QFormLayout(g)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
        self.periodic = QtWidgets.QCheckBox('snapshots périodiques, période (s) :')
        self.period = dspin(10.0, 0.1, 86400, 2, tip='période des snapshots (s)')
        form.addRow(self.periodic, self.period)
        self.raw = QtWidgets.QCheckBox('enregistrer aussi les canaux 0D sans rôle')
        form.addRow('', self.raw)
        self.burst = QtWidgets.QCheckBox('rafales sur événement')
        form.addRow('', self.burst)
        self.burst_events = CheckList()
        self.burst_events.setMaximumHeight(90)
        self.burst_events.set_items(list(BURST_TRIGGERS))
        for i in range(self.burst_events.count()):
            it = self.burst_events.item(i)
            it.setToolTip(BURST_TRIGGERS[it.text()])
        form.addRow('Déclencheurs :', self.burst_events)
        self.pre = dspin(60.0, 0, 3600, 1, tip='durée du tampon circulaire enregistré AVANT l’événement (s)')
        self.pre_rate = dspin(2.0, 0.1, 50, 2, tip='fréquence du tampon avant événement (Hz)')
        self.duration = dspin(30.0, 0.1, 3600, 1, tip='durée de la rafale APRÈS l’événement (s)')
        self.interval = dspin(1.0, 0.05, 600, 2, tip='intervalle entre snapshots pendant la rafale (s)')
        form.addRow('Avant événement (s) :', self.pre)
        form.addRow('Fréquence avant (Hz) :', self.pre_rate)
        form.addRow('Durée rafale (s) :', self.duration)
        form.addRow('Intervalle rafale (s) :', self.interval)
        self.trigger_events = CheckList()
        self.trigger_events.setMaximumHeight(90)
        self.trigger_events.set_items(list(BURST_TRIGGERS))
        folder = self.app.paths.trigger
        form.addRow(QtWidgets.QLabel(f'Fichiers déclencheurs pour l’ordi B (O8) :\n'
                                     f'{folder if folder else "désactivé (config : paths.event_trigger_folder)"}'),
                    self.trigger_events)
        lay.addWidget(g)
        help_lbl = QtWidgets.QLabel(
            '<i>Un fichier HDF5 par session (rotation par taille/durée). Les snapshots contiennent tous les rôles, '
            'tous les moteurs, les états et variables internes des automatismes, la sécurité et l’état ML. '
            'Lecture : <code>from pymodaq_plugins_lasercontrol.recording import load_session</code>.</i>')
        help_lbl.setWordWrap(True)
        lay.addWidget(help_lbl)
        lay.addStretch()
        for w in (self.periodic, self.raw, self.burst):
            w.toggled.connect(self._on_changed)
        for w in (self.period, self.pre, self.pre_rate, self.duration, self.interval):
            w.valueChanged.connect(self._on_changed)
        self.burst_events.changed.connect(self._on_changed)
        self.trigger_events.changed.connect(self._on_changed)

    def settings(self) -> dict:
        return {'periodic_enabled': self.periodic.isChecked(), 'period_s': self.period.value(),
                'record_raw_channels': self.raw.isChecked(), 'burst_enabled': self.burst.isChecked(),
                'burst_events': self.burst_events.checked(), 'pre_buffer_s': self.pre.value(),
                'pre_rate_hz': self.pre_rate.value(), 'burst_duration_s': self.duration.value(),
                'burst_interval_s': self.interval.value(), 'trigger_events': self.trigger_events.checked()}

    def load_from_preset(self, preset: dict):
        r = preset.get('recording', {})
        self._loading = True
        try:
            self.periodic.setChecked(bool(r.get('periodic_enabled', True)))
            self.period.setValue(float(r.get('period_s', 10)))
            self.raw.setChecked(bool(r.get('record_raw_channels', True)))
            self.burst.setChecked(bool(r.get('burst_enabled', True)))
            self.burst_events.set_items(list(BURST_TRIGGERS), r.get('burst_events', []))
            self.pre.setValue(float(r.get('pre_buffer_s', 60)))
            self.pre_rate.setValue(float(r.get('pre_rate_hz', 2)))
            self.duration.setValue(float(r.get('burst_duration_s', 30)))
            self.interval.setValue(float(r.get('burst_interval_s', 1)))
            self.trigger_events.set_items(list(BURST_TRIGGERS), r.get('trigger_events', []))
        finally:
            self._loading = False

    def write_to_preset(self, preset: dict):
        preset['recording'] = self.settings()

    def _on_changed(self, *args):
        if self._loading:
            return
        self.app.preset_dirty = True
        s = self.settings()
        self.app.recorder.configure(s)
        self.app.triggers.set_triggers(s['trigger_events'])

    def _on_rec(self):
        self.app.toggle_recording(not self.app.recorder.active)
        self.refresh()

    def refresh(self):
        rec = self.app.recorder
        self.btn_rec.setText('■ Arrêter l’enregistrement' if rec.active else '● Démarrer l’enregistrement')
        self.btn_snap.setEnabled(rec.active)
        if rec.active and rec.writer is not None:
            w = rec.writer
            txt = (f'<b>Enregistrement en cours</b> : {rec.current_path}<br>'
                   f'snapshots : {w.n_snapshots} · événements : {w.n_events} · rafales en cours : {rec.bursts_running}')
            if w.error:
                txt += f'<br><span style="color:#c0392b">Erreur d’écriture : {w.error}</span>'
        else:
            txt = f'Pas d’enregistrement en cours. Dossier : {self.app.paths.recordings}'
        t = self.app.triggers
        if t.error:
            txt += f'<br><span style="color:#c0392b">Fichier déclencheur : {t.error}</span>'
        elif t.last_file is not None:
            txt += f'<br>Dernier fichier déclencheur : {t.last_file.name}'
        self.status.setText(txt)
