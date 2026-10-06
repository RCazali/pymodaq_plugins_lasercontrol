"""Global status bar, always visible (specification T5.1, T3.7, T8.2).

Global state, one light per automation, freshness of the detectors used by the roles, intensity
versus threshold, mode-lock annotation (O6-A), recording, note, shake and the emergency stop.
Large fonts: it must stay readable through the remote screen access.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict

from qtpy import QtCore, QtGui, QtWidgets

from ..core.coordinator import ML_LABELS
from ..core.states import STATE_COLORS, STATE_LABELS, AutoState, GlobalState
from .widgets import Badge, DET_COLORS, Led, StateBadge, fmt, run_dialog, styled_button

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

ML_COLORS = {'unknown': '#7f8c8d', 'ML_ON': '#27ae60', 'ML_OFF': '#c0392b', 'UNSTABLE': '#f39c12'}
#: the state of an automation chip is also given by a symbol (colours are hard to tell apart through goggles)
STATE_MARKS = {AutoState.ARMED: '… ', AutoState.ACTIVE: '▶ ', AutoState.PAUSED: '‖ ', AutoState.HELD: '■ ',
               AutoState.ERROR: '✖ '}


class StatusBar(QtWidgets.QFrame):
    automation_clicked = QtCore.Signal(str)
    CHIP_MAX_W = 190
    CHIP_MIN_W = 110

    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.setObjectName('lcStatusBar')
        self.setStyleSheet('#lcStatusBar { border-bottom: 1px solid rgba(127,127,127,120); }')
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(10)

        self.global_badge = StateBadge('', font_pt=13)
        self.global_badge.setMinimumWidth(170)
        lay.addWidget(self.global_badge)

        # engaged automations: never widen the window (1300 px), the chips share the free space
        self.auto_area = QtWidgets.QWidget()
        self.auto_area.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.auto_box = QtWidgets.QHBoxLayout(self.auto_area)
        self.auto_box.setContentsMargins(0, 0, 0, 0)
        self.auto_box.setSpacing(4)
        self.more = QtWidgets.QLabel('')
        self.more.setStyleSheet('font-weight: bold; border: 2px solid palette(text); border-radius: 4px; '
                                'padding: 1px 4px;')
        self.more.setVisible(False)
        self.auto_box.addWidget(self.more)
        self.auto_box.addStretch()
        lay.addWidget(self.auto_area, 1)
        self._auto_badges: Dict[str, QtWidgets.QPushButton] = {}

        lay.addWidget(self._vline())
        self.det_box = QtWidgets.QHBoxLayout()
        self.det_box.setSpacing(6)
        lay.addLayout(self.det_box)
        self._det_leds: Dict[str, Led] = {}
        self.btn_grab = styled_button('▶ Acquisition', '#2980b9',
                                      'Start the continuous acquisition of the detectors used by the roles')
        self.btn_grab.clicked.connect(self.app.start_detectors)
        lay.addWidget(self.btn_grab)

        lay.addWidget(self._vline())
        self.intensity = QtWidgets.QLabel('I —')
        self.intensity.setToolTip('Intensity guard (S2)')
        self.intensity.setStyleSheet('font-size: 12pt; font-weight: bold;')
        lay.addWidget(self.intensity)

        lay.addWidget(self._vline())
        self.ml_buttons: Dict[str, QtWidgets.QPushButton] = {}
        for state in ('ML_ON', 'ML_OFF', 'UNSTABLE'):
            b = QtWidgets.QPushButton(ML_LABELS[state])
            b.setCheckable(True)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setToolTip('Manual annotation of the mode-lock state (timestamped in the log, O6-A)')
            b.clicked.connect(lambda _=False, s=state: self._on_ml(s))
            self.ml_buttons[state] = b
            lay.addWidget(b)

        self.btn_note = styled_button('📝 Note', None, 'Add a timestamped note to the log (T7.5)')
        self.btn_note.clicked.connect(self._on_note)
        lay.addWidget(self.btn_note)
        self.btn_shake = styled_button('🎢 Shake', '#9b59b6', 'Piezo shake (Space in Alignment)')
        self.btn_shake.clicked.connect(lambda: self.app.shake('button'))
        lay.addWidget(self.btn_shake)
        self.btn_rec = styled_button('● REC', '#7f8c8d', 'Start / stop the HDF5 recording', checkable=True)
        self.btn_rec.clicked.connect(self._on_rec)
        lay.addWidget(self.btn_rec)

        self.btn_reset = styled_button('Re-arm', '#e67e22',
                                       'Leave the EMERGENCY STOP state (after checking)')
        self.btn_reset.clicked.connect(self._on_reset)
        self.btn_reset.setVisible(False)
        lay.addWidget(self.btn_reset)
        self.btn_estop = QtWidgets.QPushButton('🛑 EMERGENCY STOP')
        self.btn_estop.setFocusPolicy(QtCore.Qt.NoFocus)
        self.btn_estop.setMinimumHeight(40)
        self.btn_estop.setStyleSheet('QPushButton { background-color: #c0392b; color: white; font-weight: bold; '
                                     'font-size: 13pt; padding: 4px 14px; border-radius: 5px; }'
                                     'QPushButton:pressed { background-color: #7b241c; }')
        self.btn_estop.setToolTip('Stops ALL automations, then ALL motors of the dashboard '
                                  f'(shortcut: {self.app.estop_shortcut_text()}).')
        self.btn_estop.clicked.connect(lambda: self.app.emergency_stop('button'))
        lay.addWidget(self.btn_estop)

    @staticmethod
    def _vline():
        f = QtWidgets.QFrame()
        f.setFrameShape(QtWidgets.QFrame.VLine)
        f.setStyleSheet('color: rgba(127,127,127,120);')
        return f

    # ------------------------------------------------------------------ actions
    def _on_ml(self, state: str):
        self.app.core.set_ml_state(state, 'UI')
        self.refresh()

    def _on_note(self):
        text, ok = QtWidgets.QInputDialog.getMultiLineText(self, 'Note', 'Timestamped note (log + recording):')
        if ok and text.strip():
            self.app.core.add_note(text.strip(), self.app.operator or 'UI')

    def _on_rec(self):
        self.app.toggle_recording(self.btn_rec.isChecked())
        self.refresh()

    def _on_reset(self):
        if QtWidgets.QMessageBox.question(
                self, 'Re-arm', 'Leave the EMERGENCY STOP state?\n\nFirst check the state of the laser '
                                'and of the motors. No automation restarts by itself.') == QtWidgets.QMessageBox.Yes:
            self.app.core.reset_estop('UI')
            self.refresh()

    # ------------------------------------------------------------------ refresh (<= 5 Hz)
    def refresh(self):
        core = self.app.core
        g = core.global_state
        self.global_badge.set_state(g, prefix='')
        self.btn_reset.setVisible(core.estop)
        # automations
        # only what is engaged: the list of PIDs / optimisations can be long (their buttons are in
        # the Alignment tab and their own tabs)
        autos = [a for a in core.automations.values() if a.state != AutoState.OFF]
        # what needs attention first, then the routines (they drive the others); the chips share the free
        # width, and what does not fit is summarised by a "+n" chip (tooltip: the list)
        autos.sort(key=lambda a: (a.state not in (AutoState.HELD, AutoState.ERROR), a.kind != 'routine'))
        fit = max(int((self.auto_area.width() + 4) // (self.CHIP_MIN_W + 4)), 1)
        hidden = autos[fit - 1:] if len(autos) > fit else []
        autos = autos[:fit - 1] if hidden else autos
        chip_w = int(min(self.CHIP_MAX_W, max(self.CHIP_MIN_W, (self.auto_area.width() + 4)
                                                / max(len(autos) + bool(hidden), 1) - 4)))
        self.more.setVisible(bool(hidden))
        self.more.setText(f'+{len(hidden)}')
        self.more.setToolTip('\n'.join(f'{STATE_MARKS.get(a.state, "")}{a.label}: {STATE_LABELS.get(a.state, a.state)}'
                                       for a in hidden))
        ids = [a.id for a in autos]
        for aid in list(self._auto_badges):
            if aid not in ids:
                w = self._auto_badges.pop(aid)
                w.setParent(None)
                w.deleteLater()
        for a in autos:
            b = self._auto_badges.get(a.id)
            if b is None:
                b = QtWidgets.QPushButton()
                b.setFocusPolicy(QtCore.Qt.NoFocus)
                b.clicked.connect(lambda _=False, i=a.id: self.automation_clicked.emit(i))
                self._auto_badges[a.id] = b
                self.auto_box.insertWidget(self.auto_box.count() - 2, b)
            b.setFixedWidth(chip_w)
            color = STATE_COLORS.get(a.state, '#7f8c8d')
            text = f'{STATE_MARKS.get(a.state, "")}{a.label}'
            b.setText(b.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, chip_w - 16))
            b.setStyleSheet(f'QPushButton {{ background-color: {color}; color: white; font-weight: bold; '
                            f'border-radius: 4px; padding: 2px 6px; }}')
            b.setToolTip(f'{a.label}: {STATE_LABELS.get(a.state, a.state)}' + (f'\n{a.reason}' if a.reason else ''))
        # detectors used by roles
        dets = self.app.role_detectors()
        for d in list(self._det_leds):
            if d not in dets:
                w = self._det_leds.pop(d)
                w.setParent(None)
                w.deleteLater()
        any_idle = False
        for d in dets:
            led = self._det_leds.get(d)
            if led is None:
                led = Led(d)
                led.setStyleSheet('font-size: 11pt;')
                self._det_leds[d] = led
                self.det_box.addWidget(led)
            st = core.signals.detector_status(d)
            grabbing = core.broker.is_grabbing(d)
            any_idle = any_idle or (d in core.broker.detectors and not grabbing)
            age = '—' if not math.isfinite(st.age) else f'{st.age:.1f} s'
            tip = f'{d}: {st.state} {st.reason}\nacquisition: {"running" if grabbing else "stopped"}'
            if st.heartbeat_channel:
                tip += f'\nframe counter: {st.heartbeat_channel} = {fmt(st.heartbeat_value, 8)}'
            led.set_color(DET_COLORS.get(st.state, '#7f8c8d'), f'{d} {age}', tip)
        self.btn_grab.setVisible(any_idle)
        # intensity
        guard = core.safety.guard
        state = guard.evaluate()
        v = guard.value()
        if state == 'disabled':
            self.intensity.setText('I: guard DISABLED')
            self.intensity.setStyleSheet('font-size: 11pt; font-weight: bold; color: #e67e22;')
        else:
            trip, release = guard.thresholds()
            ratio = guard.ratio()
            txt = f'I {fmt(v)}'
            if math.isfinite(ratio):
                txt += f' ({100 * ratio:.0f} %)'
            color = {'ok': '#27ae60', 'low': '#e74c3c', 'unknown': '#7f8c8d'}.get(state, '#7f8c8d')
            self.intensity.setText(txt)
            self.intensity.setStyleSheet(f'font-size: 12pt; font-weight: bold; color: {color};')
            self.intensity.setToolTip(f'Intensity guard (S2): {state}\nrole: {guard.cfg.role}\n'
                                      f'threshold: {fmt(trip)}  release: {fmt(release)}  reference: {fmt(guard.reference)}')
        # ML
        for s, b in self.ml_buttons.items():
            on = core.ml_state == s
            b.setChecked(on)
            b.setStyleSheet(f'QPushButton {{ background-color: {ML_COLORS[s] if on else "transparent"}; '
                            f'color: {"white" if on else "palette(text)"}; font-weight: bold; padding: 2px 6px; '
                            f'border: 1px solid {ML_COLORS[s]}; border-radius: 4px; }}')
        # recording
        rec = self.app.recorder
        self.btn_rec.setChecked(rec.active)
        self.btn_rec.setStyleSheet('QPushButton { background-color: %s; color: white; font-weight: bold; '
                                   'padding: 3px 8px; }' % ('#c0392b' if rec.active else '#7f8c8d'))
        self.btn_rec.setText('● REC' if rec.active else '○ REC')
        if rec.active and rec.current_path is not None:
            self.btn_rec.setToolTip(f'Recording: {rec.current_path}')
        self.btn_shake.setEnabled(not core.estop and not self.app.shaker.busy)
