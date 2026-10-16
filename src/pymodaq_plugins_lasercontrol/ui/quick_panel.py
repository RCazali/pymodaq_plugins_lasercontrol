"""Automation buttons of the Alignment tab: the PIDs, optimisations and routines marked "Button in
Alignment" in their tab, one card each, under the motor cards (band PID / OPT / SEQ).

One click does it, without confirmation (the automation settings were chosen in their own tab):
stopped -> start, frozen by a safety rule (HELD) -> resume, otherwise -> stop. Right click: the other
actions and "Open settings". Refusals (invalid preset, missing safety limits, E-STOP...) are
written under the buttons and in the journal, never in a dialog box.

Every widget here refuses the keyboard focus so that the Alignment keyboard jog stays active.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from qtpy import QtCore, QtGui, QtWidgets

from ..core.states import ENGAGED_STATES, STATE_COLORS, AutoState
from ..presets.schema import OPT_BUTTON_ACTION_LABELS
from .widgets import FlowLayout, fmt

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

NEUTRAL = '#7f8c8d'
DEBOUNCE_S = 0.5            # a double click must not start then stop
MESSAGE_S = 20.0


def _rgba(color: str, alpha: int) -> str:
    c = QtGui.QColor(color)
    return f'rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})'


class QuickCard(QtWidgets.QFrame):
    """A clickable card: coloured band (PID / OPT), name, state line, what a click does."""
    clicked = QtCore.Signal(str)
    menu_requested = QtCore.Signal(str, QtCore.QPoint)

    WIDTH = 280
    HEIGHT = 62
    COMPACT_WIDTH = 190         # jog window: name and state line only
    COMPACT_HEIGHT = 38
    COMPACT_CHARS = 22
    BAND_W = 34

    def __init__(self, item: dict, parent=None, compact: bool = False):
        super().__init__(parent)
        self.item = item
        self.auto_id = item['id']
        self._style_key = None
        self.compact = compact
        # compact: about COMPACT_CHARS characters of text (190 px with the 9 pt font of Windows)
        self.width_px = max(self.COMPACT_WIDTH, self.BAND_W + 20 + self.COMPACT_CHARS *
                            self.fontMetrics().averageCharWidth()) if compact else self.WIDTH
        self.setObjectName('quickCard')
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedSize(self.width_px, max(self.COMPACT_HEIGHT, 2 * self.fontMetrics().height() + 6)
                          if compact else self.HEIGHT)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 6, 0)
        h.setSpacing(7)
        self.band = QtWidgets.QLabel({'pid': 'PID', 'routine': 'SEQ'}.get(item['kind'], 'OPT'))
        self.band.setObjectName('quickBand')
        self.band.setAlignment(QtCore.Qt.AlignCenter)
        self.band.setFixedWidth(self.BAND_W)
        h.addWidget(self.band)
        v = QtWidgets.QVBoxLayout()
        v.setContentsMargins(0, 1 if compact else 3, 0, 1 if compact else 3)
        v.setSpacing(0)
        self.name = QtWidgets.QLabel(item['label'])
        f = self.name.font()
        if not compact:
            f.setPointSize(f.pointSize() + 1)
        f.setBold(True)
        self.name.setFont(f)
        self.line = QtWidgets.QLabel('')
        self.hint = QtWidgets.QLabel('')
        self.hint.setStyleSheet('color: gray; font-size: 10px;')
        for w in (self.name, self.line, self.hint):
            w.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            v.addWidget(w)
        self.hint.setVisible(not compact)        # compact: the hint is in the tooltip
        h.addLayout(v, 1)
        self.band.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QtCore.QSize:
        return self.size() if self.minimumSize() == self.maximumSize() else super().sizeHint()   # the flow layout

    @staticmethod
    def _elide(label: QtWidgets.QLabel, text: str, width: int):
        label.setText(label.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, width))

    def set_view(self, state: AutoState, line: str, hint: str, tip: str):
        color = STATE_COLORS.get(state, NEUTRAL)
        engaged = state in ENGAGED_STATES
        key = (color, engaged)
        if key != self._style_key:
            self._style_key = key
            border = color if engaged else _rgba(NEUTRAL, 150)
            bg = _rgba(color, 45) if engaged else 'transparent'
            self.setStyleSheet(
                f'#quickCard {{ border: 2px solid {border}; border-radius: 8px; background-color: {bg}; }}'
                f'#quickCard:hover {{ border: 2px solid {color}; background-color: {_rgba(color, 70)}; }}'
                f'#quickBand {{ background-color: {color}; color: white; font-weight: bold; font-size: 11px; '
                f'border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}')
            self.line.setStyleSheet(f'color: {color if engaged else "gray"}; font-weight: bold;')
        text_w = self.width_px - self.BAND_W - 20
        self._elide(self.name, self.item['label'], text_w)
        self._elide(self.line, line, text_w)
        self._elide(self.hint, hint, text_w)
        self.setToolTip(f'{tip}\n{hint}' if self.compact and hint else tip)

    def mouseReleaseEvent(self, e):
        if self.rect().contains(e.pos()):
            if e.button() == QtCore.Qt.LeftButton:
                self.clicked.emit(self.auto_id)
            elif e.button() == QtCore.Qt.RightButton:
                self.menu_requested.emit(self.auto_id, self.mapToGlobal(e.pos()))
        super().mouseReleaseEvent(e)


class QuickPanel(QtWidgets.QFrame):
    """Buttons of the PIDs / optimisations shown in the Alignment tab (see module docstring)."""
    height_changed = QtCore.Signal()     # compact: the cards wrapped on more / fewer lines

    def __init__(self, app: 'LaserControlApp', parent=None, compact: bool = False):
        """``compact``: the strip of the jog window (small cards, no scroll area, E-STOP / Re-arm buttons)."""
        super().__init__(parent)
        self.app = app
        self.compact = compact
        self.cards: Dict[str, QuickCard] = {}
        self._signature: Optional[Tuple] = None
        self._last_click: Dict[str, float] = {}
        self.setObjectName('quickPanel')
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        if not compact:
            self.setStyleSheet('#quickPanel { border-top: 2px solid rgba(127,127,127,110); }')
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(*((0, 0, 0, 0) if compact else (2, 6, 2, 2)))
        lay.setSpacing(2 if compact else 4)
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('Automations')
        f = title.font()
        f.setBold(True)
        if not compact:
            f.setPointSize(f.pointSize() + 1)
        title.setFont(f)
        head.addWidget(title)
        hint = QtWidgets.QLabel('click: start / stop (held: resume) · right click: more')
        hint.setStyleSheet('color: gray; font-style: italic;')
        if compact:
            title.setToolTip(hint.text())
        else:
            head.addWidget(hint)
        head.addStretch()
        self.btn_estop = self.btn_rearm = None
        if compact:
            self.btn_rearm = QtWidgets.QPushButton('Re-arm')
            self.btn_rearm.setToolTip('Leave the EMERGENCY STOP state')
            self.btn_rearm.setStyleSheet('background-color: #e67e22; color: white; font-weight: bold; '
                                         'padding: 1px 8px;')
            self.btn_rearm.clicked.connect(self._on_rearm)
            self.btn_rearm.setVisible(False)
            head.addWidget(self.btn_rearm)
            self.btn_estop = QtWidgets.QPushButton('■ E-STOP')
            self.btn_estop.setToolTip(f'Emergency stop: every automation, then every motor of the dashboard '
                                      f'({app.estop_shortcut_text()})')
            self.btn_estop.setStyleSheet('background-color: #c0392b; color: white; font-weight: bold; '
                                         'padding: 1px 8px;')
            self.btn_estop.clicked.connect(lambda: self.app.emergency_stop('jog window'))
            head.addWidget(self.btn_estop)
            for b in (self.btn_rearm, self.btn_estop):
                b.setFocusPolicy(QtCore.Qt.NoFocus)
        lay.addLayout(head)
        # a routine waiting for the operator (e.g. HA maximum not confirmed): big, bright, never colour only
        self.banner = QtWidgets.QFrame()
        self.banner.setObjectName('opBanner')
        self.banner.setStyleSheet('#opBanner { border: 3px solid #ffd75e; border-radius: 6px; '
                                  'background-color: rgba(255, 215, 94, 40); }')
        bl = QtWidgets.QHBoxLayout(self.banner)
        bl.setContentsMargins(8, 4, 8, 4)
        self.banner_text = QtWidgets.QLabel('')
        self.banner_text.setWordWrap(True)
        f2 = self.banner_text.font()
        f2.setBold(True)
        f2.setPointSize(f2.pointSize() + 1)
        self.banner_text.setFont(f2)
        bl.addWidget(self.banner_text, 1)
        self.banner_buttons: Dict[str, QtWidgets.QPushButton] = {}
        from ..engines.program import ANSWERS
        for key, text in ANSWERS.items():
            b = QtWidgets.QPushButton(text)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setStyleSheet('QPushButton { font-weight: bold; padding: 4px 10px; }')
            b.clicked.connect(lambda _=False, k=key: self._on_answer(k))
            bl.addWidget(b)
            self.banner_buttons[key] = b
        self.banner.setVisible(False)
        self._banner_id = ''
        lay.addWidget(self.banner)
        self.message = QtWidgets.QLabel('')
        self.message.setWordWrap(True)
        self.message.setStyleSheet('color: #c0392b; font-weight: bold;')
        self.message.setVisible(False)
        self.message.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.message)
        self._msg_timer = QtCore.QTimer(self)
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(lambda: self.message.setVisible(False))
        self.container = QtWidgets.QWidget()
        self.flow = FlowLayout(self.container, spacing=4 if compact else 6)
        self.flow.setContentsMargins(2, 2, 2, 2)
        if compact:                       # the jog window grows with its cards: no scroll area
            self.scroll = self.container
            lay.addWidget(self.container)
        else:
            self.scroll = QtWidgets.QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.scroll.setFocusPolicy(QtCore.Qt.NoFocus)
            self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.scroll.setWidget(self.container)
            lay.addWidget(self.scroll, 1)
        self.empty = QtWidgets.QLabel('No button: tick "Button in the jog window" in the settings of a PID '
                                      '(PID tab), of an optimization (Optimization tab) or of a routine '
                                      '(Routines tab).')
        if compact:
            self.empty.setText('No button: tick "Button in the jog window" in the PID / Optimization / Routines tab.')
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet('color: gray; font-style: italic;')
        lay.addWidget(self.empty)

    # ------------------------------------------------------------------ building
    def _rebuild(self, items: List[dict]):
        for c in self.cards.values():
            self.flow.removeWidget(c)
            c.setParent(None)
            c.deleteLater()
        self.cards = {}
        for it in items:
            card = QuickCard(it, compact=self.compact)
            card.clicked.connect(self._on_click)
            card.menu_requested.connect(self._on_menu)
            self.flow.addWidget(card)
            self.cards[it['id']] = card
        self.empty.setVisible(not items)
        self.scroll.setVisible(bool(items))
        self.container.updateGeometry()
        self._fit_cards()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_cards()

    def _fit_cards(self):
        """Compact: the layouts of the jog window ignore the height-for-width of the flow layout, so the container
        gets the height of its wrapped lines as minimum height"""
        if not self.compact:
            return
        width = self.container.width() or self.width()
        height = self.flow.heightForWidth(max(width, 1)) if self.cards else 0
        if height != self.container.minimumHeight():
            self.container.setFixedHeight(height)
            self.layout().invalidate()
            self.updateGeometry()
            self.height_changed.emit()

    # ------------------------------------------------------------------ actions
    def _say(self, text: str):
        self.message.setText(text)
        self.message.setVisible(bool(text))
        if text:
            self._msg_timer.start(int(MESSAGE_S * 1000))

    def _item(self, auto_id: str) -> dict:
        card = self.cards.get(auto_id)
        return card.item if card is not None else {'label': auto_id}

    def _on_click(self, auto_id: str, action: str = 'toggle'):
        now = time.monotonic()
        if action == 'toggle' and now - self._last_click.get(auto_id, -1e9) < DEBOUNCE_S:
            return
        self._last_click[auto_id] = now
        ok, msg = self.app.quick_click(auto_id, action)
        self._say('' if ok else f"{self._item(auto_id)['label']}: {msg}")
        self.refresh()

    def _on_menu(self, auto_id: str, pos: QtCore.QPoint):
        a = self.app.core.get(auto_id)
        state = a.state if a is not None else AutoState.OFF
        item = self._item(auto_id)
        menu = QtWidgets.QMenu(self)
        if state not in ENGAGED_STATES:
            text = '▶ Start'
            if item.get('kind') == 'optimizer':
                text += f" ({OPT_BUTTON_ACTION_LABELS.get(item.get('action'), 'one pass')})"
            menu.addAction(text, lambda: self._on_click(auto_id, 'start'))
        else:
            if state in (AutoState.HELD, AutoState.PAUSED):
                menu.addAction('⏯ Resume', lambda: self._on_click(auto_id, 'resume'))
            menu.addAction('⏹ Stop', lambda: self._on_click(auto_id, 'stop'))
        menu.addSeparator()
        menu.addAction('⚙ Open settings', lambda: self.app.select_automation_tab(auto_id))
        fn = getattr(menu, 'exec', None) or menu.exec_
        fn(pos)

    def _on_rearm(self):
        from .widgets import ask_yes_no
        if ask_yes_no(self.window(), 'Re-arm', 'Leave the EMERGENCY STOP state?\n\nFirst check the state of the laser '
                                               'and of the motors.'):
            self.app.core.reset_estop('jog window')
        self.refresh()

    def _on_answer(self, choice: str):
        if self._banner_id:
            ok, msg = self.app.answer_question(self._banner_id, choice)
            self._say('' if ok else msg)
        self.refresh()

    def _refresh_banner(self):
        qs = self.app.questions()
        if not qs:
            self.banner.setVisible(False)
            self._banner_id = ''
            return
        aid, label, q = qs[0]
        self._banner_id = aid
        more = f'   (+{len(qs) - 1} other question(s))' if len(qs) > 1 else ''
        self.banner_text.setText(f"⚠ {label}: {q['text']} — what next?{more}")
        for key, b in self.banner_buttons.items():
            b.setVisible(key in q.get('choices', []))
        self.banner.setVisible(True)

    # ------------------------------------------------------------------ refresh (<= 5 Hz)
    def refresh(self):
        self._refresh_banner()
        items = self.app.quick_items()
        sig = tuple((it['id'], it['label'], it['action']) for it in items)
        if sig != self._signature:
            self._signature = sig
            self._rebuild(items)
        core = self.app.core
        estop = core.estop
        if self.btn_rearm is not None and self.btn_rearm.isVisible() != estop:
            self.btn_rearm.setVisible(estop)
        for auto_id, card in self.cards.items():
            a = core.get(auto_id)
            state = a.state if a is not None else AutoState.OFF
            reason = a.reason if a is not None else ''
            line, hint = self._describe(card.item, a, state, reason, estop)
            tip = f"{card.item['label']} ({auto_id})"
            if a is not None and hasattr(a, 'summary'):
                try:
                    tip += '\n' + a.summary()
                except Exception:
                    pass
            if reason and state != AutoState.OFF:
                tip += f'\n{reason}'
            card.set_view(state, line, hint, tip)

    def _describe(self, item: dict, a, state: AutoState, reason: str, estop: bool) -> Tuple[str, str]:
        full_reason = reason
        reason = reason.split(': ')[0]           # 'stale data: SimB/...' -> the tooltip has the rest
        is_opt = item.get('kind') == 'optimizer'
        what = OPT_BUTTON_ACTION_LABELS.get(item.get('action'), 'one pass') if is_opt else ''
        if state == AutoState.OFF:
            line = 'Stopped'
            if item.get('kind') == 'routine' and a is not None and a.outcome:
                line = {'done': '✔ Done', 'failed': '✖ Failed', 'stopped': '■ Stopped'}.get(a.outcome, 'Stopped')
                if a.outcome == 'failed':
                    line += ' · ' + full_reason.replace('failed at ', '')
            elif is_opt and a is not None and getattr(a, 'pass_count', 0) and a.status:
                line += f' · {a.status}'
            elif a is not None and full_reason and full_reason not in ('started', 'stopped from Alignment',
                                                                       'manual stop'):
                line += f' · {reason}'
            hint = 'EMERGENCY STOP: re-arm first' if estop else \
                ('click: start' + (f' ({what})' if is_opt else ''))
            return line, hint
        if state == AutoState.HELD:
            return f'Held: {reason}', 'click: resume · right click: stop'
        if state == AutoState.ERROR:
            return f'Error: {reason}', 'click: stop'
        if state == AutoState.PAUSED:
            if a is not None and getattr(a, 'pause_kind', '') == 'manual':
                return f'Paused: {reason}', 'click: resume · right click: stop'
            return f'Paused: {reason}', 'click: stop · right click: resume'
        if state == AutoState.ARMED:
            return 'Arming…', 'click: stop'
        # ACTIVE
        if item.get('kind') == 'routine':
            if getattr(a, 'question', None) is not None:
                return '⚠ Waiting for the operator', 'answer above · click: stop'
            return f'▶ {a.status}', 'click: stop'
        if is_opt:
            if a.in_pass:
                line = f'Pass running · it. {a.iteration}'
            else:
                left = a.seconds_to_next_pass(self.app.core.clock.now())
                line = 'Active' if left is None else f'Next pass in {int(left) // 60:02d}:{int(left) % 60:02d}'
            return line, 'click: stop'
        errs = [l.controller.last for l in a.loops]
        if not all(math.isfinite(t.error) for t in errs):
            return 'Active', 'click: stop'
        line = 'Active · error ' + ' / '.join(fmt(t.error, 3) for t in errs)
        if all(t.in_deadband for t in errs):
            line += ' (deadband)'
        return line, 'click: stop'
