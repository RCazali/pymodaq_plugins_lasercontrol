"""Journal of the timestamped events (consumer of the event bus, T3.6)."""
from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, List

from qtpy import QtCore, QtGui, QtWidgets

from ..core.events import Event, Level

if TYPE_CHECKING:  # pragma: no cover
    from ..core.events import EventBus

LEVEL_COLORS = {Level.DEBUG: '#95a5a6', Level.INFO: None, Level.WARNING: '#e67e22', Level.ALARM: '#e74c3c',
                Level.ERROR: '#8e44ad'}
LEVEL_NAMES = {Level.DEBUG: 'debug', Level.INFO: 'info', Level.WARNING: 'attention', Level.ALARM: 'ALARME',
               Level.ERROR: 'ERREUR'}


class EventLogWidget(QtWidgets.QWidget):
    MAX_ROWS = 3000

    def __init__(self, events: 'EventBus', parent=None):
        super().__init__(parent)
        self.events = events
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel('Niveau min. :'))
        self.level = QtWidgets.QComboBox()
        for lvl in (Level.DEBUG, Level.INFO, Level.WARNING, Level.ALARM):
            self.level.addItem(LEVEL_NAMES[lvl], int(lvl))
        self.level.setCurrentIndex(1)
        self.level.currentIndexChanged.connect(self._reload)
        bar.addWidget(self.level)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText('filtrer (texte, source)…')
        self.filter.textChanged.connect(self._reload)
        bar.addWidget(self.filter, 1)
        self.autoscroll = QtWidgets.QCheckBox('défilement auto')
        self.autoscroll.setChecked(True)
        bar.addWidget(self.autoscroll)
        lay.addLayout(bar)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Heure', 'Niveau', 'Source', 'Message'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        self.table.setFocusPolicy(QtCore.Qt.ClickFocus)
        lay.addWidget(self.table)
        self._pending: List[Event] = []
        self._token = events.subscribe(self._on_event)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._flush)
        self._timer.start()
        self._reload()

    def close_bus(self):
        self.events.unsubscribe(self._token)
        self._timer.stop()

    def _accept(self, ev: Event) -> bool:
        if ev.level < self.level.currentData():
            return False
        f = self.filter.text().strip().lower()
        return not f or f in ev.message.lower() or f in ev.source.lower() or f in ev.kind.lower()

    def _on_event(self, ev: Event):
        self._pending.append(ev)

    def _flush(self):
        if not self._pending:
            return
        evs, self._pending = self._pending, []
        for ev in evs:
            if self._accept(ev):
                self._append(ev)
        if self.table.rowCount() > self.MAX_ROWS:
            for _ in range(self.table.rowCount() - self.MAX_ROWS):
                self.table.removeRow(0)
        if self.autoscroll.isChecked():
            self.table.scrollToBottom()

    def _append(self, ev: Event):
        r = self.table.rowCount()
        self.table.insertRow(r)
        t = _dt.datetime.fromtimestamp(ev.t).strftime('%H:%M:%S')
        items = [t, LEVEL_NAMES.get(ev.level, str(int(ev.level))), ev.source, ev.message]
        color = LEVEL_COLORS.get(ev.level)
        for c, text in enumerate(items):
            it = QtWidgets.QTableWidgetItem(text)
            it.setToolTip(ev.message if c == 3 else _dt.datetime.fromtimestamp(ev.t).isoformat(sep=' '))
            if color:
                it.setForeground(QtGui.QBrush(QtGui.QColor(color)))
            if ev.level >= Level.ALARM:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.table.setItem(r, c, it)

    def _reload(self):
        self.table.setRowCount(0)
        for ev in self.events.recent(self.MAX_ROWS):
            if self._accept(ev):
                self._append(ev)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.scrollToBottom()
