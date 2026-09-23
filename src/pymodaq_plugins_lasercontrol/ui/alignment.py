"""Alignment tab (cahier des charges T2.3 / T5.4), ported from the XY Stage Controller.

Tk-like keyboard alignment: every logical axis visible at once, one active axis chosen with
1..9, 0, jog with w/s/a/d (+Shift = fast), live positions, end-of-move history.

What changed: every order goes through the broker (roles instead of module names). A manual
action on a motor stops the automation that owns it *before* the order is sent (D5/S7), with an
explicit message in the journal. The 'limit' jog mode (move_abs towards Min/Max, stopped on key
release) is flagged ⚠: that long move may continue on the controller if PyMoDAQ crashes (S10).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from ..core.log import DedupLogger, module_logger
from .widgets import mono_font

if TYPE_CHECKING:  # pragma: no cover
    from .manual import ManualControl

logger = DedupLogger(module_logger(__file__))

ALIGN_HISTORY_MAX = 2000
AXIS_KEYS = [QtCore.Qt.Key_1, QtCore.Qt.Key_2, QtCore.Qt.Key_3, QtCore.Qt.Key_4, QtCore.Qt.Key_5,
             QtCore.Qt.Key_6, QtCore.Qt.Key_7, QtCore.Qt.Key_8, QtCore.Qt.Key_9, QtCore.Qt.Key_0]
AXIS_KEY_LABELS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']
JOG_KEYS = {QtCore.Qt.Key_W: ('v', +1), QtCore.Qt.Key_S: ('v', -1),
            QtCore.Qt.Key_D: ('h', +1), QtCore.Qt.Key_A: ('h', -1)}
JOG_MODES = ['auto', 'native', 'step', 'limit']
JOG_MODE_TIPS = {
    'auto': "native si le plugin implémente jog(), sinon step",
    'native': "mouvement continu via la commande jog(direction, vitesse) du plugin. Lent/Rapide = vitesse",
    'step': "mouvements relatifs répétés tant que la touche est tenue. Lent/Rapide = taille du pas "
            "(recommandé : rien ne continue si PyMoDAQ plante)",
    'limit': "⚠ move_abs vers Min/Max, arrêt au relâchement. Ce long mouvement PEUT CONTINUER sur le "
             "contrôleur si PyMoDAQ plante (S10) : préférer step ou native",
}
AXIS_COLORS = ['#1f77b4', '#d95f02', '#2ca02c', '#d62728', '#7b52ab', '#8c564b',
               '#c2185b', '#00838f', '#9e7a00', '#546e7a', '#3949ab', '#6d4c41']
KEY_HINT = {'h': 'a ◀  ▶ d', 'v': 's ▼  ▲ w'}
HIST_BG = '#1e1e2e'
HIST_STOPS = np.array([[112, 118, 146], [137, 180, 250], [249, 226, 175]], dtype=float)
HIST_MARKER = '#f38ba8'


def _channel(actuator='None', invert=False, slow=0.1, fast=1.0, mode='step',
             vmin=0.0, vmax=25.0, speed_param='', label='', decimals=4):
    return {'actuator': actuator, 'invert': invert, 'mode': mode, 'slow': slow, 'fast': fast,
            'min': vmin, 'max': vmax, 'speed_param': speed_param, 'label': label, 'decimals': decimals}


def axis_color(ax: dict, idx: int) -> str:
    return ax.get('color') or AXIS_COLORS[idx % len(AXIS_COLORS)]


def channel_label(ch: dict, ch_key: str) -> str:
    return ch.get('label') or ('↔' if ch_key == 'h' else '↕')


def lab_default_axes() -> list:
    """Axis list of the historical Tk GUI (same order, names, directions and speeds). Actuators
    are left to 'None': assign roles once in the Configuration sub-tab (saved in the preset)."""
    pico = dict(slow=100, fast=1000, decimals=0)
    ha = dict(slow=0.1, fast=2.0)
    return [
        {'name': 'OC', 'h': _channel(invert=True, label='Horiz', **pico), 'v': _channel(label='Vert', **pico)},
        {'name': 'End mirror', 'h': _channel(invert=True, label='Horiz', **pico), 'v': _channel(label='Vert', **pico)},
        {'name': 'Aperture', 'h': _channel(label='X', **ha), 'v': _channel(label='Y', **ha)},
        {'name': 'Detuning', 'h': _channel(slow=0.1 / 3, fast=2.0 / 3, label='Pos'), 'v': None},
        {'name': 'Kerr medium', 'h': _channel(slow=1.0, fast=4.0, label='Pos', decimals=3), 'v': None},
        {'name': '100mm Stage', 'h': _channel(label='Pos', **ha), 'v': None},
        {'name': 'Gas Jet xy', 'h': _channel(label='X', **ha), 'v': _channel(label='Y', **ha)},
        {'name': 'Gas Jet z', 'h': None, 'v': _channel(invert=True, label='Z', **ha)},
        {'name': 'PM align', 'h': _channel(invert=True, label='Horiz', **pico), 'v': _channel(label='Vert', **pico)},
        {'name': 'Kerr shift', 'h': _channel(invert=True, slow=1000, fast=1000, label='Shift', decimals=0), 'v': None},
        {'name': 'OC beam', 'h': _channel(invert=True, label='Horiz', **pico), 'v': None},
    ]


def _history_colors(n: int) -> np.ndarray:
    if n <= 0:
        return np.zeros((0, 3))
    t = np.linspace(0.0, 1.0, n) if n > 1 else np.ones(1)
    seg = np.clip(t * (len(HIST_STOPS) - 1), 0, len(HIST_STOPS) - 1 - 1e-9)
    i = seg.astype(int)
    f = (seg - i)[:, None]
    return HIST_STOPS[i] * (1 - f) + HIST_STOPS[i + 1] * f


def _same_point(a, b) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))


class _CardContainer(QtWidgets.QWidget):
    def __init__(self, on_resize):
        super().__init__()
        self._on_resize = on_resize

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._on_resize(e.size().width())


class _AxisCard(QtWidgets.QFrame):
    clicked = QtCore.Signal()

    def mousePressEvent(self, e):
        self.clicked.emit()
        super().mousePressEvent(e)


class KeyPage(QtWidgets.QWidget):
    """Owns the keyboard focus and forwards key / focus events to its owner."""
    focus_changed = QtCore.Signal(bool)

    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def keyPressEvent(self, e):
        if not (self.hasFocus() and self._owner.handle_key_press(e)):
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if not self._owner.handle_key_release(e):   # releases always processed (safety)
            super().keyReleaseEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.focus_changed.emit(True)

    def focusOutEvent(self, e):
        self._owner.release_all_jogs()               # a release elsewhere would never reach us
        self.focus_changed.emit(False)
        super().focusOutEvent(e)

    def hideEvent(self, e):
        self._owner.release_all_jogs()
        super().hideEvent(e)


class AxisHistoryPlot(QtWidgets.QWidget):
    """End-of-move history of the active axis (click on a point = go back there, after confirmation)."""
    point_clicked = QtCore.Signal(int)
    clear_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points: list = []
        self._live = None
        self._user_view = False          # the user zoomed / panned: stop following the data
        self._dims = 0
        self._orient: Optional[str] = None
        self._decimals = [4, 4]
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(2, 0, 2, 0)
        self.title = QtWidgets.QLabel('Historique')
        self.title.setStyleSheet('font-weight: bold;')
        bar.addWidget(self.title)
        self.count = QtWidgets.QLabel('')
        self.count.setStyleSheet('color: gray; font-size: 11px;')
        bar.addWidget(self.count)
        bar.addStretch()
        self.btn_fit = QtWidgets.QToolButton()
        self.btn_fit.setText('⤢')
        self.btn_fit.setToolTip('Recentrer sur tout l’historique')
        self.btn_clear = QtWidgets.QToolButton()
        self.btn_clear.setText('🗑')
        self.btn_clear.setToolTip('Effacer l’historique de cet axe')
        for b in (self.btn_fit, self.btn_clear):
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setAutoRaise(True)
            bar.addWidget(b)
        self.btn_fit.clicked.connect(self.fit)
        self.btn_clear.clicked.connect(self.clear_requested.emit)
        v.addLayout(bar)
        self.plot = pg.PlotWidget(background=HIST_BG)
        self.plot.setFocusPolicy(QtCore.Qt.NoFocus)
        self.plot.setMinimumHeight(140)
        pi = self.plot.getPlotItem()
        pi.hideButtons()
        pi.showGrid(x=True, y=True, alpha=0.2)
        for name in ('left', 'bottom'):
            axis = pi.getAxis(name)
            try:
                axis.enableAutoSIPrefix(False)
            except Exception:
                pass
        self._line = pg.PlotDataItem(pen=pg.mkPen((166, 173, 200, 70), width=1))
        self.plot.addItem(self._line)
        try:
            self._scatter = pg.ScatterPlotItem(pen=None, hoverable=True, hoverPen=pg.mkPen('w', width=1.5))
        except TypeError:
            self._scatter = pg.ScatterPlotItem(pen=None)
        self._scatter.sigClicked.connect(self._on_points_clicked)
        self.plot.addItem(self._scatter)
        self._last = pg.ScatterPlotItem(size=13, symbol='o', brush=None, pen=pg.mkPen((249, 226, 175), width=1.5))
        self.plot.addItem(self._last)
        self._cursor = pg.ScatterPlotItem(size=16, symbol='+', pen=pg.mkPen(HIST_MARKER, width=2),
                                          brush=pg.mkBrush(HIST_MARKER))
        self.plot.addItem(self._cursor)
        self._empty = pg.TextItem('', color='#7f849c', anchor=(0.5, 1.5))
        self.plot.addItem(self._empty)
        pi.getViewBox().sigRangeChangedManually.connect(self._on_manual_range)
        v.addWidget(self.plot, stretch=1)

    def _on_manual_range(self, *args):
        self._user_view = True

    def set_axis(self, name, color, orient, labels, units, decimals):
        self._orient = orient
        self._dims = len(labels) if orient else 0
        self._decimals = list(decimals) + [4, 4]
        self.title.setText(f"<span style='color:{color};'>■</span> Historique · {name}")
        pi = self.plot.getPlotItem()
        show_x = orient in ('2d', 'h')
        show_y = orient in ('2d', 'v')
        pi.showAxis('bottom', show_x)
        pi.showAxis('left', show_y)
        pi.getViewBox().setMouseEnabled(x=show_x, y=show_y)
        if orient == 'h':
            pi.setLabel('bottom', labels[0], units=units[0])
        elif orient == 'v':
            pi.setLabel('left', labels[0], units=units[0])
        elif orient == '2d':
            pi.setLabel('bottom', labels[0], units=units[0])
            pi.setLabel('left', labels[1], units=units[1])

    def set_points(self, points, live):
        self._points = points
        n = len(points)
        self.count.setText(f"{n} mouvement{'s' if n != 1 else ''}" if self._dims else '')
        if self._dims == 0 or n == 0:
            self._line.setData([], [])
            self._scatter.setData([], [])
            self._last.setData([], [])
            self._empty.setText('aucun actionneur' if self._dims == 0 else 'aucun mouvement enregistré')
            x0 = y0 = 0.0
            if live and self._dims and all(v is not None for v in live):
                xs, ys = self._xy([live])
                x0, y0 = float(xs[0]), float(ys[0])
            self._empty.setPos(x0, y0)
            self._empty.setVisible(True)
        else:
            self._empty.setVisible(False)
            xs, ys = self._xy([p[1:] for p in points])
            rgb = _history_colors(n)
            alpha = np.linspace(150, 255, n) if n > 1 else np.full(1, 255.0)
            brushes = [pg.mkBrush(int(r), int(g), int(b), int(a)) for (r, g, b), a in zip(rgb, alpha)]
            sizes = np.linspace(5, 9, n) if n > 1 else np.full(1, 9.0)
            kw = dict(x=xs, y=ys, brush=brushes, size=sizes, data=list(range(n)))
            try:
                self._scatter.setData(tip=self._tip, **kw)
            except TypeError:
                self._scatter.setData(**kw)
            self._line.setData(xs, ys) if self._orient == '2d' else self._line.setData([], [])
            self._last.setData([xs[-1]], [ys[-1]])
        self.update_live(live)
        if not self._user_view:
            self.fit()

    def update_live(self, live):
        self._live = list(live) if live else None
        if not live or self._dims == 0 or any(v is None for v in live):
            self._cursor.setData([], [])
            return
        xs, ys = self._xy([live])
        self._cursor.setData(xs, ys)
        if not self._user_view:        # follow the live position when it leaves the view
            (x0, x1), (y0, y1) = self.plot.getPlotItem().getViewBox().viewRange()
            if not (x0 <= float(xs[0]) <= x1 and y0 <= float(ys[0]) <= y1):
                self.fit()

    @staticmethod
    def _span(values) -> tuple:
        """Range of values, never degenerate (a single point gets ±1 % of its value, at least ±1)."""
        lo, hi = float(np.min(values)), float(np.max(values))
        if hi - lo < 1e-12:
            half = max(1.0, abs(lo) * 0.01)
            return lo - half, hi + half
        pad = 0.08 * (hi - lo)
        return lo - pad, hi + pad

    def fit(self):
        pi = self.plot.getPlotItem()
        pi.disableAutoRange()
        pts = [p[1:] for p in self._points]
        live = getattr(self, '_live', None)
        if live and self._dims and all(v is not None for v in live):
            pts.append(live)
        if not pts or self._dims == 0:
            pi.setXRange(-1, 1, padding=0)
            pi.setYRange(-1, 1, padding=0)
            self._user_view = False
            return
        xs, ys = self._xy(pts)
        if self._orient in ('h', '2d'):
            pi.setXRange(*self._span(xs), padding=0)
        else:
            pi.setXRange(-1, 1, padding=0)
        if self._orient in ('v', '2d'):
            pi.setYRange(*self._span(ys), padding=0)
        else:
            pi.setYRange(-1, 1, padding=0)
        self._user_view = False

    def _xy(self, values):
        a = np.array([[float(x) for x in v] for v in values], dtype=float)
        zero = np.zeros(len(a))
        if self._orient == '2d':
            return a[:, 0], a[:, 1]
        if self._orient == 'v':
            return zero, a[:, 0]
        return a[:, 0], zero

    def _tip(self, x=None, y=None, data=None, **_):
        try:
            p = self._points[int(data)]
        except Exception:
            return ''
        vals = '  '.join(f'{v:.{d}f}' for v, d in zip(p[1:], self._decimals))
        when = time.strftime('%d/%m %H:%M:%S', time.localtime(p[0])) if p[0] else ''
        return f'#{int(data)}   {vals}\n{when}\nclic = revenir ici'

    def _on_points_clicked(self, _item, pts, *args):
        if len(pts):
            idx = pts[-1].data()
            if idx is not None:
                self.point_clicked.emit(int(idx))


class AlignmentWidget(QtWidgets.QWidget):
    config_changed = QtCore.Signal()
    focus_changed = QtCore.Signal(bool)

    VALUE_FONT_PT = 13
    DETAIL_FONT_PT = 16
    CARD_MIN_W = 300
    CARD_NAME_W = 100

    def __init__(self, manual: 'ManualControl', shake_callback: Optional[Callable[[], None]] = None,
                 history_path: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.manual = manual
        self._shake = shake_callback
        self._axes: list = lab_default_axes()
        self._active = 0
        self._fast = False
        self._held: dict = {}             # qt key -> (axis_idx, 'h'|'v', direction)
        self._stepping: dict = {}         # role -> (channel cfg, signed direction)
        self._positions: dict = {}        # role -> float
        self._history: dict = {}
        self._settled: dict = {}
        self._history_path = Path(history_path) if history_path else None
        self._load_history()
        self._history_save_timer = QtCore.QTimer(self)
        self._history_save_timer.setSingleShot(True)
        self._history_save_timer.setInterval(1500)
        self._history_save_timer.timeout.connect(self._save_history)
        self._busy: set = set()
        self._pos_labels: dict = {}
        self._delta_labels: dict = {}
        self._delta_ref: dict = {}
        self._cards: list = []
        self._card_cols = 0
        self._hide_unassigned = False
        self._loading = False
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._apply_config_edit)
        self.manual.add_listener(self._on_value)
        self._setup_ui()
        self._rebuild()

    # ================================================================ UI
    def _setup_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.inner_tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.inner_tabs)
        self.page = KeyPage(self)
        self.page.focus_changed.connect(self.focus_changed)
        page_l = QtWidgets.QVBoxLayout(self.page)
        page_l.setContentsMargins(3, 3, 3, 3)
        status = QtWidgets.QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 2)
        status.setSpacing(8)
        self.kb_label = QtWidgets.QPushButton()
        self.kb_label.setFocusPolicy(QtCore.Qt.NoFocus)
        self.kb_label.clicked.connect(self.activate_keyboard)
        status.addWidget(self.kb_label)
        self.speed_label = QtWidgets.QLabel()
        status.addWidget(self.speed_label)
        self.key_label = QtWidgets.QLabel('touche : –')
        self.key_label.setStyleSheet('color: gray;')
        self.key_label.setMinimumWidth(80)
        status.addWidget(self.key_label)
        help_lbl = QtWidgets.QLabel('w/s ↕ · a/d ↔ · Maj rapide · 1…9,0 axe · Espace shaker')
        help_lbl.setStyleSheet('color: gray; font-style: italic;')
        status.addWidget(help_lbl)
        status.addStretch()
        self.chk_hide = QtWidgets.QCheckBox('Masquer non assignés')
        self.chk_hide.setFocusPolicy(QtCore.Qt.NoFocus)
        self.chk_hide.toggled.connect(self._on_hide_toggled)
        status.addWidget(self.chk_hide)
        btn_read = QtWidgets.QPushButton('⟳ Lire')
        btn_read.setToolTip('Lire la position de tous les actionneurs assignés')
        btn_read.setFocusPolicy(QtCore.Qt.NoFocus)
        btn_read.clicked.connect(self.read_all_positions)
        status.addWidget(btn_read)
        btn_stop = QtWidgets.QPushButton('■ STOP ALIGNEMENT')
        btn_stop.setToolTip('Arrête tous les actionneurs de l’onglet Alignement (souris uniquement)')
        btn_stop.setFocusPolicy(QtCore.Qt.NoFocus)
        btn_stop.setStyleSheet('background-color: #c0392b; color: white; font-weight: bold; padding: 1px 10px;')
        btn_stop.clicked.connect(self.stop_all)
        status.addWidget(btn_stop)
        page_l.addLayout(status)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        page_l.addWidget(splitter, stretch=1)
        self.pos_scroll = QtWidgets.QScrollArea()
        self.pos_scroll.setWidgetResizable(True)
        self.pos_scroll.setFocusPolicy(QtCore.Qt.NoFocus)
        self.pos_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.pos_scroll.setMinimumWidth(self.CARD_MIN_W + 24)      # at least one full column of cards
        self.card_container = _CardContainer(self._reflow_cards)
        self.card_grid = QtWidgets.QGridLayout(self.card_container)
        self.card_grid.setSpacing(5)
        self.card_grid.setContentsMargins(2, 2, 2, 2)
        self.pos_scroll.setWidget(self.card_container)
        splitter.addWidget(self.pos_scroll)
        right = QtWidgets.QWidget()
        right_l = QtWidgets.QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        self.detail = QtWidgets.QFrame()
        self.detail.setObjectName('detailPanel')
        self.detail_l = QtWidgets.QVBoxLayout(self.detail)
        self.detail_l.setContentsMargins(0, 0, 0, 4)
        self.detail_l.setSpacing(2)
        right_l.addWidget(self.detail)
        self.history_plot = AxisHistoryPlot()
        self.history_plot.point_clicked.connect(self._go_to_history_point)
        self.history_plot.clear_requested.connect(self._clear_active_history)
        right_l.addWidget(self.history_plot, stretch=1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([600, 500])
        self.inner_tabs.addTab(self.page, '🕹️ Alignement')

        cfg = QtWidgets.QWidget()
        cfg_l = QtWidgets.QHBoxLayout(cfg)
        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel('Axes (ordre = touches 1…9, 0) :'))
        self.axis_list = QtWidgets.QListWidget()
        self.axis_list.currentRowChanged.connect(self._load_axis_form)
        left.addWidget(self.axis_list)
        row = QtWidgets.QHBoxLayout()
        for text, slot in (('▲', lambda: self._move_axis(-1)), ('▼', lambda: self._move_axis(+1)),
                           ('＋', self._add_axis), ('✕', self._remove_axis)):
            b = QtWidgets.QPushButton(text)
            b.setFixedWidth(40)
            b.clicked.connect(slot)
            row.addWidget(b)
        left.addLayout(row)
        b_reset = QtWidgets.QPushButton('Disposition labo (Tk)')
        b_reset.clicked.connect(self._reset_layout)
        left.addWidget(b_reset)
        cfg_l.addLayout(left, stretch=1)
        form_w = QtWidgets.QWidget()
        form_l = QtWidgets.QVBoxLayout(form_w)
        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(QtWidgets.QLabel('Nom :'))
        self.f_name = QtWidgets.QLineEdit()
        self.f_name.textEdited.connect(self._on_form_edited)
        name_row.addWidget(self.f_name)
        self.f_color = QtWidgets.QPushButton('Couleur…')
        self.f_color.clicked.connect(self._pick_color)
        name_row.addWidget(self.f_color)
        form_l.addLayout(name_row)
        self.f_ch = {}
        ch_row = QtWidgets.QHBoxLayout()
        for key, title in (('h', '↔ Canal horizontal (a − / d +)'), ('v', '↕ Canal vertical (s − / w +)')):
            box = QtWidgets.QGroupBox(title)
            box.setCheckable(True)
            box.toggled.connect(self._on_form_edited)
            fl = QtWidgets.QFormLayout(box)
            w = {'box': box}
            w['actuator'] = QtWidgets.QComboBox()
            w['actuator'].currentTextChanged.connect(self._on_form_edited)
            w['invert'] = QtWidgets.QCheckBox('inverser le sens')
            w['invert'].toggled.connect(self._on_form_edited)
            w['mode'] = QtWidgets.QComboBox()
            w['mode'].addItems(JOG_MODES)
            for i, m in enumerate(JOG_MODES):
                w['mode'].setItemData(i, JOG_MODE_TIPS[m], QtCore.Qt.ToolTipRole)
            w['mode'].currentTextChanged.connect(self._on_form_edited)
            for k in ('slow', 'fast', 'min', 'max'):
                w[k] = QtWidgets.QDoubleSpinBox()
                w[k].setRange(-1e9, 1e9)
                w[k].setDecimals(4)
                w[k].valueChanged.connect(self._on_form_edited)
            w['label'] = QtWidgets.QLineEdit()
            w['label'].setPlaceholderText('nom court affiché : X, Y, Horiz, Z…')
            w['label'].textEdited.connect(self._on_form_edited)
            w['decimals'] = QtWidgets.QSpinBox()
            w['decimals'].setRange(0, 6)
            w['decimals'].valueChanged.connect(self._on_form_edited)
            w['speed_param'] = QtWidgets.QLineEdit()
            w['speed_param'].setPlaceholderText('ex. velocity  ou  group/velocity (mode limit)')
            w['speed_param'].textEdited.connect(self._on_form_edited)
            w['warn'] = QtWidgets.QLabel('')
            w['warn'].setWordWrap(True)
            w['warn'].setStyleSheet('color: #c0392b;')
            fl.addRow('Actionneur (rôle) :', w['actuator'])
            fl.addRow('Libellé :', w['label'])
            fl.addRow('Décimales :', w['decimals'])
            fl.addRow('', w['invert'])
            fl.addRow('Mode de jog :', w['mode'])
            fl.addRow('Lent :', w['slow'])
            fl.addRow('Rapide (Maj) :', w['fast'])
            fl.addRow('Min (mode limit) :', w['min'])
            fl.addRow('Max (mode limit) :', w['max'])
            fl.addRow('Paramètre vitesse :', w['speed_param'])
            fl.addRow(w['warn'])
            ch_row.addWidget(box)
            self.f_ch[key] = w
        form_l.addLayout(ch_row)
        modes_help = QtWidgets.QLabel('<br>'.join(f'<b>{m}</b> : {t}' for m, t in JOG_MODE_TIPS.items()))
        modes_help.setWordWrap(True)
        modes_help.setStyleSheet('color: gray;')
        form_l.addWidget(modes_help)
        form_l.addStretch()
        form_scroll = QtWidgets.QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        form_scroll.setWidget(form_w)
        cfg_l.addWidget(form_scroll, stretch=3)
        self.inner_tabs.addTab(cfg, '⚙️ Configuration')
        self._update_speed_label()
        self.set_keyboard_active(False)
        for b in self.findChildren(QtWidgets.QAbstractButton):
            if b is not self.kb_label:
                b.setFocusPolicy(QtCore.Qt.NoFocus)

    def set_keyboard_active(self, active: bool):
        if active:
            self.kb_label.setText('⌨ Clavier : ACTIF')
            self.kb_label.setStyleSheet('background-color: #27ae60; color: white; font-weight: bold; padding: 2px 8px;')
        else:
            self.kb_label.setText('⌨ Clavier : inactif — cliquer')
            self.kb_label.setStyleSheet('background-color: #7f8c8d; color: white; font-weight: bold; padding: 2px 8px;')

    def _spin(self, val: float) -> QtWidgets.QDoubleSpinBox:
        sb = QtWidgets.QDoubleSpinBox()
        sb.setRange(-1e9, 1e9)
        sb.setDecimals(4)
        sb.setValue(val)
        sb.setFocusPolicy(QtCore.Qt.ClickFocus)
        sb.setKeyboardTracking(False)
        sb.setMinimumWidth(72)
        sb.setMaximumWidth(120)
        sb.lineEdit().returnPressed.connect(self.activate_keyboard)
        return sb

    # ================================================================ view building
    @staticmethod
    def _axis_assigned(ax: dict) -> bool:
        return any(ax.get(k) and ax[k].get('actuator', 'None') != 'None' for k in ('h', 'v'))

    def _effective_mode(self, ch: dict) -> str:
        mode = ch.get('mode', 'auto')
        if mode == 'auto':
            role = ch.get('actuator', 'None')
            return 'native' if role != 'None' and self.manual.supports_native_jog(role) else 'step'
        return mode

    def _rebuild(self):
        self.release_all_jogs()
        self._pos_labels = {}
        for card in self._cards:
            if card is not None:
                card.setParent(None)
                card.deleteLater()
        self._cards = []
        for i, ax in enumerate(self._axes):
            if self._hide_unassigned and not self._axis_assigned(ax):
                self._cards.append(None)
                continue
            card = self._make_card(i, ax)
            card.clicked.connect(lambda n=i: self.select_axis(n))
            self._cards.append(card)
        self._card_cols = 0
        self._reflow_cards(self.card_container.width())
        for role in self._all_actuators():
            pos = self.manual.position(role)
            if pos is not None:
                self._positions[role] = pos
                self._settled.setdefault(role, pos)
        for act, val in self._positions.items():
            self._show_position(act, val)
        for act in self._busy:
            self._set_busy(act, True)
        self._refresh_config_list()
        self.select_axis(min(self._active, max(len(self._axes) - 1, 0)), stop=False)

    def _make_card(self, i: int, ax: dict) -> _AxisCard:
        color = axis_color(ax, i)
        key = AXIS_KEY_LABELS[i] if i < len(AXIS_KEY_LABELS) else '·'
        card = _AxisCard()
        card.setObjectName('axisCard')
        card.setCursor(QtCore.Qt.PointingHandCursor)
        card.setMinimumWidth(self.CARD_MIN_W - 16)
        card.setProperty('axis_color', color)
        h = QtWidgets.QHBoxLayout(card)
        h.setContentsMargins(0, 0, 6, 0)
        h.setSpacing(6)
        band = QtWidgets.QLabel(key)
        band.setObjectName('cardBand')
        band.setAlignment(QtCore.Qt.AlignCenter)
        band.setFixedWidth(22)
        band.setStyleSheet(f'#cardBand {{ background-color: {color}; color: white; font-weight: bold; '
                           f'font-size: 15px; border-top-left-radius: 5px; border-bottom-left-radius: 5px; }}')
        h.addWidget(band)
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(0)
        grid.setColumnStretch(2, 1)
        name = QtWidgets.QLabel(ax['name'])
        name.setStyleSheet(f'color: {color}; font-weight: bold; font-size: 12px;')
        name.setFixedWidth(self.CARD_NAME_W)
        grid.addWidget(name, 0, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        tips = [ax['name']]
        r = 0
        for ch_key in ('h', 'v'):
            ch = ax.get(ch_key)
            if not ch:
                continue
            act = ch.get('actuator', 'None')
            warn = self._effective_mode(ch) == 'limit' and act != 'None'
            lab = QtWidgets.QLabel(channel_label(ch, ch_key) + (' ⚠' if warn else ''))
            lab.setStyleSheet(f'color: {color}; font-weight: bold; font-size: 11px;')
            if warn:
                lab.setToolTip(JOG_MODE_TIPS['limit'])
            grid.addWidget(lab, r, 1, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            val = QtWidgets.QLabel('—')
            val.setFont(mono_font(self.VALUE_FONT_PT))
            val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            grid.addWidget(val, r, 2)
            known = act != 'None' and self.manual.exists(act)
            unit = QtWidgets.QLabel(self.manual.units(act) if known else '')
            unit.setStyleSheet('color: gray; font-size: 11px;')
            unit.setFixedWidth(26)
            grid.addWidget(unit, r, 3, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            state = 'non assigné' if act == 'None' else (act if known else f'{act} (absent !)')
            tips.append(f"{channel_label(ch, ch_key)} : {state}   ({KEY_HINT[ch_key]})")
            if act != 'None':
                self._pos_labels.setdefault(act, []).append((val, int(ch.get('decimals', 4))))
            r += 1
        if r == 0:
            grid.addWidget(QtWidgets.QLabel('<i>aucun canal</i>'), 0, 1, 1, 3)
        h.addLayout(grid, stretch=1)
        card.setToolTip('\n'.join(tips))
        if not self._axis_assigned(ax):
            eff = QtWidgets.QGraphicsOpacityEffect(card)
            eff.setOpacity(0.45)
            card.setGraphicsEffect(eff)
        return card

    def _reflow_cards(self, width: int):
        cols = max(1, int(width) // self.CARD_MIN_W) if width > 0 else 3
        if cols == self._card_cols:
            return
        self._card_cols = cols
        while self.card_grid.count():
            self.card_grid.takeAt(0)
        for r in range(self.card_grid.rowCount()):
            self.card_grid.setRowStretch(r, 0)
        for c in range(self.card_grid.columnCount()):
            self.card_grid.setColumnStretch(c, 0)
        n = 0
        for card in self._cards:
            if card is None:
                continue
            self.card_grid.addWidget(card, n // cols, n % cols, QtCore.Qt.AlignTop)
            n += 1
        for c in range(cols):
            self.card_grid.setColumnStretch(c, 1)
        self.card_grid.setRowStretch((n + cols - 1) // cols, 1)

    def _build_detail(self):
        while self.detail_l.count():
            item = self.detail_l.takeAt(0)
            w = item.widget()
            sub = item.layout()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif sub is not None:
                self._clear_layout(sub)
        for act, lbls in list(self._pos_labels.items()):
            self._pos_labels[act] = [x for x in lbls if not x[0].property('detail')]
        self._delta_labels = {}
        if not self._axes:
            return
        idx = self._active
        ax = self._axes[idx]
        color = axis_color(ax, idx)
        key = AXIS_KEY_LABELS[idx] if idx < len(AXIS_KEY_LABELS) else '·'
        self.detail.setStyleSheet(f'#detailPanel {{ border: 3px solid {color}; border-radius: 7px; }}')
        header = QtWidgets.QFrame()
        header.setObjectName('detailHeader')
        header.setStyleSheet(f'#detailHeader {{ background-color: {color}; border-top-left-radius: 4px; '
                             f'border-top-right-radius: 4px; }}')
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(4, 1, 4, 1)
        title_lbl = QtWidgets.QLabel(
            f"<span style='font-size:15px; font-weight:bold;'>&nbsp;{key}&nbsp;</span>"
            f"<span style='font-size:13px; font-weight:bold;'>&nbsp;{ax['name']}</span>"
            f"<span style='font-size:10px;'>&nbsp;&nbsp;axe actif</span>")
        title_lbl.setStyleSheet('color: white;')
        hl.addWidget(title_lbl)
        hl.addStretch()
        btn_reset = QtWidgets.QPushButton('Remise à zéro Δ')
        btn_reset.setFocusPolicy(QtCore.Qt.NoFocus)
        btn_reset.setStyleSheet('QPushButton { color: white; border: 1px solid rgba(255,255,255,140); '
                                'border-radius: 3px; padding: 0px 6px; font-size: 11px; }')
        btn_reset.clicked.connect(self._reset_delta)
        hl.addWidget(btn_reset)
        self.detail_l.addWidget(header)
        body = QtWidgets.QGridLayout()
        body.setContentsMargins(8, 1, 8, 0)
        body.setHorizontalSpacing(6)
        body.setVerticalSpacing(1)
        body.setColumnStretch(1, 1)
        r = 0
        n_ch = sum(1 for k in ('h', 'v') if ax.get(k))
        for ch_key in ('h', 'v'):
            ch = ax.get(ch_key)
            if not ch:
                continue
            act = ch.get('actuator', 'None')
            dec = int(ch.get('decimals', 4))
            known = act != 'None' and self.manual.exists(act)
            owner = self.manual.owner_label(act) if known else ''
            title = QtWidgets.QLabel(
                f"<span style='color:{color}; font-size:13px; font-weight:bold;'>{channel_label(ch, ch_key)}</span>"
                f"<br><span style='color:gray; font-size:10px;'>{act if act != 'None' else 'non assigné'}"
                f"{' · ' + owner if owner and owner != 'Libre' else ''}</span>")
            title.setToolTip(f'touches {KEY_HINT[ch_key]} · mode {self._effective_mode(ch)}')
            body.addWidget(title, r, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            val = QtWidgets.QLabel('—')
            val.setProperty('detail', True)
            val.setFont(mono_font(self.DETAIL_FONT_PT))
            val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            body.addWidget(val, r, 1)
            unit = QtWidgets.QLabel(self.manual.units(act) if known else '')
            unit.setStyleSheet('color: gray; font-size: 12px;')
            body.addWidget(unit, r, 2)
            delta = QtWidgets.QLabel('')
            delta.setFont(mono_font(10, bold=False))
            delta.setMinimumWidth(88)
            delta.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            delta.setToolTip('Déplacement depuis la sélection de l’axe (Remise à zéro Δ pour recommencer)')
            body.addWidget(delta, r, 3, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            r += 1
            if act != 'None':
                self._pos_labels.setdefault(act, []).append((val, dec))
                self._delta_labels[act] = (delta, dec)
                self._delta_ref.setdefault(act, self._positions.get(act))
                ctrl = QtWidgets.QHBoxLayout()
                target = self._spin(self._positions.get(act, 0.0) or 0.0)
                target.setDecimals(max(dec, 1))
                target.setToolTip('Cible absolue')
                btn_go = QtWidgets.QPushButton('Go')
                btn_go.setFocusPolicy(QtCore.Qt.NoFocus)
                btn_go.setMaximumWidth(60)
                btn_go.setStyleSheet('background-color: #2980b9; color: white; font-weight: bold; padding: 1px 10px;')
                btn_go.clicked.connect(lambda _, a=act, t=target: self.move_abs(a, t.value()))
                step = self._spin(ch.get('rel_step', 5.0 if ch_key == 'h' else 1.0))
                step.setMinimum(0)
                step.setToolTip('Pas relatif pour − / +')
                step.valueChanged.connect(lambda v, c=ch: c.__setitem__('rel_step', v))
                b_m = QtWidgets.QPushButton('−')
                b_p = QtWidgets.QPushButton('+')
                for b in (b_m, b_p):
                    b.setFocusPolicy(QtCore.Qt.NoFocus)
                    b.setFixedWidth(28)
                b_m.clicked.connect(lambda _, a=act, s=step: self.move_rel(a, -s.value()))
                b_p.clicked.connect(lambda _, a=act, s=step: self.move_rel(a, +s.value()))
                lt = QtWidgets.QLabel('aller à')
                lt.setStyleSheet('color: gray;')
                ls = QtWidgets.QLabel('pas')
                ls.setStyleSheet('color: gray;')
                ctrl.setSpacing(4)
                for w in (lt, target, btn_go):
                    ctrl.addWidget(w)
                ctrl.addStretch(1)
                for w in (ls, step, b_m, b_p):
                    ctrl.addWidget(w)
                body.addLayout(ctrl, r, 0, 1, 4)
                r += 1
            n_ch -= 1
            if n_ch > 0:
                sep = QtWidgets.QFrame()
                sep.setFrameShape(QtWidgets.QFrame.HLine)
                sep.setStyleSheet('color: rgba(127,127,127,90);')
                body.addWidget(sep, r, 0, 1, 4)
                r += 1
        self.detail_l.addLayout(body)
        for act in self._delta_labels:
            if act in self._positions:
                self._show_position(act, self._positions[act])
            self._set_busy(act, act in self._busy)

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            sub = item.layout()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif sub is not None:
                AlignmentWidget._clear_layout(sub)

    def _reset_delta(self):
        for act in self._delta_labels:
            self._delta_ref[act] = self._positions.get(act)
            if act in self._positions:
                self._show_position(act, self._positions[act])
        self.activate_keyboard()

    def _on_hide_toggled(self, checked: bool):
        self._hide_unassigned = checked
        self._rebuild()
        self.config_changed.emit()
        self.activate_keyboard()

    def _pick_color(self):
        row = self.axis_list.currentRow()
        if not 0 <= row < len(self._axes):
            return
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(axis_color(self._axes[row], row)), self, 'Couleur de l’axe')
        if c.isValid():
            self._axes[row]['color'] = c.name()
            self._apply_config_edit()

    def _all_actuators(self) -> list:
        acts = []
        for ax in self._axes:
            for k in ('h', 'v'):
                ch = ax.get(k)
                if ch and ch.get('actuator', 'None') != 'None' and ch['actuator'] not in acts:
                    acts.append(ch['actuator'])
        return acts

    # ================================================================ display
    def _on_value(self, role: str, value: float, done: bool):
        if role not in self._pos_labels and role not in self._stepping and role not in self._all_actuators():
            return
        if value is not None:
            self._positions[role] = value
            self._show_position(role, value)
            if role in self._active_acts():
                self.history_plot.update_live(self._live_point())
        if not done:
            return
        self._set_busy(role, False)
        if value is not None:
            self._record_history(role, value)
        if role in self._stepping:                     # step mode: chain while the key is held
            ch, sgn = self._stepping[role]
            if self.manual.move_rel(role, sgn * (ch['fast'] if self._fast else ch['slow']), 'alignement'):
                self._set_busy(role, True)
            else:
                self._stepping.pop(role, None)

    def _show_position(self, act: str, val: float):
        for lbl, dec in self._pos_labels.get(act, []):
            lbl.setText(f'{val:.{dec}f}')
        if act in self._delta_labels:
            lbl, dec = self._delta_labels[act]
            ref = self._delta_ref.get(act)
            if ref is None:
                self._delta_ref[act] = ref = val
            lbl.setText(f'Δ {val - ref:+.{dec}f}')

    def _set_busy(self, act: str, busy: bool):
        if busy:
            self._busy.add(act)
        else:
            self._busy.discard(act)
        style = ('background-color: #e67e22; color: white; border-radius: 4px; padding: 0px 6px;'
                 if busy else 'padding: 0px 6px;')
        for lbl, _ in self._pos_labels.get(act, []):
            lbl.setStyleSheet(style)

    def _update_speed_label(self):
        self.speed_label.setText('⏩ RAPIDE' if self._fast else 'vitesse lente')
        self.speed_label.setStyleSheet('color: #e67e22; font-weight: bold;' if self._fast else 'color: gray;')

    def activate_keyboard(self):
        if self.inner_tabs.currentWidget() is self.page and self.page.isVisible():
            self.page.setFocus(QtCore.Qt.OtherFocusReason)

    # ================================================================ axis select
    def select_axis(self, idx: int, stop: bool = True):
        if not self._axes or idx >= len(self._axes):
            return
        if stop:
            self.release_all_jogs()
        if idx != self._active:
            self._delta_ref = {}
        self._active = idx
        for i, card in enumerate(self._cards):
            if card is None:
                continue
            color = card.property('axis_color')
            if i == idx:
                qc = QtGui.QColor(color)
                card.setStyleSheet(f'#axisCard {{ border: 2px solid {color}; border-radius: 6px; '
                                   f'background-color: rgba({qc.red()},{qc.green()},{qc.blue()},40); }}')
            else:
                card.setStyleSheet('#axisCard { border: 2px solid rgba(127,127,127,70); border-radius: 6px; }')
        self._build_detail()
        self._refresh_history_view(refit=True)
        if 0 <= idx < len(self._cards) and self._cards[idx] is not None:
            self.pos_scroll.ensureWidgetVisible(self._cards[idx], 0, 20)
        self.activate_keyboard()

    # ================================================================ history
    @staticmethod
    def _axis_channels(ax: dict) -> list:
        return [(k, ax[k]) for k in ('h', 'v') if ax.get(k) and ax[k].get('actuator', 'None') != 'None']

    def _active_acts(self) -> list:
        if not self._axes or not 0 <= self._active < len(self._axes):
            return []
        return [ch['actuator'] for _, ch in self._axis_channels(self._axes[self._active])]

    def _live_point(self) -> list:
        return [self._positions.get(a) for a in self._active_acts()]

    def _record_history(self, act: str, val: float):
        before_all = dict(self._settled)
        self._settled[act] = val
        now = time.time()
        done_keys = set()
        refresh = False
        active_key = '|'.join(self._active_acts())
        for ax in self._axes:
            acts = [ch['actuator'] for _, ch in self._axis_channels(ax)]
            key = '|'.join(acts)
            if act not in acts or key in done_keys:
                continue
            done_keys.add(key)
            after = [self._settled.get(a) for a in acts]
            if any(v is None for v in after):
                continue
            before = [before_all.get(a) for a in acts]
            hist = self._history.setdefault(key, [])
            if not any(v is None for v in before) and (not hist or not _same_point(hist[-1][1:], before)) \
                    and not _same_point(before, after):
                hist.append([now, *before])
            if not hist or not _same_point(hist[-1][1:], after):
                hist.append([now, *after])
                if len(hist) > ALIGN_HISTORY_MAX:
                    del hist[:len(hist) - ALIGN_HISTORY_MAX]
                self._history_save_timer.start()
                refresh = refresh or key == active_key
        if refresh:
            self._refresh_history_view()

    def _refresh_history_view(self, refit: bool = False):
        if not self._axes or not 0 <= self._active < len(self._axes):
            self.history_plot.set_axis('—', '#888', None, [], [], [])
            self.history_plot.set_points([], None)
            return
        ax = self._axes[self._active]
        chans = self._axis_channels(ax)
        labels, units, decs = [], [], []
        for k, ch in chans:
            labels.append(channel_label(ch, k))
            units.append(self.manual.units(ch['actuator']) if self.manual.exists(ch['actuator']) else '')
            decs.append(int(ch.get('decimals', 4)))
        keys = [k for k, _ in chans]
        orient = '2d' if len(keys) == 2 else (keys[0] if keys else None)
        self.history_plot.set_axis(ax['name'], axis_color(ax, self._active), orient, labels, units, decs)
        key = '|'.join(ch['actuator'] for _, ch in chans)
        self.history_plot.set_points(self._history.get(key, []) if chans else [], self._live_point())
        if refit:
            self.history_plot.fit()

    def _go_to_history_point(self, i: int):
        acts = self._active_acts()
        hist = self._history.get('|'.join(acts), [])
        if not acts or not 0 <= i < len(hist):
            return
        p = hist[i]
        ax = self._axes[self._active]
        decs = [int(ch.get('decimals', 4)) for _, ch in self._axis_channels(ax)]
        txt = '\n'.join(f'{a} → {v:.{d}f}' for a, v, d in zip(acts, p[1:], decs))
        when = time.strftime('%d/%m %H:%M:%S', time.localtime(p[0])) if p[0] else '?'
        self.release_all_jogs()
        if QtWidgets.QMessageBox.question(self, 'Revenir dans l’historique',
                                          f"Ramener {ax['name']} au point #{i} ({when}) ?\n\n{txt}") == \
                QtWidgets.QMessageBox.Yes:
            for a, v in zip(acts, p[1:]):
                self.move_abs(a, v)
        self.activate_keyboard()

    def _clear_active_history(self):
        acts = self._active_acts()
        key = '|'.join(acts)
        n = len(self._history.get(key, []))
        if not acts or n == 0:
            self.activate_keyboard()
            return
        name = self._axes[self._active]['name']
        if QtWidgets.QMessageBox.question(self, 'Effacer l’historique',
                                          f'Effacer les {n} positions enregistrées de {name} ?') == \
                QtWidgets.QMessageBox.Yes:
            self._history.pop(key, None)
            self._save_history()
            self._refresh_history_view(refit=True)
        self.activate_keyboard()

    def _load_history(self):
        if self._history_path is None or not self._history_path.exists():
            return
        try:
            with open(self._history_path, 'r', encoding='utf-8') as f:
                raw = json.load(f).get('series', {})
            for key, pts in raw.items():
                clean = [[float(x) for x in p] for p in pts if isinstance(p, list) and len(p) >= 2]
                if clean:
                    self._history[str(key)] = clean[-ALIGN_HISTORY_MAX:]
        except Exception as e:
            logger.warning(f'Alignment history could not be read ({self._history_path}): {e}')

    def _save_history(self):
        if self._history_path is None:
            return
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._history_path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'series': self._history}, f)
            os.replace(tmp, self._history_path)
        except Exception as e:
            logger.warning(f'Alignment history could not be saved: {e}')

    def flush_history(self):
        if self._history_save_timer.isActive():
            self._history_save_timer.stop()
        self._save_history()

    # ================================================================ keyboard
    def handle_key_press(self, e: QtGui.QKeyEvent) -> bool:
        key = e.key()
        if key == QtCore.Qt.Key_Shift:
            if not e.isAutoRepeat():
                self._set_fast(True)
            return True
        if e.isAutoRepeat():
            return key in JOG_KEYS or key in AXIS_KEYS or key == QtCore.Qt.Key_Space
        if e.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
            return False
        self.key_label.setText(f'touche : {e.text() or QtGui.QKeySequence(key).toString()}')
        if key in AXIS_KEYS:
            idx = AXIS_KEYS.index(key)
            if idx < len(self._axes):
                self.select_axis(idx)
            return True
        if key == QtCore.Qt.Key_Space:
            if self._shake is not None:
                self._shake()
            return True
        if key in JOG_KEYS:
            self._set_fast(bool(e.modifiers() & QtCore.Qt.ShiftModifier))
            ch_key, direction = JOG_KEYS[key]
            self._held[key] = (self._active, ch_key, direction)
            self._start_jog(self._active, ch_key, direction)
            return True
        return False

    def handle_key_release(self, e: QtGui.QKeyEvent) -> bool:
        key = e.key()
        if key == QtCore.Qt.Key_Shift:
            if not e.isAutoRepeat():
                self._set_fast(False)
            return True
        if key not in JOG_KEYS:
            return False
        if e.isAutoRepeat():
            return True
        held = self._held.pop(key, None)
        if held is None:
            return True
        axis_idx, ch_key, _ = held
        for other in self._held.values():
            if other[0] == axis_idx and other[1] == ch_key:
                self._start_jog(*other)
                return True
        self._stop_jog(axis_idx, ch_key)
        return True

    def _set_fast(self, fast: bool):
        if fast == self._fast:
            return
        self._fast = fast
        self._update_speed_label()
        done = set()
        for axis_idx, ch_key, direction in list(self._held.values()):
            if (axis_idx, ch_key) not in done:
                done.add((axis_idx, ch_key))
                ch = self._axes[axis_idx].get(ch_key) if axis_idx < len(self._axes) else None
                if ch and self._effective_mode(ch) in ('native', 'limit'):
                    self._start_jog(axis_idx, ch_key, direction)

    # ================================================================ jog
    def _start_jog(self, axis_idx: int, ch_key: str, direction: int):
        if axis_idx >= len(self._axes):
            return
        ch = self._axes[axis_idx].get(ch_key)
        if not ch:
            return
        act = ch.get('actuator', 'None')
        if act == 'None' or not self.manual.exists(act):
            self.key_label.setText(f"⚠ aucun actionneur sur {self._axes[axis_idx]['name']} {ch_key.upper()}")
            return
        sgn = -direction if ch.get('invert') else direction
        speed = abs(ch['fast'] if self._fast else ch['slow'])
        mode = self._effective_mode(ch)
        ok = False
        if mode == 'native':
            ok = self.manual.jog_native(act, sgn, speed, 'alignement')
        elif mode == 'limit':
            path = [p for p in str(ch.get('speed_param', '')).replace('.', '/').split('/') if p]
            ok = self.manual.jog_limit(act, ch['max'] if sgn > 0 else ch['min'], path, speed, 'alignement')
        else:
            first = act not in self._stepping
            self._stepping[act] = (ch, sgn)
            ok = True
            if first and act not in self._busy:
                ok = self.manual.move_rel(act, sgn * speed, 'alignement')
                if not ok:
                    self._stepping.pop(act, None)
        if ok:
            self._set_busy(act, True)

    def _stop_jog(self, axis_idx: int, ch_key: str):
        if axis_idx >= len(self._axes):
            return
        ch = self._axes[axis_idx].get(ch_key)
        if not ch:
            return
        act = ch.get('actuator', 'None')
        if act == 'None' or not self.manual.exists(act):
            return
        if act in self._stepping:
            self._stepping.pop(act, None)      # the current step finishes, no new one
        else:
            self.manual.stop(act, 'alignement')

    def release_all_jogs(self):
        held = list(self._held.values())
        self._held.clear()
        stopped = set()
        for axis_idx, ch_key, _ in held:
            if (axis_idx, ch_key) not in stopped:
                stopped.add((axis_idx, ch_key))
                self._stop_jog(axis_idx, ch_key)
        self._stepping.clear()
        if self._fast:
            self._fast = False
            self._update_speed_label()

    def stop_all(self):
        self.release_all_jogs()
        for act in self._all_actuators():
            if self.manual.exists(act):
                self.manual.stop(act, 'stop alignement')
        self.activate_keyboard()

    # ================================================================ abs/rel moves
    def move_abs(self, act: str, value: float):
        if self.manual.move_abs(act, value, 'alignement'):
            self._set_busy(act, True)
        self.activate_keyboard()

    def move_rel(self, act: str, delta: float):
        if self.manual.move_rel(act, delta, 'alignement'):
            self._set_busy(act, True)
        self.activate_keyboard()

    def read_all_positions(self):
        self.manual.read_positions(self._all_actuators())
        self.activate_keyboard()

    # ================================================================ configuration
    def _refresh_config_list(self):
        row = self.axis_list.currentRow()
        self.axis_list.blockSignals(True)
        self.axis_list.clear()
        for i, ax in enumerate(self._axes):
            key = f'{AXIS_KEY_LABELS[i]})' if i < len(AXIS_KEY_LABELS) else '(pas de touche)'
            self.axis_list.addItem(f"{key} {ax['name']}")
            self.axis_list.item(i).setForeground(QtGui.QBrush(QtGui.QColor(axis_color(ax, i))))
        self.axis_list.blockSignals(False)
        if self._axes:
            self.axis_list.setCurrentRow(min(max(row, 0), len(self._axes) - 1))
        self._load_axis_form(self.axis_list.currentRow())

    def _load_axis_form(self, row: int):
        self._loading = True
        try:
            names = ['None'] + self.manual.actuator_roles()
            ok = 0 <= row < len(self._axes)
            ax = self._axes[row] if ok else {'name': ''}
            self.f_name.setText(ax.get('name', ''))
            for k, w in self.f_ch.items():
                ch = ax.get(k)
                w['box'].setChecked(bool(ch))
                ch = ch or _channel()
                combo = w['actuator']
                combo.clear()
                items = list(names)
                if ch['actuator'] not in items:
                    items.append(ch['actuator'])
                combo.addItems(items)
                combo.setCurrentText(ch['actuator'])
                w['invert'].setChecked(bool(ch.get('invert')))
                w['label'].setText(ch.get('label', ''))
                w['decimals'].setValue(int(ch.get('decimals', 4)))
                w['mode'].setCurrentText(ch.get('mode', 'auto'))
                for f in ('slow', 'fast', 'min', 'max'):
                    w[f].setValue(float(ch.get(f, 0.0)))
                w['speed_param'].setText(ch.get('speed_param', ''))
                w['warn'].setText(JOG_MODE_TIPS['limit'] if ch.get('mode') == 'limit' else '')
        finally:
            self._loading = False

    def _on_form_edited(self, *args):
        if self._loading:
            return
        row = self.axis_list.currentRow()
        if not 0 <= row < len(self._axes):
            return
        ax = self._axes[row]
        ax['name'] = self.f_name.text().strip() or ax['name']
        for k, w in self.f_ch.items():
            if not w['box'].isChecked():
                ax[k] = None
                continue
            ch = ax.get(k) or _channel()
            ch.update({'actuator': w['actuator'].currentText() or 'None', 'invert': w['invert'].isChecked(),
                       'mode': w['mode'].currentText(), 'slow': w['slow'].value(), 'fast': w['fast'].value(),
                       'min': w['min'].value(), 'max': w['max'].value(),
                       'speed_param': w['speed_param'].text().strip(), 'label': w['label'].text().strip(),
                       'decimals': w['decimals'].value()})
            ax[k] = ch
            w['warn'].setText(JOG_MODE_TIPS['limit'] if ch['mode'] == 'limit' else '')
        item = self.axis_list.item(row)
        if item is not None:
            key = f'{AXIS_KEY_LABELS[row]})' if row < len(AXIS_KEY_LABELS) else '(pas de touche)'
            item.setText(f"{key} {ax['name']}")
        self._save_timer.start()

    def _apply_config_edit(self):
        self._rebuild()
        self.config_changed.emit()

    def _move_axis(self, delta: int):
        row = self.axis_list.currentRow()
        new = row + delta
        if 0 <= row < len(self._axes) and 0 <= new < len(self._axes):
            self._axes[row], self._axes[new] = self._axes[new], self._axes[row]
            if self._active == row:
                self._active = new
            elif self._active == new:
                self._active = row
            self._apply_config_edit()
            self.axis_list.setCurrentRow(new)

    def _add_axis(self):
        self._axes.append({'name': f'Axe {len(self._axes) + 1}', 'h': _channel(), 'v': None})
        self._apply_config_edit()
        self.axis_list.setCurrentRow(len(self._axes) - 1)

    def _remove_axis(self):
        row = self.axis_list.currentRow()
        if 0 <= row < len(self._axes):
            name = self._axes[row]['name']
            if QtWidgets.QMessageBox.question(self, 'Supprimer l’axe', f'Supprimer « {name} » ?') == \
                    QtWidgets.QMessageBox.Yes:
                self._axes.pop(row)
                self._active = min(self._active, max(len(self._axes) - 1, 0))
                self._apply_config_edit()

    def _reset_layout(self):
        if QtWidgets.QMessageBox.question(
                self, 'Disposition labo',
                'Remplacer la liste des axes par la disposition du labo (Tk) ?\n'
                'Les actionneurs assignés sont conservés pour les axes de même nom.') != QtWidgets.QMessageBox.Yes:
            return
        old = {ax['name']: ax for ax in self._axes}
        new = lab_default_axes()
        for ax in new:
            prev = old.get(ax['name'])
            if prev:
                for k in ('h', 'v'):
                    if ax.get(k) and prev.get(k):
                        ax[k]['actuator'] = prev[k].get('actuator', 'None')
        self._axes = new
        self._active = 0
        self._apply_config_edit()

    def set_hardware(self):
        self._rebuild()

    # ================================================================ I/O
    def get_config(self) -> dict:
        return {'axes': self._axes, 'active': self._active, 'hide_unassigned': self._hide_unassigned}

    def load_config(self, cfg: Optional[dict]):
        self.release_all_jogs()
        cfg = cfg or {}
        axes = cfg.get('axes')
        self._axes = [dict(a) for a in axes] if isinstance(axes, list) and axes else lab_default_axes()
        for ax in self._axes:
            for k in ('h', 'v'):
                if isinstance(ax.get(k), dict):
                    ax[k] = dict(ax[k])
        self._active = int(cfg.get('active', 0))
        self._hide_unassigned = bool(cfg.get('hide_unassigned', False))
        self.chk_hide.blockSignals(True)
        self.chk_hide.setChecked(self._hide_unassigned)
        self.chk_hide.blockSignals(False)
        self._delta_ref = {}
        self._rebuild()

    def load_from_preset(self, preset: dict):
        self.load_config(preset.get('alignment') or {})

    def write_to_preset(self, preset: dict):
        import copy
        preset['alignment'] = copy.deepcopy(self.get_config())

    def refresh(self):
        # busy flags may be cleared by a stop elsewhere (E-STOP): resync with the broker
        for act in list(self._busy):
            if not self.manual.is_busy(act) and act not in self._stepping:
                self._set_busy(act, False)

    def shutdown(self):
        self.release_all_jogs()
        self.flush_history()
        self.manual.remove_listener(self._on_value)
