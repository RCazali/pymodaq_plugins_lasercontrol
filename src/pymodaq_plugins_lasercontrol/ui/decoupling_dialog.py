"""Decoupling of several PIDs (PID tab › "🔀 Decouple PIDs…").

Non-modal window: choose the PIDs (one signal each), measure every motor of these PIDs on every signal
(one motor at a time, back to its start position), then give each PID the combination of motors that
moves only its own signal (engines/decoupling.py). Nothing changes in the PIDs before "Apply".
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from qtpy import QtCore, QtWidgets

from ..core.states import ENGAGED_STATES
from ..engines.calibration import CalibrationResult
from ..engines.decoupling import DecouplingResult, decouple, format_matrix
from .widgets import ask_yes_no, mono_font, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp
    from .pid_tab import PIDTab


class DecouplingDialog(QtWidgets.QDialog):
    def __init__(self, app: 'LaserControlApp', pid_tab: 'PIDTab'):
        super().__init__(pid_tab)
        self.app = app
        self.pid_tab = pid_tab
        self.setWindowTitle('Decouple PIDs')
        self.setModal(False)
        self.resize(820, 560)
        self.result: Optional[DecouplingResult] = None
        self._run: List[Tuple[str, str]] = []                 # (pid id, signal) of the measured run
        self._results_by: Dict[str, Dict[str, CalibrationResult]] = {}
        self._collected = None
        v = QtWidgets.QVBoxLayout(self)
        help_lbl = QtWidgets.QLabel(
            'Tick the PIDs to decouple (one signal each, at least as many motors as signals in total). '
            '<b>Measure</b> moves every motor of these PIDs by small bipolar steps, one at a time, and '
            'records every signal; the motors come back to their start positions. <b>Apply</b> gives each '
            'PID the combination of motors that moves only its own signal, and the loop settings (sign, Kp, '
            'deadband, settle) of that combination. Any action on a motor stops the measurement; closing '
            'this window does not.')
        help_lbl.setWordWrap(True)
        v.addWidget(help_lbl)
        self.list = QtWidgets.QListWidget()
        self.list.setMaximumHeight(130)
        self.list.itemChanged.connect(self._on_selection_changed)
        v.addWidget(self.list)
        self.info = QtWidgets.QLabel('')
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Cycles per motor:'))
        self.cycles = QtWidgets.QSpinBox()
        self.cycles.setRange(1, 20)
        self.cycles.setValue(3)
        self.cycles.setToolTip('Bipolar cycles per motor (4 moves each). More cycles = smaller errors on the '
                               'matrix (∝ 1/√cycles) but a longer measurement: raise it when the report says '
                               'that the errors allow a large remaining coupling')
        row.addWidget(self.cycles)
        self.btn_measure = styled_button('🎯 Measure', '#8e44ad')
        self.btn_apply = styled_button('✔ Apply weights', '#27ae60')
        self.btn_close = styled_button('Close', None)
        self.btn_measure.clicked.connect(self._on_measure)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_measure)
        row.addWidget(self.btn_apply)
        row.addStretch()
        row.addWidget(self.btn_close)
        v.addLayout(row)
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(mono_font(10, bold=False))
        self.report.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        v.addWidget(self.report, 1)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ------------------------------------------------------------------ selection
    def reload(self, checked: Optional[List[str]] = None):
        keep = set(checked or []) | set(self.selected_ids())
        self.list.blockSignals(True)
        self.list.clear()
        for pid_id in self.pid_tab._order:
            w = self.pid_tab.widgets[pid_id]
            cfg = w.get_config()
            motors = ', '.join(f'{r}×{wt:+g}' for r, wt in cfg['motors'].items()) or '—'
            it = QtWidgets.QListWidgetItem(f"{w.label()}   —   {cfg['signal'] or '(no signal)'} → {motors}")
            it.setData(QtCore.Qt.UserRole, pid_id)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if pid_id in keep else QtCore.Qt.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._on_selection_changed()

    def selected_ids(self) -> List[str]:
        return [self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.Checked]

    def set_selected(self, ids: List[str]):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(QtCore.Qt.Checked if it.data(QtCore.Qt.UserRole) in ids else QtCore.Qt.Unchecked)

    def _configs(self, ids: List[str]) -> List[dict]:
        return [self.pid_tab.widgets[i].get_config() for i in ids if i in self.pid_tab.widgets]

    def _on_selection_changed(self, *args):
        cfgs = self._configs(self.selected_ids())
        signals = [c['signal'] for c in cfgs]
        motors = list(dict.fromkeys(r for c in cfgs for r in c['motors']))
        text = (f"Signals: {', '.join(s or '—' for s in signals) or '—'}    Motors measured: "
                f"{', '.join(motors) or '—'}")
        if len(cfgs) >= 2 and len(motors) < len(signals):
            text += (f"\n⚠ {len(motors)} motors for {len(signals)} signals: add motors to the PIDs "
                     f"(➕ Add motor) so that each PID can use every motor of the group")
        self.info.setText(text)

    # ------------------------------------------------------------------ measure
    def _on_measure(self):
        cal = self.app.decoupling()
        if cal is not None and cal.state in ENGAGED_STATES:
            self.app.core.stop(cal.id, 'decoupling stopped')
            return
        ids = self.selected_ids()
        cfgs = self._configs(ids)
        motors = list(dict.fromkeys(r for c in cfgs for r in c['motors']))
        if not ask_yes_no(self, 'Decouple PIDs',
                          f"Measure {', '.join(motors)} on {', '.join(c['signal'] for c in cfgs)}?\n\n"
                          f"Each motor moves by small steps (both directions, at most its max step) while the "
                          f"others stay still, then comes back to its start position. The PIDs are not "
                          f"changed until you click Apply."):
            return
        ok, msg = self.app.start_decoupling(cfgs, cycles=self.cycles.value())
        if not ok:
            warn(self, 'Decouple PIDs', 'Cannot start the measurement:\n\n' + msg.replace(' ; ', '\n'))
            return
        self._run = [(c['id'], c['signal']) for c in cfgs]
        self.result = None
        self.report.setPlainText('Measuring…')

    def _collect(self, cal):
        self._collected = cal
        self._results_by = {s: dict(r) for s, r in cal.results_by.items()}
        if not cal.completed:
            self.result = None
            self.report.setPlainText(f'Measurement stopped: {cal.reason or "aborted"} — nothing changed.')
            return
        if not self._run:                          # run started elsewhere (e.g. before a reopen)
            by_signal = {w.get_config()['signal']: p for p, w in self.pid_tab.widgets.items()}
            self._run = [(by_signal.get(s, ''), s) for s in cal.signals]
        self.result = decouple(self._results_by, cal.signals, cal.roles)
        current = {s: self.pid_tab.widgets[p].motor_weights() for p, s in self._run if p in self.pid_tab.widgets}
        self.report.setPlainText(format_matrix(self.result, current))

    def refresh(self):
        if not self.isVisible():
            return
        cal = self.app.decoupling()
        running = cal is not None and cal.state in ENGAGED_STATES
        if cal is not None and not running and cal is not self._collected:
            self._collect(cal)
        if running:
            done, total = cal.progress()
            self.btn_measure.setText(f'⏹ Stop measurement ({min(done + 1, total)}/{total})')
        else:
            self.btn_measure.setText('🎯 Measure')
        engaged = any(self._pid_engaged(p) for p in self.selected_ids())
        self.btn_measure.setEnabled(running or (len(self.selected_ids()) >= 2 and not engaged
                                                and not self.app.core.estop))
        self.btn_apply.setEnabled(not running and self.result is not None and self.result.ok
                                  and not any(self._pid_engaged(p) for p, _ in self._run))
        self.list.setEnabled(not running)

    def _pid_engaged(self, pid_id: str) -> bool:
        a = self.app.core.get(f'PID:{pid_id}')
        return a is not None and a.state in ENGAGED_STATES

    # ------------------------------------------------------------------ apply
    def proposals(self) -> Tuple[Optional[list], List[str]]:
        """[(widget, weights, results, settings)] for every PID of the run, or (None, reasons)."""
        out, problems = [], []
        for pid_id, signal in self._run:
            w = self.pid_tab.widgets.get(pid_id)
            if w is None or w.get_config()['signal'] != signal:
                problems.append(f'{pid_id}: removed or its signal changed since the measurement')
                continue
            weights = self.result.weights[signal]
            results = self._results_by.get(signal, {})
            settings, notes = w.decoupling_proposal(weights, results)
            if settings is None:
                problems.append(f"{w.label()}: " + '; '.join(notes))
                continue
            out.append((w, weights, results, settings))
        return (None, problems) if problems else (out, [])

    def _on_apply(self):
        if self.result is None or not self.result.ok:
            return
        props, problems = self.proposals()
        if props is None:
            warn(self, 'Decouple PIDs', 'Cannot apply:\n\n' + '\n'.join(problems))
            return
        lines = []
        for w, weights, _, st in props:
            comb = ', '.join(f'{r}×{v:+.4g}' for r, v in weights.items() if v)
            lines.append(f"{w.label()}: {comb}\n    sign {st['sign_kp']}, Kp {st['kp']:.4g}, "
                         f"deadband {st['deadband']:.3g}, settle {st['settle_s']:.3g} s")
        if not ask_yes_no(self, 'Apply the decoupling', '\n'.join(lines) + '\n\nApply these settings?'):
            return
        for w, weights, results, st in props:
            w.apply_decoupling(weights, results, st)
        self.app.preset_dirty = True
        self.pid_tab.refresh()
        self.reload()
        self.report.appendPlainText('\n✔ Applied.')

    def closeEvent(self, e):
        self._timer.stop()
        super().closeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()
