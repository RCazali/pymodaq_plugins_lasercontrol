"""Overview tab (cahier des charges T5.2, T5.3).

* one-click stabilisation: summary of what will move, Activate / Deactivate,
* one card per motor role: position, owner (Human / PID_x / Optimiser / Free), S4 envelope gauge,
* recent curves of the key quantities (laser power, camera centroids...).

"Someone arriving in front of the screen understands in 5 seconds what moves and why."
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from ..core.states import AutoState
from .widgets import (Badge, CheckList, EnvelopeGauge, StateBadge, fmt, fmt_pos, mono_font, owner_color,
                      section_label, styled_button)

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

PLOT_COLORS = ['#00a8ff', '#e67e22', '#2ecc71', '#e84393', '#f1c40f', '#9b59b6', '#1abc9c', '#e74c3c']


class MotorCard(QtWidgets.QFrame):
    def __init__(self, role: str, module: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.module = module
        self.setObjectName('motorCard')
        self.setStyleSheet('#motorCard { border: 1px solid rgba(127,127,127,110); border-radius: 6px; }')
        g = QtWidgets.QGridLayout(self)
        g.setContentsMargins(6, 3, 6, 3)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(1)
        name = QtWidgets.QLabel(f'<b>{role}</b>')
        name.setToolTip(f'module : {module}')
        g.addWidget(name, 0, 0)
        self.owner = Badge('Libre', owner_color('Libre'), font_pt=9)
        g.addWidget(self.owner, 0, 1, 1, 2, QtCore.Qt.AlignRight)
        self.pos = QtWidgets.QLabel('—')
        self.pos.setFont(mono_font(13))
        self.pos.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        g.addWidget(self.pos, 1, 0, 1, 2)
        self.units = QtWidgets.QLabel('')
        self.units.setStyleSheet('color: gray;')
        g.addWidget(self.units, 1, 2)
        self.gauge = EnvelopeGauge()
        g.addWidget(self.gauge, 2, 0, 1, 3)
        self.detail = QtWidgets.QLabel('')
        self.detail.setStyleSheet('color: gray; font-size: 9pt;')
        g.addWidget(self.detail, 3, 0, 1, 3)
        g.setColumnStretch(0, 1)

    def update_from(self, app: 'LaserControlApp'):
        core = app.core
        m = self.module
        pos = core.broker.position(m)
        st = core.broker.state(m)
        busy = bool(st and st.busy)
        self.pos.setText(fmt_pos(pos, 4))
        self.pos.setStyleSheet('background-color: #e67e22; color: white; border-radius: 3px; padding: 0 4px;'
                               if busy else 'padding: 0 4px;')
        self.units.setText(core.broker.units(m))
        label = core.owner_label(m)
        owner = core.ownership.owner_of(m)
        self.owner.set(label, owner_color(label), f'propriétaire : {label}')
        lim = core.safety.limits(self.role)
        a = core.automations.get(owner) if owner else None
        if a is not None:
            policy = a.policy()
            lo, hi = core.safety.allowed_range(a.id, self.role, m, policy)
            ref = core.safety.reference(a.id, m)
            if not (math.isfinite(lo) and math.isfinite(hi)):
                lo = hi = None
            self.gauge.set_values(lo, hi, pos, ref)
            exc = f'Δ engagement {pos - ref:+.4g}' if (ref is not None and pos is not None) else ''
            self.detail.setText(f'{a.label} · {exc}')
        elif lim is not None and lim.abs_min is not None and lim.abs_max is not None:
            self.gauge.set_values(lim.abs_min, lim.abs_max, pos, None)
            self.detail.setText(f'limites abs [{lim.abs_min:g}, {lim.abs_max:g}]')
        else:
            self.gauge.set_values(None, None, pos, None)
            self.detail.setText('' if lim else 'limites de sécurité non définies')


class OverviewTab(QtWidgets.QWidget):
    CARD_MIN_W = 230

    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self._cards: Dict[str, MotorCard] = {}
        self._cols = 0
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._plots: Dict[str, pg.PlotItem] = {}
        self._refresh_count = 0
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        # ---------------------------------------------------------- stabilisation
        stab = QtWidgets.QGroupBox('Stabilisation')
        sl = QtWidgets.QGridLayout(stab)
        self.preset_label = QtWidgets.QLabel('—')
        self.preset_label.setStyleSheet('font-size: 12pt; font-weight: bold;')
        sl.addWidget(self.preset_label, 0, 0)
        self.preset_badge = Badge('brouillon', '#e67e22')
        sl.addWidget(self.preset_badge, 0, 1)
        self.btn_on = styled_button('▶ Activer la stabilisation', '#27ae60',
                                    'Démarre les PID du preset marqués « stabilisation » (après confirmation)')
        self.btn_off = styled_button('⏹ Désactiver', '#7f8c8d', 'Arrête les PID de stabilisation (aucun mouvement)')
        self.btn_on.setMinimumHeight(34)
        self.btn_off.setMinimumHeight(34)
        self.btn_on.clicked.connect(self._on_activate)
        self.btn_off.clicked.connect(self.app.deactivate_stabilization)
        sl.addWidget(self.btn_on, 0, 3)
        sl.addWidget(self.btn_off, 0, 4)
        sl.setColumnStretch(2, 1)
        self.summary = QtWidgets.QLabel('')
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        sl.addWidget(self.summary, 1, 0, 1, 5)
        self.problems = QtWidgets.QLabel('')
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet('color: #c0392b;')
        sl.addWidget(self.problems, 2, 0, 1, 5)
        outer.addWidget(stab)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        outer.addWidget(split, 1)

        # ---------------------------------------------------------- motor cards
        cards_w = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(cards_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(section_label('Moteurs (position · propriétaire · enveloppe S4)'))
        self.cards_scroll = QtWidgets.QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.cards_host = QtWidgets.QWidget()
        self.cards_grid = QtWidgets.QGridLayout(self.cards_host)
        self.cards_grid.setSpacing(5)
        self.cards_grid.setContentsMargins(2, 2, 2, 2)
        self.cards_scroll.setWidget(self.cards_host)
        cl.addWidget(self.cards_scroll)
        split.addWidget(cards_w)

        # ---------------------------------------------------------- curves
        plots_w = QtWidgets.QWidget()
        pl = QtWidgets.QHBoxLayout(plots_w)
        pl.setContentsMargins(0, 0, 0, 0)
        left = QtWidgets.QVBoxLayout()
        left.addWidget(section_label('Courbes récentes'))
        self.signal_list = CheckList()
        self.signal_list.setMaximumWidth(220)
        self.signal_list.changed.connect(self._rebuild_plots)
        left.addWidget(self.signal_list)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Durée :'))
        self.span = QtWidgets.QComboBox()
        for label, s in (('1 min', 60), ('5 min', 300), ('10 min', 600), ('30 min', 1800)):
            self.span.addItem(label, s)
        self.span.setCurrentIndex(1)
        row.addWidget(self.span)
        left.addLayout(row)
        pl.addLayout(left)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setFocusPolicy(QtCore.Qt.NoFocus)
        pl.addWidget(self.glw, 1)
        split.addWidget(plots_w)
        split.setSizes([300, 400])

    # ------------------------------------------------------------------ build
    def rebuild(self):
        """Roles or hardware changed: rebuild cards and the signal list."""
        for c in self._cards.values():
            c.setParent(None)
            c.deleteLater()
        self._cards = {}
        for role in sorted(self.app.core.roles.actuators):
            module = self.app.core.roles.module_for(role)
            self._cards[role] = MotorCard(role, module)
        self._cols = 0
        self._reflow()
        sigs = sorted(self.app.core.roles.signals)
        wanted = self.app.preset.get('overview', {}).get('plot_roles') or []
        if not wanted:
            inten = self.app.core.safety.config.intensity.role
            wanted = ([inten] if inten else []) + [s for s in sigs if s != inten][:3]
        self.signal_list.set_items(sigs, wanted)
        self._rebuild_plots()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def _reflow(self):
        width = self.cards_scroll.viewport().width()
        cols = max(1, width // self.CARD_MIN_W) if width > 0 else 4
        if cols == self._cols:
            return
        self._cols = cols
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        for i, card in enumerate(self._cards.values()):
            self.cards_grid.addWidget(card, i // cols, i % cols, QtCore.Qt.AlignTop)
        for c in range(cols):
            self.cards_grid.setColumnStretch(c, 1)
        self.cards_grid.setRowStretch((len(self._cards) + cols - 1) // cols, 1)

    def _rebuild_plots(self):
        self.glw.clear()
        self._curves = {}
        self._plots = {}
        roles = self.signal_list.checked()[:8]
        first = None
        for i, role in enumerate(roles):
            p = self.glw.addPlot(row=i, col=0)
            p.showGrid(x=True, y=True, alpha=0.25)
            p.setLabel('left', role)
            p.getAxis('left').setWidth(60)
            if i < len(roles) - 1:
                p.getAxis('bottom').setStyle(showValues=False)
            else:
                p.setLabel('bottom', 'temps (s)')
            if first is None:
                first = p
            else:
                p.setXLink(first)
            self._plots[role] = p
            self._curves[role] = p.plot(pen=pg.mkPen(PLOT_COLORS[i % len(PLOT_COLORS)], width=1.5))

    def write_to_preset(self, preset: dict):
        preset.setdefault('overview', {})['plot_roles'] = self.signal_list.checked()

    def load_from_preset(self, preset: dict):
        self.rebuild()

    # ------------------------------------------------------------------ actions
    def _on_activate(self):
        self.app.activate_stabilization(self)

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        app = self.app
        p = app.preset
        dirty = ' *' if app.preset_dirty else ''
        self.preset_label.setText(f"Preset : {p.get('name', '—')}{dirty}  ·  {p.get('conditions', {}).get('regime', '')}")
        if p.get('status') == 'validated':
            self.preset_badge.set('✔ validé', '#27ae60', f"validé le {p.get('validated_on')} par {p.get('validated_by')}")
        else:
            self.preset_badge.set('brouillon', '#e67e22', 'preset non validé')
        pids = app.stabilization_pids()
        lines = []
        for cfg in pids:
            a = app.core.get(f"PID:{cfg['id']}")
            state = a.state.value if a is not None else 'OFF'
            mot = ', '.join(f"{r}×{w:+g}" for r, w in cfg.get('motors', {}).items() if w)
            lines.append(f"• <b>{cfg.get('label') or cfg['id']}</b> [{state}] : {cfg.get('signal')} → {mot}")
        self.summary.setText('<br>'.join(lines) if lines else
                             '<i>Aucun PID marqué « stabilisation » dans le preset.</i>')
        running = any(app.core.get(f"PID:{c['id']}") is not None and
                      app.core.get(f"PID:{c['id']}").state != AutoState.OFF for c in pids)
        self.btn_on.setEnabled(bool(pids) and not app.core.estop)
        self.btn_off.setEnabled(running)
        self.problems.setText(app.stabilization_problems_text())
        for card in self._cards.values():
            card.update_from(app)
        self._refresh_count += 1
        if self._refresh_count % 2 == 0:
            self._update_plots()

    def _update_plots(self):
        span = float(self.span.currentData())
        now = time.time()
        for role, curve in self._curves.items():
            ch = self.app.core.roles.channel_for(role)
            h = self.app.core.signals.history(ch)
            if h is None:
                curve.setData([], [])
                continue
            t, v = h.arrays(since=now - span)
            curve.setData(t - now, v, connect='finite')
