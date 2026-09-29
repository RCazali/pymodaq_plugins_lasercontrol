"""Small reusable widgets and helpers of the Laser Control UI (Qt through qtpy)."""
from __future__ import annotations

import math
from typing import Iterable, List, Optional

from qtpy import QtCore, QtGui, QtWidgets

from ..core.states import STATE_COLORS, STATE_LABELS, AutoState, GlobalState

OWNER_COLORS = {'Free': '#7f8c8d', 'Human': '#e67e22'}
PALETTE = ['#2980b9', '#27ae60', '#8e44ad', '#d35400', '#16a085', '#c0392b', '#2c3e50', '#f39c12']
DET_COLORS = {'ok': '#27ae60', 'warn': '#f39c12', 'stale': '#e74c3c', 'unknown': '#7f8c8d'}


def owner_color(label: str) -> str:
    if label in OWNER_COLORS:
        return OWNER_COLORS[label]
    return PALETTE[sum(ord(c) for c in label) % len(PALETTE)]


def mono_font(pt: int = 11, bold: bool = True) -> QtGui.QFont:
    f = QtGui.QFont('Consolas')
    f.setStyleHint(QtGui.QFont.Monospace)
    f.setPointSize(pt)
    f.setBold(bold)
    return f


def fmt(v, digits: int = 4) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '—'
    if not math.isfinite(v):
        return '—'
    return f'{v:.{digits}g}'


def fmt_pos(v, decimals: int = 3) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '—'
    return f'{v:.{decimals}f}' if math.isfinite(v) else '—'


def run_dialog(dlg) -> int:
    fn = getattr(dlg, 'exec', None) or getattr(dlg, 'exec_')
    return fn()


def ask_yes_no(parent, title: str, text: str, default_no: bool = True) -> bool:
    buttons = QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
    default = QtWidgets.QMessageBox.No if default_no else QtWidgets.QMessageBox.Yes
    return QtWidgets.QMessageBox.question(parent, title, text, buttons, default) == QtWidgets.QMessageBox.Yes


def info(parent, title: str, text: str):
    QtWidgets.QMessageBox.information(parent, title, text)


def warn(parent, title: str, text: str):
    QtWidgets.QMessageBox.warning(parent, title, text)


def no_focus(*widgets):
    for w in widgets:
        w.setFocusPolicy(QtCore.Qt.NoFocus)


def dspin(value: float = 0.0, lo: float = -1e9, hi: float = 1e9, decimals: int = 4, step: float = 0.1,
          width: Optional[int] = None, tip: str = '') -> QtWidgets.QDoubleSpinBox:
    sb = QtWidgets.QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(decimals)
    sb.setSingleStep(step)
    sb.setValue(value)
    sb.setKeyboardTracking(False)
    sb.setFocusPolicy(QtCore.Qt.ClickFocus)
    if width:
        sb.setMinimumWidth(width)
    if tip:
        sb.setToolTip(tip)
    return sb


def ispin(value: int = 0, lo: int = 0, hi: int = 1000000, tip: str = '') -> QtWidgets.QSpinBox:
    sb = QtWidgets.QSpinBox()
    sb.setRange(lo, hi)
    sb.setValue(value)
    sb.setKeyboardTracking(False)
    sb.setFocusPolicy(QtCore.Qt.ClickFocus)
    if tip:
        sb.setToolTip(tip)
    return sb


def styled_button(text: str, color: Optional[str] = None, tip: str = '', bold: bool = True,
                  checkable: bool = False) -> QtWidgets.QPushButton:
    b = QtWidgets.QPushButton(text)
    style = []
    if color:
        style.append(f'background-color: {color}; color: white;')
    if bold:
        style.append('font-weight: bold;')
    if style:
        b.setStyleSheet('QPushButton {' + ' '.join(style) + ' padding: 3px 8px; }'
                        'QPushButton:disabled { background-color: #95a5a6; color: #ecf0f1; }')
    if tip:
        b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setFocusPolicy(QtCore.Qt.NoFocus)
    return b


class Badge(QtWidgets.QLabel):
    """Colored rounded label (state, owner...)."""

    def __init__(self, text: str = '', color: str = '#7f8c8d', parent=None, font_pt: int = 10):
        super().__init__(text, parent)
        self._font_pt = font_pt
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.set(text, color)

    def set(self, text: str, color: str, tip: Optional[str] = None):
        self.setText(text)
        self.setStyleSheet(f'QLabel {{ background-color: {color}; color: white; border-radius: 4px; '
                           f'padding: 1px 6px; font-weight: bold; font-size: {self._font_pt}pt; }}')
        if tip is not None:
            self.setToolTip(tip)


class StateBadge(Badge):
    def set_state(self, state, reason: str = '', prefix: str = ''):
        label = STATE_LABELS.get(state, str(state))
        color = STATE_COLORS.get(state, '#7f8c8d')
        self.set(f'{prefix}{label}', color, reason or label)


class Led(QtWidgets.QLabel):
    """Round colored indicator with an optional text."""

    def __init__(self, text: str = '', parent=None):
        super().__init__(parent)
        self._text = text
        self.set_color('#7f8c8d')

    def set_color(self, color: str, text: Optional[str] = None, tip: Optional[str] = None):
        if text is not None:
            self._text = text
        self.setText(f'<span style="color:{color}; font-size:14pt;">●</span> {self._text}')
        if tip is not None:
            self.setToolTip(tip)


class EnvelopeGauge(QtWidgets.QWidget):
    """S4 envelope: [ref - exc, ref + exc] (and abs limits), current position marker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(14)
        self.setMaximumHeight(18)
        self.setMinimumWidth(90)
        self._lo = self._hi = self._pos = self._ref = None

    def set_values(self, lo: Optional[float], hi: Optional[float], pos: Optional[float], ref: Optional[float]):
        self._lo, self._hi, self._pos, self._ref = lo, hi, pos, ref
        if lo is not None and hi is not None and pos is not None and hi > lo:
            used = max(abs(pos - ref) / ((hi - lo) / 2), 0) if ref is not None else None
            self.setToolTip(f'envelope [{lo:.6g}, {hi:.6g}]  position {pos:.6g}' +
                            (f'  ({100 * used:.0f} % used)' if used is not None else ''))
        else:
            self.setToolTip('no active envelope (motor not driven automatically)')
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        r = self.rect().adjusted(1, 3, -1, -3)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        lo, hi, pos, ref = self._lo, self._hi, self._pos, self._ref
        if lo is None or hi is None or pos is None or not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            p.setPen(QtGui.QPen(QtGui.QColor('#95a5a6'), 1, QtCore.Qt.DashLine))
            p.drawRect(r)
            p.end()
            return
        frac = (pos - lo) / (hi - lo)
        used = abs(frac - 0.5) * 2
        color = '#27ae60' if used < 0.6 else ('#f39c12' if used < 0.9 else '#e74c3c')
        p.setPen(QtGui.QPen(QtGui.QColor('#7f8c8d'), 1))
        p.setBrush(QtGui.QColor(127, 127, 127, 40))
        p.drawRoundedRect(r, 3, 3)
        if ref is not None and math.isfinite(ref):
            xr = r.left() + (ref - lo) / (hi - lo) * r.width()
            p.setPen(QtGui.QPen(QtGui.QColor('#7f8c8d'), 1, QtCore.Qt.DotLine))
            p.drawLine(QtCore.QPointF(xr, r.top()), QtCore.QPointF(xr, r.bottom()))
        x = r.left() + min(max(frac, 0.0), 1.0) * r.width()
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(color))
        p.drawRect(QtCore.QRectF(x - 3, r.top() - 2, 6, r.height() + 4))
        p.end()


class ClickableFrame(QtWidgets.QFrame):
    clicked = QtCore.Signal()

    def mousePressEvent(self, e):
        self.clicked.emit()
        super().mousePressEvent(e)


class CheckList(QtWidgets.QListWidget):
    """List of checkable strings."""

    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.itemChanged.connect(lambda *_: self.changed.emit())

    def set_items(self, names: Iterable[str], checked: Iterable[str] = ()):
        checked = set(checked)
        self.blockSignals(True)
        self.clear()
        for n in names:
            it = QtWidgets.QListWidgetItem(n)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if n in checked else QtCore.Qt.Unchecked)
            self.addItem(it)
        self.blockSignals(False)

    def checked(self) -> List[str]:
        return [self.item(i).text() for i in range(self.count())
                if self.item(i).checkState() == QtCore.Qt.Checked]


def reset_rows(table: QtWidgets.QTableWidget, rows: int):
    """Resize a table whose cells hold widgets. Emptying it first deletes every cell widget:
    ``setCellWidget`` on an occupied cell does not delete the previous widget with Qt 5, which then
    stays visible, orphaned, over the table."""
    table.setRowCount(0)
    table.setRowCount(rows)


def fit_table_height(table: QtWidgets.QTableWidget, max_rows: int = 14, min_rows: int = 2):
    """Fixed table height showing all its rows (up to ``max_rows``), for tables inside a scroll area."""
    rows = min(max(table.rowCount(), min_rows), max_rows)
    row_h = table.verticalHeader().defaultSectionSize()
    if table.rowCount():
        row_h = max(row_h, max(table.rowHeight(r) for r in range(min(table.rowCount(), max_rows))))
    h = table.horizontalHeader().sizeHint().height() + rows * row_h + 2 * table.frameWidth() + 4
    if table.horizontalScrollBar().isVisible():
        h += table.horizontalScrollBar().sizeHint().height()
    table.setMinimumHeight(h)
    table.setMaximumHeight(h)


def set_combo_items(combo: QtWidgets.QComboBox, items: List[str], current: Optional[str] = None,
                    keep_unknown: bool = True):
    """Refill a combo without losing the current value (unknown values kept, marked with ⚠)."""
    cur = current if current is not None else combo.currentText()
    combo.blockSignals(True)
    combo.clear()
    items = list(items)
    if keep_unknown and cur and cur not in items and cur != 'None':
        items.append(cur)
    combo.addItems(items)
    if cur in items:
        combo.setCurrentText(cur)
    combo.blockSignals(False)


class HLine(QtWidgets.QFrame):
    def __init__(self):
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.HLine)
        self.setStyleSheet('color: rgba(127,127,127,90);')


def section_label(text: str) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(f'<b>{text}</b>')
    return lbl


def scroll_wrap(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    sa = QtWidgets.QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QtWidgets.QFrame.NoFrame)
    sa.setWidget(widget)
    return sa


class FlowLayout(QtWidgets.QLayout):
    """Left-to-right layout that wraps to the next line (Qt 'flow layout' example)."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: List[QtWidgets.QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > r.right() + 1 and line_h > 0:
                x = r.x()
                y = y + line_h + self._spacing
                next_x = x + hint.width() + self._spacing
                line_h = 0
            if not test_only:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()
