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
        self.btn_rec = styled_button('● Start recording', '#c0392b')
        self.btn_rec.clicked.connect(self._on_rec)
        top.addWidget(self.btn_rec)
        self.btn_snap = styled_button('📷 Manual snapshot', None, 'One immediate snapshot in the file')
        self.btn_snap.clicked.connect(lambda: self.app.recorder.snapshot_now(KIND_MANUAL))
        top.addWidget(self.btn_snap)
        self.btn_folder = styled_button('📂 Folder', None)
        self.btn_folder.clicked.connect(lambda: open_folder(self.app.paths.recordings))
        top.addWidget(self.btn_folder)
        top.addStretch()
        lay.addLayout(top)
        self.status = QtWidgets.QLabel('')
        self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        g = QtWidgets.QGroupBox('Settings (saved in the preset)')
        form = QtWidgets.QFormLayout(g)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
        self.periodic = QtWidgets.QCheckBox('periodic snapshots, period (s):')
        self.period = dspin(10.0, 0.1, 86400, 2, tip='snapshot period (s)')
        form.addRow(self.periodic, self.period)
        self.raw = QtWidgets.QCheckBox('also record the 0D channels without a role')
        form.addRow('', self.raw)
        self.burst = QtWidgets.QCheckBox('bursts on event')
        form.addRow('', self.burst)
        self.burst_events = CheckList()
        self.burst_events.setMaximumHeight(90)
        self.burst_events.set_items(list(BURST_TRIGGERS))
        for i in range(self.burst_events.count()):
            it = self.burst_events.item(i)
            it.setToolTip(BURST_TRIGGERS[it.text()])
        form.addRow('Triggers:', self.burst_events)
        self.pre = dspin(60.0, 0, 3600, 1, tip='length of the circular buffer saved BEFORE the event (s)')
        self.pre_rate = dspin(2.0, 0.1, 50, 2, tip='rate of the buffer before the event (Hz)')
        self.duration = dspin(30.0, 0.1, 3600, 1, tip='length of the burst AFTER the event (s)')
        self.interval = dspin(1.0, 0.05, 600, 2, tip='interval between snapshots during the burst (s)')
        form.addRow('Before event (s):', self.pre)
        form.addRow('Rate before (Hz):', self.pre_rate)
        form.addRow('Burst length (s):', self.duration)
        form.addRow('Burst interval (s):', self.interval)
        self.trigger_events = CheckList()
        self.trigger_events.setMaximumHeight(90)
        self.trigger_events.set_items(list(BURST_TRIGGERS))
        folder = self.app.paths.trigger
        form.addRow(QtWidgets.QLabel(f'Trigger files for computer B (O8):\n'
                                     f'{folder if folder else "disabled (config: paths.event_trigger_folder)"}'),
                    self.trigger_events)
        lay.addWidget(g)
        help_lbl = QtWidgets.QLabel(
            '<i>One HDF5 file per session (rotation by size/duration). Snapshots contain every role, '
            'every motor, the states and internal variables of the automations, the safety and the ML state. '
            'Reading: <code>from pymodaq_plugins_lasercontrol.recording import load_session</code>.</i>')
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
        self.btn_rec.setText('■ Stop recording' if rec.active else '● Start recording')
        self.btn_snap.setEnabled(rec.active)
        if rec.active and rec.writer is not None:
            w = rec.writer
            txt = (f'<b>Recording</b>: {rec.current_path}<br>'
                   f'snapshots: {w.n_snapshots} · events: {w.n_events} · bursts running: {rec.bursts_running}')
            if w.error:
                txt += f'<br><span style="color:#c0392b">Write error: {w.error}</span>'
        else:
            txt = f'Not recording. Folder: {self.app.paths.recordings}'
        t = self.app.triggers
        if t.error:
            txt += f'<br><span style="color:#c0392b">Trigger file: {t.error}</span>'
        elif t.last_file is not None:
            txt += f'<br>Last trigger file: {t.last_file.name}'
        self.status.setText(txt)
