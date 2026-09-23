"""XY tab (cahier des charges T2.3 / T5.4), ported from the XY Stage Controller.

One sub-tab per XY group: two actuator roles, jog pad + w/a/s/d keyboard (Shift x10), saved
positions, 2D map (click to move), snake 'hardware' scan. Every order goes through the broker
(human preemption D5/S7); the snake scan is an automation of the core (bounded relative chunks,
explicit envelope = the scan rectangle, stopped by any manual action).
"""
from __future__ import annotations

import copy
from collections import deque
from typing import TYPE_CHECKING, Dict, List, Optional

import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from ..core.states import AutoState, ENGAGED_STATES
from ..engines.scan import XYScanAutomation, snake_pattern
from .widgets import ask_yes_no, info, set_combo_items, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

HISTORY_MAX_LEN = 2000
XY_JOG_KEYS = {QtCore.Qt.Key_W: ('y', +1), QtCore.Qt.Key_S: ('y', -1),
               QtCore.Qt.Key_D: ('x', +1), QtCore.Qt.Key_A: ('x', -1)}


class ActuatorWidget(QtWidgets.QGroupBox):
    move_requested = QtCore.Signal(float)
    stop_requested = QtCore.Signal()

    def __init__(self, axis_name: str, parent=None):
        super().__init__(parent)
        self.setTitle(axis_name)
        self._current_raw = 0.0
        self._has_position = False
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(6, 2, 6, 4)
        layout.setHorizontalSpacing(4)
        layout.setVerticalSpacing(3)
        layout.setColumnStretch(1, 1)

        def spin(val=0.0, vmin=-1e9):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(vmin, 1e9)
            sb.setDecimals(3)
            sb.setValue(val)
            sb.setFocusPolicy(QtCore.Qt.ClickFocus)
            sb.setMinimumWidth(72)
            return sb

        layout.addWidget(QtWidgets.QLabel('Pos'), 0, 0)
        self.current_pos_label = QtWidgets.QLabel('N/A')
        self.current_pos_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        f = QtGui.QFont()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        self.current_pos_label.setFont(f)
        layout.addWidget(self.current_pos_label, 0, 1)
        self.unit_labels = [QtWidgets.QLabel('u.a.') for _ in range(3)]
        layout.addWidget(self.unit_labels[0], 0, 2)
        self.status_label = QtWidgets.QLabel('Repos')
        self.status_label.setStyleSheet('color: gray; font-size: 11px;')
        self.status_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        layout.addWidget(self.status_label, 0, 3, 1, 2)
        layout.addWidget(QtWidgets.QLabel('Cible'), 1, 0)
        self.target_spinbox = spin()
        layout.addWidget(self.target_spinbox, 1, 1)
        layout.addWidget(self.unit_labels[1], 1, 2)
        self.btn_move_abs = QtWidgets.QPushButton('Go')
        self.btn_move_abs.setStyleSheet('background-color: #2980b9; color: white; font-weight: bold; padding: 1px 8px;')
        self.btn_stop = QtWidgets.QPushButton('STOP')
        self.btn_stop.setStyleSheet('background-color: #c0392b; color: white; font-weight: bold; padding: 1px 6px;')
        for b in (self.btn_move_abs, self.btn_stop):
            b.setFocusPolicy(QtCore.Qt.NoFocus)
        layout.addWidget(self.btn_move_abs, 1, 3)
        layout.addWidget(self.btn_stop, 1, 4)
        layout.addWidget(QtWidgets.QLabel('Pas'), 2, 0)
        self.step_spinbox = spin(1.0, vmin=1e-9)
        self.step_spinbox.setToolTip('Pas de a/d (X) ou w/s (Y) et du pavé ; Maj = ×10')
        layout.addWidget(self.step_spinbox, 2, 1)
        layout.addWidget(self.unit_labels[2], 2, 2)
        self.btn_move_abs.clicked.connect(lambda: self.move_requested.emit(self.target_spinbox.value()))
        self.btn_stop.clicked.connect(self.stop_requested)

    def update_position(self, raw_position: float):
        self._current_raw = raw_position
        self._has_position = True
        self.current_pos_label.setText(f'{raw_position:.3f}')

    def update_unit_display(self, unit_str: str):
        for lbl in self.unit_labels:
            lbl.setText(unit_str or 'u.a.')

    def set_status(self, text: str):
        self.status_label.setText(text)

    @property
    def step_val(self) -> float:
        return self.step_spinbox.value()

    @property
    def current_raw(self) -> float:
        return self._current_raw

    def set_busy(self, busy: bool):
        if busy:
            self.current_pos_label.setStyleSheet('background-color: #e67e22; color: white; padding: 0px 4px; '
                                                 'border-radius: 3px;')
            self.status_label.setText('En mouvement…')
        else:
            self.current_pos_label.setStyleSheet('padding: 0px 4px;')
            self.status_label.setText('Repos')


class JogPad(QtWidgets.QWidget):
    left_requested = QtCore.Signal()
    right_requested = QtCore.Signal()
    up_requested = QtCore.Signal()
    down_requested = QtCore.Signal()
    home_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        s_mv = 'font-size: 14px; font-weight: bold;'
        self.btn_up, self.btn_down = QtWidgets.QPushButton('▲'), QtWidgets.QPushButton('▼')
        self.btn_left, self.btn_right = QtWidgets.QPushButton('◀'), QtWidgets.QPushButton('▶')
        self.btn_home = QtWidgets.QPushButton('⌂')
        for btn, tip in ((self.btn_up, 'Y + un pas (w)'), (self.btn_down, 'Y − un pas (s)'),
                         (self.btn_left, 'X − un pas (a)'), (self.btn_right, 'X + un pas (d)'),
                         (self.btn_home, 'Aller en (0, 0)')):
            btn.setToolTip(tip)
            btn.setFixedSize(34, 30)
            btn.setFocusPolicy(QtCore.Qt.NoFocus)
            btn.setStyleSheet(s_mv + ('background-color: #34495e; color: white;' if btn is self.btn_home else ''))
        grid.addWidget(self.btn_up, 0, 1)
        grid.addWidget(self.btn_left, 1, 0)
        grid.addWidget(self.btn_home, 1, 1)
        grid.addWidget(self.btn_right, 1, 2)
        grid.addWidget(self.btn_down, 2, 1)
        self.btn_left.clicked.connect(self.left_requested)
        self.btn_right.clicked.connect(self.right_requested)
        self.btn_up.clicked.connect(self.up_requested)
        self.btn_down.clicked.connect(self.down_requested)
        self.btn_home.clicked.connect(self.home_requested)


class SavedPositionsWidget(QtWidgets.QWidget):
    go_requested = QtCore.Signal(float, float)
    positions_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_x = 0.0
        self._current_y = 0.0
        self._positions: dict = {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Nom', 'X', 'Y', ''])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setFocusPolicy(QtCore.Qt.ClickFocus)
        layout.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText('Nom de la position…')
        self.name_edit.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.btn_save = QtWidgets.QPushButton('💾 Mémoriser')
        self.btn_save.setStyleSheet('font-weight: bold;')
        self.btn_save.setFocusPolicy(QtCore.Qt.NoFocus)
        row.addWidget(self.name_edit)
        row.addWidget(self.btn_save)
        layout.addLayout(row)
        self.btn_save.clicked.connect(self._on_save)

    def set_current_position(self, x: float, y: float):
        self._current_x, self._current_y = x, y

    def _on_save(self):
        name = self.name_edit.text().strip()
        if not name:
            warn(self, 'Nom requis', 'Donner un nom à la position.')
            return
        self._positions[name] = {'x': self._current_x, 'y': self._current_y}
        self.name_edit.clear()
        self.refresh_table()
        self.positions_changed.emit()

    def refresh_table(self):
        self.table.setRowCount(0)
        for name, pos in self._positions.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{pos['x']:.3f}"))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{pos['y']:.3f}"))
            cell = QtWidgets.QWidget()
            cl = QtWidgets.QHBoxLayout(cell)
            cl.setContentsMargins(2, 1, 2, 1)
            cl.setSpacing(3)
            b_go = QtWidgets.QPushButton('Go')
            b_go.setStyleSheet('background-color: #27ae60; color: white; font-weight: bold; padding: 2px 6px;')
            b_del = QtWidgets.QPushButton('✕')
            b_del.setStyleSheet('background-color: #c0392b; color: white; font-weight: bold; padding: 2px 6px;')
            for b in (b_go, b_del):
                b.setFocusPolicy(QtCore.Qt.NoFocus)
            b_go.clicked.connect(lambda _, n=name: self.go_requested.emit(self._positions[n]['x'],
                                                                         self._positions[n]['y']))
            b_del.clicked.connect(lambda _, n=name: self._on_delete(n))
            cl.addWidget(b_go)
            cl.addWidget(b_del)
            self.table.setCellWidget(row, 3, cell)
        self.table.resizeColumnToContents(3)

    def _on_delete(self, name: str):
        if ask_yes_no(self, 'Supprimer la position', f'Supprimer « {name} » ?'):
            self._positions.pop(name, None)
            self.refresh_table()
            self.positions_changed.emit()


class StageMapWidget(QtWidgets.QWidget):
    limits_changed = QtCore.Signal()
    move_to_requested = QtCore.Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history_x: deque = deque(maxlen=HISTORY_MAX_LEN)
        self._history_y: deque = deque(maxlen=HISTORY_MAX_LEN)
        self._past_scans_items = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        def make_spin(val):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(-1e9, 1e9)
            sb.setDecimals(3)
            sb.setValue(val)
            sb.setFocusPolicy(QtCore.Qt.ClickFocus)
            sb.setMinimumWidth(66)
            sb.valueChanged.connect(lambda: self.limits_changed.emit())
            return sb

        self.x_min_sb, self.x_max_sb = make_spin(-50.0), make_spin(50.0)
        self.y_min_sb, self.y_max_sb = make_spin(-50.0), make_spin(50.0)
        lim = QtWidgets.QHBoxLayout()
        lim.setSpacing(3)
        lbl = QtWidgets.QLabel('Limites  X')
        lbl.setToolTip('Limites de la platine (unités natives) : les Go en dehors sont refusés')
        lim.addWidget(lbl)
        lim.addWidget(self.x_min_sb, 1)
        lim.addWidget(QtWidgets.QLabel('…'))
        lim.addWidget(self.x_max_sb, 1)
        lim.addSpacing(6)
        lim.addWidget(QtWidgets.QLabel('Y'))
        lim.addWidget(self.y_min_sb, 1)
        lim.addWidget(QtWidgets.QLabel('…'))
        lim.addWidget(self.y_max_sb, 1)
        b = QtWidgets.QPushButton('Appliquer vue')
        b.setFocusPolicy(QtCore.Qt.NoFocus)
        b.clicked.connect(self.update_view_bounds)
        lim.addWidget(b)
        layout.addLayout(lim)
        trail = QtWidgets.QHBoxLayout()
        self.chk_trail = QtWidgets.QCheckBox('Trace')
        self.chk_trail.setChecked(True)
        self.btn_clear_trail = QtWidgets.QPushButton('Effacer trace')
        self.btn_clear_history = QtWidgets.QPushButton('🗑️ Effacer scans')
        self.chk_click_move = QtWidgets.QCheckBox('Clic carte → déplacement')
        self.chk_click_move.setStyleSheet('color: #e67e22; font-weight: bold;')
        for w in (self.chk_trail, self.btn_clear_trail, self.btn_clear_history, self.chk_click_move):
            w.setFocusPolicy(QtCore.Qt.NoFocus)
        trail.addWidget(self.chk_trail)
        trail.addWidget(self.btn_clear_trail)
        trail.addWidget(self.btn_clear_history)
        trail.addStretch()
        trail.addWidget(self.chk_click_move)
        layout.addLayout(trail)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setFocusPolicy(QtCore.Qt.NoFocus)
        self.plot_widget.setMinimumHeight(150)
        self.plot_widget.setAspectLocked(True)
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setBackground('#1e1e2e')
        self._rect = QtWidgets.QGraphicsRectItem(-50.0, -50.0, 100.0, 100.0)
        self._rect.setPen(pg.mkPen('#74c7ec', width=2, style=QtCore.Qt.DashLine))
        self.plot_widget.addItem(self._rect)
        self._trail_curve = pg.PlotDataItem(pen=pg.mkPen('#89b4fa', width=1, style=QtCore.Qt.DotLine))
        self.plot_widget.addItem(self._trail_curve)
        self._saved_scatter = pg.ScatterPlotItem(size=10, pen=pg.mkPen('#a6e3a1', width=1),
                                                 brush=pg.mkBrush('#a6e3a150'), symbol='d')
        self.plot_widget.addItem(self._saved_scatter)
        self._pos_marker = pg.ScatterPlotItem(size=14, pen=pg.mkPen('#f38ba8', width=2),
                                              brush=pg.mkBrush('#f38ba8'), symbol='+')
        self.plot_widget.addItem(self._pos_marker)
        self._vline = pg.InfiniteLine(angle=90, pen=pg.mkPen('#f38ba880', width=1))
        self._hline = pg.InfiniteLine(angle=0, pen=pg.mkPen('#f38ba880', width=1))
        self.plot_widget.addItem(self._vline)
        self.plot_widget.addItem(self._hline)
        self._pos_text = pg.TextItem('', color='#cdd6f4', anchor=(0, 1))
        self.plot_widget.addItem(self._pos_text)
        self._target_marker = pg.ScatterPlotItem(size=12, pen=pg.mkPen('#fab387', width=2),
                                                 brush=pg.mkBrush('#fab38780'), symbol='o')
        self.plot_widget.addItem(self._target_marker)
        layout.addWidget(self.plot_widget)
        self.update_view_bounds()
        self.plot_widget.scene().sigMouseClicked.connect(self._on_map_clicked)
        self.chk_trail.toggled.connect(self._update_trail_visibility)
        self.btn_clear_trail.clicked.connect(self._clear_trail)
        self.btn_clear_history.clicked.connect(self.clear_scans_history)

    def set_labels_units(self, unit: str):
        self.plot_widget.setLabel('bottom', 'X', units=unit)
        self.plot_widget.setLabel('left', 'Y', units=unit)

    def add_scan_zone_to_history(self, xmin, xmax, dx, ymin, ymax, dy, completed=True):
        color = '#2ecc71' if completed else '#e67e22'
        rect = QtWidgets.QGraphicsRectItem(xmin, ymin, xmax - xmin, ymax - ymin)
        rect.setPen(pg.mkPen(color, width=1.5))
        rect.setBrush(pg.mkBrush(color + '15'))
        text = pg.TextItem(f'dx:{dx:.3g}\ndy:{dy:.3g}', color='#a6adc8', anchor=(0.5, 0.5))
        text.setPos((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)
        self.plot_widget.addItem(rect)
        self.plot_widget.addItem(text)
        self._past_scans_items += [rect, text]

    def clear_scans_history(self):
        for item in self._past_scans_items:
            self.plot_widget.removeItem(item)
        self._past_scans_items.clear()

    def update_view_bounds(self):
        xmin, xmax = self.x_min_sb.value(), self.x_max_sb.value()
        ymin, ymax = self.y_min_sb.value(), self.y_max_sb.value()
        self._rect.setRect(xmin, ymin, xmax - xmin, ymax - ymin)
        self.plot_widget.setXRange(xmin - 0.05 * (xmax - xmin), xmax + 0.05 * (xmax - xmin))
        self.plot_widget.setYRange(ymin - 0.05 * (ymax - ymin), ymax + 0.05 * (ymax - ymin))

    def update_position(self, x: float, y: float):
        self._pos_marker.setData([x], [y])
        self._vline.setValue(x)
        self._hline.setValue(y)
        self._pos_text.setText(f'  ({x:.2f}, {y:.2f})')
        self._pos_text.setPos(x, y)
        self._history_x.append(x)
        self._history_y.append(y)
        if self.chk_trail.isChecked():
            self._trail_curve.setData(list(self._history_x), list(self._history_y))

    def update_saved_positions(self, positions: dict):
        if not positions:
            self._saved_scatter.setData([], [])
            return
        xs = [p['x'] for p in positions.values()]
        ys = [p['y'] for p in positions.values()]
        tips = [f"{n}\n({p['x']:.3g}, {p['y']:.3g})" for n, p in positions.items()]
        try:
            self._saved_scatter.setData(xs, ys, tip=tips)
        except TypeError:
            self._saved_scatter.setData(xs, ys)

    def _on_map_clicked(self, event):
        if not self.chk_click_move.isChecked() or event.button() != QtCore.Qt.LeftButton:
            return
        p = self.plot_widget.getPlotItem().vb.mapSceneToView(event.scenePos())
        self._target_marker.setData([p.x()], [p.y()])
        self.move_to_requested.emit(p.x(), p.y())

    def _update_trail_visibility(self, visible: bool):
        self._trail_curve.setVisible(visible)
        if visible:
            self._trail_curve.setData(list(self._history_x), list(self._history_y))

    def _clear_trail(self):
        self._history_x.clear()
        self._history_y.clear()
        self._trail_curve.setData([], [])

    @property
    def x_limits(self):
        return self.x_min_sb.value(), self.x_max_sb.value()

    @property
    def y_limits(self):
        return self.y_min_sb.value(), self.y_max_sb.value()


class XYGroupWidget(QtWidgets.QWidget):
    """One XY pair. The widget owns the keyboard while it has the focus."""
    config_changed = QtCore.Signal()
    focus_changed = QtCore.Signal(bool)

    def __init__(self, app: 'LaserControlApp', pair_id: str):
        super().__init__()
        self.app = app
        self.manual = app.manual
        self.pair_id = pair_id
        self._x = 'None'
        self._y = 'None'
        self._held: dict = {}
        self._stepping: dict = {}
        self._busy: set = set()
        self._fast = False
        self._scan_bounds: dict = {}
        self._scan_zone_drawn = True
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._setup_ui()
        for sb in self.findChildren(QtWidgets.QAbstractSpinBox):
            le = sb.findChild(QtWidgets.QLineEdit)
            if le is not None:
                le.returnPressed.connect(self.activate_keyboard)
        for b in self.findChildren(QtWidgets.QAbstractButton):
            b.setFocusPolicy(QtCore.Qt.NoFocus)
        self._connect_ui()
        self.manual.add_listener(self._on_value)
        self.set_hardware()

    # ------------------------------------------------------------------ UI
    def _setup_ui(self):
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(3, 3, 3, 3)
        self.left_tabs = QtWidgets.QTabWidget()
        op = QtWidgets.QWidget()
        opl = QtWidgets.QVBoxLayout(op)
        opl.setContentsMargins(0, 0, 0, 0)
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        top = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.widget_x = ActuatorWidget('Axe X  (a ◀ ▶ d)')
        self.widget_y = ActuatorWidget('Axe Y  (s ▼ ▲ w)')
        tl.addWidget(self.widget_x, 1)
        tl.addWidget(self.widget_y, 1)
        self.jog_pad = JogPad()
        tl.addWidget(self.jog_pad, 0, QtCore.Qt.AlignVCenter)
        split.addWidget(top)
        box = QtWidgets.QGroupBox('📍 Positions mémorisées')
        bl = QtWidgets.QVBoxLayout(box)
        bl.setContentsMargins(2, 2, 2, 2)
        self.saved = SavedPositionsWidget()
        bl.addWidget(self.saved)
        split.addWidget(box)
        split.setSizes([120, 400])
        opl.addWidget(split)
        self.left_tabs.addTab(op, '🕹️ Pilotage')
        self.kb = QtWidgets.QPushButton()
        self.kb.clicked.connect(self.activate_keyboard)
        self.left_tabs.setCornerWidget(self.kb, QtCore.Qt.TopRightCorner)
        cfg = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(cfg)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Nom du groupe :'))
        self.name_edit = QtWidgets.QLineEdit('Nouveau groupe')
        self.name_edit.setFocusPolicy(QtCore.Qt.ClickFocus)
        row.addWidget(self.name_edit)
        cl.addLayout(row)
        sel = QtWidgets.QGroupBox('Actionneurs (rôles)')
        sf = QtWidgets.QFormLayout(sel)
        self.combo_x = QtWidgets.QComboBox()
        self.combo_y = QtWidgets.QComboBox()
        for c in (self.combo_x, self.combo_y):
            c.setFocusPolicy(QtCore.Qt.ClickFocus)
        sf.addRow('X :', self.combo_x)
        sf.addRow('Y :', self.combo_y)
        self.btn_apply = QtWidgets.QPushButton('Appliquer')
        self.btn_apply.setStyleSheet('font-weight: bold; min-height: 26px;')
        sf.addRow(self.btn_apply)
        cl.addWidget(sel)
        scan = QtWidgets.QGroupBox('🐍 Scan serpentin (automatisme du cœur)')
        sg = QtWidgets.QGridLayout(scan)

        def sspin(val, step=False):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(1e-9 if step else -1e9, 1e9)
            sb.setDecimals(3)
            sb.setValue(val)
            sb.setFocusPolicy(QtCore.Qt.ClickFocus)
            return sb

        self.sx_min, self.sx_max, self.sx_step = sspin(-10.0), sspin(10.0), sspin(1.0, True)
        self.sy_min, self.sy_max, self.sy_step = sspin(-10.0), sspin(10.0), sspin(1.0, True)
        self.s_dwell = sspin(0.0)
        self.s_dwell.setMinimum(0.0)
        for r, (lab, a, b, c) in enumerate((('X', self.sx_min, self.sx_max, self.sx_step),
                                            ('Y', self.sy_min, self.sy_max, self.sy_step))):
            sg.addWidget(QtWidgets.QLabel(f'{lab} min :'), r, 0)
            sg.addWidget(a, r, 1)
            sg.addWidget(QtWidgets.QLabel(f'{lab} max :'), r, 2)
            sg.addWidget(b, r, 3)
            sg.addWidget(QtWidgets.QLabel(f'{lab} pas :'), r, 4)
            sg.addWidget(c, r, 5)
        sg.addWidget(QtWidgets.QLabel('Attente / point (s) :'), 2, 0)
        sg.addWidget(self.s_dwell, 2, 1)
        self.btn_scan = QtWidgets.QPushButton('🚀 DÉMARRER LE SCAN')
        self.btn_scan.setStyleSheet('background-color: #2ecc71; color: white; font-weight: bold; min-height: 26px;')
        sg.addWidget(self.btn_scan, 3, 0, 1, 6)
        self.scan_info = QtWidgets.QLabel('')
        sg.addWidget(self.scan_info, 4, 0, 1, 6)
        cl.addWidget(scan)
        cl.addStretch()
        self.left_tabs.addTab(cfg, '⚙️ Configuration')
        main.addWidget(self.left_tabs, 1)
        self.map = StageMapWidget()
        main.addWidget(self.map, 1)
        self._update_kb(False)

    def _connect_ui(self):
        self.widget_x.move_requested.connect(self._move_x)
        self.widget_y.move_requested.connect(self._move_y)
        self.widget_x.stop_requested.connect(lambda: self._stop('x'))
        self.widget_y.stop_requested.connect(lambda: self._stop('y'))
        self.jog_pad.left_requested.connect(lambda: self._step('x', -self.widget_x.step_val))
        self.jog_pad.right_requested.connect(lambda: self._step('x', +self.widget_x.step_val))
        self.jog_pad.up_requested.connect(lambda: self._step('y', +self.widget_y.step_val))
        self.jog_pad.down_requested.connect(lambda: self._step('y', -self.widget_y.step_val))
        self.jog_pad.home_requested.connect(lambda: self.go_to(0.0, 0.0))
        self.saved.go_requested.connect(self.go_to)
        self.saved.positions_changed.connect(self._refresh_map_saved)
        self.saved.positions_changed.connect(self.config_changed)
        self.btn_apply.clicked.connect(self._apply_assignment)
        self.map.limits_changed.connect(self.config_changed)
        self.map.move_to_requested.connect(self.go_to)
        self.btn_scan.clicked.connect(self._on_scan)
        self.name_edit.textChanged.connect(lambda *_: self.config_changed.emit())

    def _update_kb(self, active: bool):
        if active:
            self.kb.setText('⌨ Clavier : ACTIF')
            self.kb.setStyleSheet('background-color: #27ae60; color: white; font-weight: bold; padding: 2px 8px;')
        else:
            self.kb.setText('⌨ inactif — cliquer')
            self.kb.setStyleSheet('background-color: #7f8c8d; color: white; font-weight: bold; padding: 2px 8px;')

    # ------------------------------------------------------------------ hardware
    def set_hardware(self):
        roles = ['None'] + self.manual.actuator_roles()
        set_combo_items(self.combo_x, roles, self._x)
        set_combo_items(self.combo_y, roles, self._y)
        self._apply_assignment(emit=False)

    def _apply_assignment(self, emit: bool = True):
        self._x = self.combo_x.currentText() or 'None'
        self._y = self.combo_y.currentText() or 'None'
        okx = self._x != 'None' and self.manual.exists(self._x)
        oky = self._y != 'None' and self.manual.exists(self._y)
        self.widget_x.setEnabled(okx)
        self.widget_y.setEnabled(oky)
        self.jog_pad.setEnabled(okx and oky)
        for role, w in ((self._x, self.widget_x), (self._y, self.widget_y)):
            if role != 'None' and self.manual.exists(role):
                w.update_unit_display(self.manual.units(role))
                pos = self.manual.position(role)
                if pos is not None:
                    w.update_position(pos)
        if okx:
            self.map.set_labels_units(self.manual.units(self._x))
        self.map.update_position(self.widget_x.current_raw, self.widget_y.current_raw)
        self._refresh_map_saved()
        if emit:
            self.config_changed.emit()

    def _role(self, axis: str) -> str:
        return self._x if axis == 'x' else self._y

    def _widget(self, axis: str) -> ActuatorWidget:
        return self.widget_x if axis == 'x' else self.widget_y

    # ------------------------------------------------------------------ positions
    def _on_value(self, role: str, value: float, done: bool):
        for axis in ('x', 'y'):
            if role != self._role(axis) or value is None:
                continue
            w = self._widget(axis)
            w.update_position(value)
            self.map.update_position(self.widget_x.current_raw, self.widget_y.current_raw)
            self.saved.set_current_position(self.widget_x.current_raw, self.widget_y.current_raw)
            if done:
                w.set_busy(False)
                self._busy.discard(axis)
                if axis in self._stepping and not self._scan_running():
                    self._do_step(axis)

    # ------------------------------------------------------------------ moves
    def _move(self, axis: str, position: float):
        lo, hi = self.map.x_limits if axis == 'x' else self.map.y_limits
        w = self._widget(axis)
        if not (lo <= position <= hi):
            w.set_status('⚠ Limites')
            self._stepping.pop(axis, None)
            return
        role = self._role(axis)
        if role == 'None' or not self.manual.exists(role):
            return
        if self.manual.move_abs(role, position, f'XY {self.name_edit.text()}'):
            self._busy.add(axis)
            w.set_busy(True)
        else:
            self._stepping.pop(axis, None)

    def _move_x(self, p: float):
        self._move('x', p)

    def _move_y(self, p: float):
        self._move('y', p)

    def _step(self, axis: str, delta: float):
        w = self._widget(axis)
        new = w.current_raw + delta
        w.target_spinbox.setValue(new)
        self._move(axis, new)

    def _stop(self, axis: str):
        role = self._role(axis)
        if role != 'None' and self.manual.exists(role):
            self.manual.stop(role, 'stop XY')
        self._stepping.pop(axis, None)
        self._busy.discard(axis)
        self._widget(axis).set_busy(False)

    def stop_all(self):
        self.release_all_jogs()
        self._stop('x')
        self._stop('y')

    def go_to(self, x: float, y: float):
        self._move('x', x)
        self._move('y', y)

    def _refresh_map_saved(self):
        self.map.update_saved_positions(self.saved._positions)

    # ------------------------------------------------------------------ scan
    @property
    def scan_id(self) -> str:
        return f'XYSCAN:{self.pair_id}'

    def _scan_running(self) -> bool:
        a = self.app.core.get(self.scan_id)
        return a is not None and a.state in ENGAGED_STATES

    def _on_scan(self):
        if self._scan_running():
            self.app.core.stop(self.scan_id, 'arrêt manuel')
            return
        b = dict(xmin=self.sx_min.value(), xmax=self.sx_max.value(), dx=self.sx_step.value(),
                 ymin=self.sy_min.value(), ymax=self.sy_max.value(), dy=self.sy_step.value())
        if not snake_pattern(**b):
            warn(self, 'Scan', 'Paramètres de scan invalides (min < max, pas > 0).')
            return
        xl, yl = self.map.x_limits, self.map.y_limits
        if b['xmin'] < xl[0] or b['xmax'] > xl[1] or b['ymin'] < yl[0] or b['ymax'] > yl[1]:
            warn(self, 'Scan', 'La zone de scan sort des limites de la carte.')
            return
        a = XYScanAutomation(self.pair_id, self._x, self._y, dwell=self.s_dwell.value(),
                             label=f'Scan XY {self.name_edit.text()}', **b)
        self.app.core.remove_automation(self.scan_id)
        self.app.core.add_automation(a)
        ok, msg = self.app.core.start(a.id)
        if not ok:
            warn(self, 'Scan', 'Démarrage impossible :\n\n' + msg.replace(' ; ', '\n'))
            return
        self._scan_bounds = b
        self._scan_zone_drawn = False

    def refresh(self):
        a = self.app.core.get(self.scan_id)
        running = a is not None and a.state in ENGAGED_STATES
        if running:
            done, total = a.progress()
            self.btn_scan.setText('🛑 ARRÊTER LE SCAN')
            self.btn_scan.setStyleSheet('background-color: #c0392b; color: white; font-weight: bold; min-height: 26px;')
            self.scan_info.setText(f'point {done + 1}/{total} · {a.state.value} {a.reason}')
        else:
            self.btn_scan.setText('🚀 DÉMARRER LE SCAN')
            self.btn_scan.setStyleSheet('background-color: #2ecc71; color: white; font-weight: bold; min-height: 26px;')
            if a is not None and not self._scan_zone_drawn and self._scan_bounds:
                b = self._scan_bounds
                self.map.add_scan_zone_to_history(b['xmin'], b['xmax'], b['dx'], b['ymin'], b['ymax'], b['dy'],
                                                  completed=getattr(a, 'completed', False))
                self._scan_zone_drawn = True
                self.scan_info.setText('scan terminé' if a.completed else f'scan interrompu : {a.reason}')
        for axis in ('x', 'y'):
            if axis in self._busy and not self.manual.is_busy(self._role(axis)) and axis not in self._stepping:
                self._busy.discard(axis)
                self._widget(axis).set_busy(False)

    # ------------------------------------------------------------------ keyboard
    def keyPressEvent(self, e: QtGui.QKeyEvent):
        if not (self.hasFocus() and self._handle_key_press(e)):
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e: QtGui.QKeyEvent):
        if not self._handle_key_release(e):
            super().keyReleaseEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self._update_kb(True)
        self.focus_changed.emit(True)

    def focusOutEvent(self, e):
        self.release_all_jogs()
        self._update_kb(False)
        self.focus_changed.emit(False)
        super().focusOutEvent(e)

    def hideEvent(self, e):
        self.release_all_jogs()
        super().hideEvent(e)

    def activate_keyboard(self):
        if self.isVisible():
            self.setFocus(QtCore.Qt.OtherFocusReason)

    def _handle_key_press(self, e: QtGui.QKeyEvent) -> bool:
        key = e.key()
        if key == QtCore.Qt.Key_Shift:
            if not e.isAutoRepeat():
                self._fast = True
            return True
        if e.isAutoRepeat():
            return key in XY_JOG_KEYS or key == QtCore.Qt.Key_Space
        if e.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
            return False
        if key == QtCore.Qt.Key_Space:
            self.app.shake('clavier XY')
            return True
        if key in XY_JOG_KEYS:
            self._fast = bool(e.modifiers() & QtCore.Qt.ShiftModifier)
            axis, direction = XY_JOG_KEYS[key]
            self._held[key] = (axis, direction)
            first = axis not in self._stepping
            self._stepping[axis] = direction
            if first and axis not in self._busy:
                self._do_step(axis)
            return True
        return False

    def _handle_key_release(self, e: QtGui.QKeyEvent) -> bool:
        key = e.key()
        if key == QtCore.Qt.Key_Shift:
            if not e.isAutoRepeat():
                self._fast = False
            return True
        if key not in XY_JOG_KEYS:
            return False
        if e.isAutoRepeat():
            return True
        held = self._held.pop(key, None)
        if held is None:
            return True
        axis = held[0]
        for other_axis, other_dir in self._held.values():
            if other_axis == axis:
                self._stepping[axis] = other_dir
                return True
        self._stepping.pop(axis, None)
        return True

    def _do_step(self, axis: str):
        direction = self._stepping.get(axis)
        if direction is None:
            return
        factor = 10.0 if self._fast else 1.0
        w = self._widget(axis)
        self._step(axis, direction * factor * w.step_val)

    def release_all_jogs(self):
        self._held.clear()
        self._stepping.clear()
        self._fast = False

    # ------------------------------------------------------------------ config
    def get_config(self) -> dict:
        return {'pair_id': self.pair_id, 'tab_title': self.name_edit.text(),
                'actuator_x': self.combo_x.currentText(), 'actuator_y': self.combo_y.currentText(),
                'step_x': self.widget_x.step_spinbox.value(), 'step_y': self.widget_y.step_spinbox.value(),
                'limits': {'xmin': self.map.x_min_sb.value(), 'xmax': self.map.x_max_sb.value(),
                           'ymin': self.map.y_min_sb.value(), 'ymax': self.map.y_max_sb.value()},
                'scan': {'xmin': self.sx_min.value(), 'xmax': self.sx_max.value(), 'dx': self.sx_step.value(),
                         'ymin': self.sy_min.value(), 'ymax': self.sy_max.value(), 'dy': self.sy_step.value(),
                         'dwell': self.s_dwell.value()},
                'saved_positions': copy.deepcopy(self.saved._positions)}

    def load_config(self, cfg: dict):
        if not cfg:
            return
        self.name_edit.setText(cfg.get('tab_title', 'Groupe'))
        self._x = cfg.get('actuator_x', 'None') or 'None'
        self._y = cfg.get('actuator_y', 'None') or 'None'
        roles = ['None'] + self.manual.actuator_roles()
        set_combo_items(self.combo_x, roles, self._x)
        set_combo_items(self.combo_y, roles, self._y)
        self.widget_x.step_spinbox.setValue(float(cfg.get('step_x', 1.0)))
        self.widget_y.step_spinbox.setValue(float(cfg.get('step_y', 1.0)))
        lims = cfg.get('limits', {})
        self.map.x_min_sb.setValue(lims.get('xmin', -50.0))
        self.map.x_max_sb.setValue(lims.get('xmax', 50.0))
        self.map.y_min_sb.setValue(lims.get('ymin', -50.0))
        self.map.y_max_sb.setValue(lims.get('ymax', 50.0))
        self.map.update_view_bounds()
        sc = cfg.get('scan', {})
        for w, k, d in ((self.sx_min, 'xmin', -10), (self.sx_max, 'xmax', 10), (self.sx_step, 'dx', 1),
                        (self.sy_min, 'ymin', -10), (self.sy_max, 'ymax', 10), (self.sy_step, 'dy', 1),
                        (self.s_dwell, 'dwell', 0)):
            w.setValue(float(sc.get(k, d)))
        self.saved._positions = copy.deepcopy(cfg.get('saved_positions', {}))
        self.saved.refresh_table()
        self._apply_assignment(emit=False)

    def shutdown(self):
        self.release_all_jogs()
        self.manual.remove_listener(self._on_value)


class XYTab(QtWidgets.QWidget):
    """Container of the XY groups (the former 'MainUIWidget' without the profile files: groups are
    stored in the preset)."""

    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.groups: List[XYGroupWidget] = []
        self._undo: Optional[Dict[str, dict]] = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton('＋ Groupe')
        self.btn_save_all = QtWidgets.QPushButton('📸 Mémoriser toutes les positions')
        self.btn_save_all.setStyleSheet('background-color: #8e44ad; color: white; font-weight: bold;')
        self.btn_go_all = QtWidgets.QPushButton('🚀 Tout rappeler…')
        self.btn_go_all.setStyleSheet('background-color: #27ae60; color: white; font-weight: bold;')
        self.btn_undo = QtWidgets.QPushButton('↩️ Annuler')
        self.btn_undo.setStyleSheet('background-color: #e67e22; color: white; font-weight: bold;')
        self.btn_undo.setEnabled(False)
        self.btn_stop = QtWidgets.QPushButton('■ STOP XY')
        self.btn_stop.setStyleSheet('background-color: #c0392b; color: white; font-weight: bold;')
        for b in (self.btn_add, self.btn_save_all, self.btn_go_all, self.btn_undo, self.btn_stop):
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            bar.addWidget(b)
        bar.addStretch()
        hint = QtWidgets.QLabel('w/s Y · a/d X · Maj ×10 · Espace shaker · T / Maj+T : groupe suivant')
        hint.setStyleSheet('color: gray; font-style: italic;')
        bar.addWidget(hint)
        lay.addLayout(bar)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda *_: QtCore.QTimer.singleShot(0, self._activate_current))
        self.tabs.installEventFilter(self)
        lay.addWidget(self.tabs)
        self.btn_add.clicked.connect(lambda: self.add_group())
        self.btn_save_all.clicked.connect(self._save_all)
        self.btn_go_all.clicked.connect(self._go_all)
        self.btn_undo.clicked.connect(self._undo_go)
        self.btn_stop.clicked.connect(self.stop_all)

    def add_group(self, cfg: Optional[dict] = None) -> XYGroupWidget:
        pair_id = (cfg or {}).get('pair_id') or f'g{QtCore.QDateTime.currentMSecsSinceEpoch()}'
        w = XYGroupWidget(self.app, pair_id)
        if cfg:
            w.load_config(cfg)
        self.groups.append(w)
        self.tabs.addTab(w, w.name_edit.text())
        w.name_edit.textChanged.connect(lambda text, g=w: self.tabs.setTabText(self.tabs.indexOf(g), text))
        w.config_changed.connect(self._mark_dirty)
        self._mark_dirty()
        return w

    def _mark_dirty(self):
        self.app.preset_dirty = True

    def _close_tab(self, index: int):
        w = self.tabs.widget(index)
        if not isinstance(w, XYGroupWidget):
            return
        if not ask_yes_no(self, 'Fermer le groupe', f'Supprimer le groupe « {self.tabs.tabText(index)} » du preset ?'):
            return
        self.app.core.remove_automation(w.scan_id)
        w.shutdown()
        self.groups.remove(w)
        self.tabs.removeTab(index)
        w.deleteLater()
        self._mark_dirty()

    def _activate_current(self):
        w = self.tabs.currentWidget()
        if isinstance(w, XYGroupWidget):
            w.activate_keyboard()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_T and not event.isAutoRepeat():
            n = self.tabs.count()
            if n > 1:
                step = -1 if event.modifiers() & QtCore.Qt.ShiftModifier else 1
                self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % n)
            return True
        return super().eventFilter(watched, event)

    def _save_all(self):
        if not self.groups:
            return
        default = f"Snapshot_{QtCore.QDateTime.currentDateTime().toString('yyyyMMdd_hhmmss')}"
        name, ok = QtWidgets.QInputDialog.getText(self, 'Mémoriser toutes les positions',
                                                  'Nom commun de cette photo des positions :', text=default)
        if not ok or not name.strip():
            return
        for i, g in enumerate(self.groups):
            title = g.name_edit.text().strip() or f'Groupe_{i + 1}'
            g.saved._positions[f'{name.strip()} ({title})'] = {'x': g.widget_x.current_raw, 'y': g.widget_y.current_raw}
            g.saved.refresh_table()
            g.saved.positions_changed.emit()
        info(self, 'Positions', f'Positions mémorisées dans les {len(self.groups)} groupes sous « {name.strip()} ».')

    def _go_all(self):
        prefixes = set()
        for g in self.groups:
            for full in g.saved._positions:
                prefixes.add(full.split(' (')[0] if ' (' in full else full)
        if not prefixes:
            warn(self, 'Tout rappeler', 'Aucune position mémorisée.')
            return
        item, ok = QtWidgets.QInputDialog.getItem(self, 'Tout rappeler', 'Photo de positions à rappeler :',
                                                  sorted(prefixes), editable=False)
        if not ok or not item:
            return
        if not ask_yes_no(self, 'Tout rappeler', f'Déplacer tous les groupes vers « {item} » ?\n'
                                                 '(les automatismes utilisant ces moteurs seront arrêtés)'):
            return
        self._undo = {g.pair_id: {'x': g.widget_x.current_raw, 'y': g.widget_y.current_raw} for g in self.groups}
        for i, g in enumerate(self.groups):
            title = g.name_edit.text().strip() or f'Groupe_{i + 1}'
            pos = g.saved._positions.get(f'{item} ({title})') or g.saved._positions.get(item)
            if pos:
                g.go_to(pos['x'], pos['y'])
        self.btn_undo.setEnabled(True)

    def _undo_go(self):
        if not self._undo:
            return
        for g in self.groups:
            prev = self._undo.get(g.pair_id)
            if prev:
                g.go_to(prev['x'], prev['y'])
        self._undo = None
        self.btn_undo.setEnabled(False)

    def stop_all(self):
        for g in self.groups:
            self.app.core.stop(g.scan_id, 'STOP XY')
            g.stop_all()

    # ------------------------------------------------------------------ preset / refresh
    def load_from_preset(self, preset: dict):
        for g in list(self.groups):
            g.shutdown()
            g.deleteLater()
        self.groups = []
        self.tabs.clear()
        for cfg in preset.get('xy_groups', []) or []:
            self.add_group(cfg)

    def write_to_preset(self, preset: dict):
        preset['xy_groups'] = [g.get_config() for g in self.groups]

    def set_hardware(self):
        for g in self.groups:
            g.set_hardware()

    def refresh(self):
        w = self.tabs.currentWidget()
        if isinstance(w, XYGroupWidget):
            w.refresh()

    def shutdown(self):
        for g in self.groups:
            g.shutdown()
