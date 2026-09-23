"""Automation buttons of the Alignment tab: the PIDs and optimisations marked « Bouton dans
Alignement » in their tab, one card each, under the motor cards.

One click does it, without confirmation (the automation settings were chosen in their own tab):
stopped -> start, frozen by a safety rule (HELD) -> resume, otherwise -> stop. Right click: the other
actions and « Ouvrir les réglages ». Refusals (invalid preset, missing safety limits, E-STOP...) are
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

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.auto_id = item['id']
        self._style_key = None
        self.setObjectName('quickCard')
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 6, 0)
        h.setSpacing(7)
        self.band = QtWidgets.QLabel('PID' if item['kind'] == 'pid' else 'OPT')
        self.band.setObjectName('quickBand')
        self.band.setAlignment(QtCore.Qt.AlignCenter)
        self.band.setFixedWidth(34)
        h.addWidget(self.band)
        v = QtWidgets.QVBoxLayout()
        v.setContentsMargins(0, 3, 0, 3)
        v.setSpacing(0)
        self.name = QtWidgets.QLabel(item['label'])
        f = self.name.font()
        f.setPointSize(f.pointSize() + 1)
        f.setBold(True)
        self.name.setFont(f)
        self.line = QtWidgets.QLabel('')
        self.hint = QtWidgets.QLabel('')
        self.hint.setStyleSheet('color: gray; font-size: 10px;')
        for w in (self.name, self.line, self.hint):
            w.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            v.addWidget(w)
        h.addLayout(v, 1)
        self.band.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

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
        text_w = self.WIDTH - 34 - 20
        self._elide(self.name, self.item['label'], text_w)
        self._elide(self.line, line, text_w)
        self._elide(self.hint, hint, text_w)
        self.setToolTip(tip)

    def mouseReleaseEvent(self, e):
        if self.rect().contains(e.pos()):
            if e.button() == QtCore.Qt.LeftButton:
                self.clicked.emit(self.auto_id)
            elif e.button() == QtCore.Qt.RightButton:
                self.menu_requested.emit(self.auto_id, self.mapToGlobal(e.pos()))
        super().mouseReleaseEvent(e)


class QuickPanel(QtWidgets.QFrame):
    """Buttons of the PIDs / optimisations shown in the Alignment tab (see module docstring)."""

    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.cards: Dict[str, QuickCard] = {}
        self._signature: Optional[Tuple] = None
        self._last_click: Dict[str, float] = {}
        self.setObjectName('quickPanel')
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setStyleSheet('#quickPanel { border-top: 2px solid rgba(127,127,127,110); }')
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 6, 2, 2)
        lay.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('Automatismes')
        f = title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        title.setFont(f)
        head.addWidget(title)
        hint = QtWidgets.QLabel('clic : démarrer / arrêter (gelé : reprendre) · clic droit : plus')
        hint.setStyleSheet('color: gray; font-style: italic;')
        head.addWidget(hint)
        head.addStretch()
        lay.addLayout(head)
        self.message = QtWidgets.QLabel('')
        self.message.setWordWrap(True)
        self.message.setStyleSheet('color: #c0392b; font-weight: bold;')
        self.message.setVisible(False)
        self.message.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.message)
        self._msg_timer = QtCore.QTimer(self)
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(lambda: self.message.setVisible(False))
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setFocusPolicy(QtCore.Qt.NoFocus)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.container = QtWidgets.QWidget()
        self.flow = FlowLayout(self.container, spacing=6)
        self.flow.setContentsMargins(2, 2, 2, 2)
        self.scroll.setWidget(self.container)
        lay.addWidget(self.scroll, 1)
        self.empty = QtWidgets.QLabel('Aucun bouton : cocher « Bouton dans Alignement » dans les réglages d’un PID '
                                      '(onglet PID) ou d’une optimisation (onglet Optimisation).')
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
            card = QuickCard(it)
            card.clicked.connect(self._on_click)
            card.menu_requested.connect(self._on_menu)
            self.flow.addWidget(card)
            self.cards[it['id']] = card
        self.empty.setVisible(not items)
        self.scroll.setVisible(bool(items))
        self.container.updateGeometry()

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
        self._say('' if ok else f"{self._item(auto_id)['label']} : {msg}")
        self.refresh()

    def _on_menu(self, auto_id: str, pos: QtCore.QPoint):
        a = self.app.core.get(auto_id)
        state = a.state if a is not None else AutoState.OFF
        item = self._item(auto_id)
        menu = QtWidgets.QMenu(self)
        if state not in ENGAGED_STATES:
            text = '▶ Démarrer'
            if item.get('kind') == 'optimizer':
                text += f" ({OPT_BUTTON_ACTION_LABELS.get(item.get('action'), 'une passe')})"
            menu.addAction(text, lambda: self._on_click(auto_id, 'start'))
        else:
            if state in (AutoState.HELD, AutoState.PAUSED):
                menu.addAction('⏯ Reprendre', lambda: self._on_click(auto_id, 'resume'))
            menu.addAction('⏹ Arrêter', lambda: self._on_click(auto_id, 'stop'))
        menu.addSeparator()
        menu.addAction('⚙ Ouvrir les réglages', lambda: self.app.select_automation_tab(auto_id))
        fn = getattr(menu, 'exec', None) or menu.exec_
        fn(pos)

    # ------------------------------------------------------------------ refresh (<= 5 Hz)
    def refresh(self):
        items = self.app.quick_items()
        sig = tuple((it['id'], it['label'], it['action']) for it in items)
        if sig != self._signature:
            self._signature = sig
            self._rebuild(items)
        core = self.app.core
        estop = core.estop
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
        reason = reason.split(' : ')[0]          # 'données périmées : SimB/...' -> the tooltip has the rest
        is_opt = item.get('kind') == 'optimizer'
        what = OPT_BUTTON_ACTION_LABELS.get(item.get('action'), 'une passe') if is_opt else ''
        if state == AutoState.OFF:
            line = 'Arrêté'
            if is_opt and a is not None and getattr(a, 'pass_count', 0) and a.status:
                line += f' · {a.status}'
            elif a is not None and full_reason and full_reason not in ('démarré', 'arrêt depuis Alignement',
                                                                       'arrêt manuel'):
                line += f' · {reason}'
            hint = 'ARRÊT D’URGENCE : réarmer d’abord' if estop else \
                ('clic : démarrer' + (f' ({what})' if is_opt else ''))
            return line, hint
        if state == AutoState.HELD:
            return f'Gelé : {reason}', 'clic : reprendre · clic droit : arrêter'
        if state == AutoState.ERROR:
            return f'Erreur : {reason}', 'clic : arrêter'
        if state == AutoState.PAUSED:
            return f'En pause : {reason}', 'clic : arrêter · clic droit : reprendre'
        if state == AutoState.ARMED:
            return 'Armement…', 'clic : arrêter'
        # ACTIVE
        if is_opt:
            if a.in_pass:
                line = f'Passe en cours · it. {a.iteration}'
            else:
                left = a.seconds_to_next_pass(self.app.core.clock.now())
                line = 'Actif' if left is None else f'Prochaine passe dans {int(left) // 60:02d}:{int(left) % 60:02d}'
            return line, 'clic : arrêter'
        t = a.controller.last
        line = 'Actif' if not math.isfinite(t.error) else f'Actif · erreur {fmt(t.error, 3)}'
        if getattr(t, 'in_deadband', False):
            line += ' (bande morte)'
        return line, 'clic : arrêter'
