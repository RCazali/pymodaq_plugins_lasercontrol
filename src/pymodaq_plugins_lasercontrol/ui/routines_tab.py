"""Routines tab: the SETTINGS of the routines, as structured blocks (engines/program.py; docs/roadmap.md § 3).
They are run from the Alignment tab (band SEQ); ▶ Run here is for trying them.

* left: the routines of the preset;
* middle: the routine as a diagram ("D" layout) — the main sequence, then the blocks that run in the
  background and interrupt it. Click a block to edit it; the toolbar adds a step, a REPEAT, a FOR EACH, a
  ramp or an EVERY block (into the selected container, after the selected step, else at the end);
* right: the selected block in a PyMoDAQ parameter tree, the run controls and the run history.

Marks never rely on colour alone (goggles): ◆ selected (dashed frame), ▶ running (thick frame),
‖ interrupted (dashed frame). Editing is locked while the routine runs.
"""
from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from qtpy import QtCore, QtGui, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.events import EventKind, Level
from ..core.roles import sanitize_role_name
from ..core.states import ENGAGED_STATES, AutoState
from ..engines.program import (ANSWERS, ON_FAIL, OUTCOMES, STEP_ACTIONS, Cond, Every, Node, Position, Program,
                               block_of, describe_cond, describe_node, duplicate_block, find, inside_foreach,
                               insert_block, move_block, remove_block, validate_program)
from ..engines.routine import CONDITIONS, SIGNAL_OPS, START_OPTIONS
from .widgets import StateBadge, ask_yes_no, mono_font, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

# goggles: dark background, bright foregrounds, states by shape + text
BG = QtGui.QColor('#1d1f24')
BOX = QtGui.QColor('#2c2f38')
HEAD = QtGui.QColor('#22252d')
TEXT = QtGui.QColor('#f2f2f2')
DIM = QtGui.QColor('#a9afbb')
LOOP = QtGui.QColor('#66d9ff')
MARK = QtGui.QColor('#ffd75e')
SELECT = QtGui.QColor('#ffffff')
ICON = {'repeat': '⟲', 'foreach': '⟲', 'every': '⏱'}
TARGET_KIND = {'start': 'auto', 'stop': 'auto', 'pause': 'auto', 'resume': 'auto', 'confirm': 'opt', 'move': 'motor'}
COND_TARGET = {'signal': 'signal', 'travel': 'motor', 'finished': 'auto'}
COND_TIP = {'delay': 'seconds after the end of the action', 'locked': '', 'signal': 'threshold',
            'travel': 'distance from the position at the start of the step',
            'count': 'number of times this step was entered (loop guard)', 'finished': ''}
W_BOX = 560


def font(pt=10.0, bold=False) -> QtGui.QFont:
    f = QtGui.QFont()
    f.setPointSizeF(pt)
    f.setBold(bold)
    return f


def short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + '…'


def _limits(pairs: List[Tuple[str, str]], current: str) -> Dict[str, str]:
    """{text: value} for a list parameter; an empty value is shown as such, an unknown one kept (⚠)."""
    out = {'— choose —': ''}
    out.update({text: value for value, text in pairs})
    if current and current not in out.values():
        out[f'⚠ {current} (unknown)'] = current
    return out


# ====================================================================== diagram
class BlockItem(QtWidgets.QGraphicsPathItem):
    def __init__(self, rect: QtCore.QRectF, uid: int, fill=BOX):
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        super().__init__(path)
        self.uid = uid
        self.rect_ = rect
        self.setBrush(fill)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.marker = QtWidgets.QGraphicsSimpleTextItem('', self)
        self.marker.setFont(font(13, True))
        self.marker.setBrush(MARK)
        self.marker.setPos(rect.left() - 20, rect.top() + 2)
        self.set_state(None, False)

    def set_state(self, run: Optional[str], selected: bool):
        """run: None | 'run' | 'frozen'."""
        if run == 'run':
            self.setPen(QtGui.QPen(MARK, 4))
            self.marker.setText('▶')
        elif run == 'frozen':
            self.setPen(QtGui.QPen(MARK, 3, QtCore.Qt.DashLine))
            self.marker.setText('‖')
        elif selected:
            self.setPen(QtGui.QPen(SELECT, 3, QtCore.Qt.DashLine))
            self.marker.setText('◆')
        else:
            self.setPen(QtGui.QPen(QtGui.QColor('#5a6070'), 1.2))
            self.marker.setText('')
        self.marker.setBrush(SELECT if (selected and run is None) else MARK)


class BlockView(QtWidgets.QGraphicsView):
    block_clicked = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setBackgroundBrush(BG)
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.items_: Dict[int, BlockItem] = {}
        self.selected_uid: Optional[int] = None
        self._run: Tuple[Optional[int], Optional[int]] = (None, None)
        self.scene().selectionChanged.connect(self._on_selection)

    def _on_selection(self):
        for it in self.scene().selectedItems():
            if isinstance(it, BlockItem):
                self.block_clicked.emit(it.uid)
                return

    def text(self, x, y, s, pt=10.0, bold=False, color=TEXT, parent=None):
        t = QtWidgets.QGraphicsSimpleTextItem(s, parent)
        t.setFont(font(pt, bold))
        t.setBrush(color)
        t.setPos(x, y)
        if parent is None:
            self.scene().addItem(t)
        return t

    def draw(self, p: Optional[Program]):
        sel = self.selected_uid
        self.scene().blockSignals(True)
        self.scene().clear()
        self.scene().blockSignals(False)
        self.items_ = {}
        if p is None:
            self.text(20, 20, 'No routine: "➕ New"', 11, False, DIM)
            return
        y = 8
        self.text(10, y, 'MAIN SEQUENCE', 9.5, True, DIM)
        y += 22
        for n in p.main:
            y = self._node(n, 30, y)
        end = self.scene().addRect(QtCore.QRectF(30, y + 4, 62, 22), QtGui.QPen(TEXT, 1.5),
                                   QtGui.QBrush(QtGui.QColor('#15161a')))
        self.text(37, y + 6, '✔ END', 9, True, TEXT)
        y += 44
        self.text(10, y, 'IN THE BACKGROUND — interrupts the main sequence while it runs', 9.5, True, DIM)
        y += 22
        if not p.background:
            self.text(30, y, '(none: "⏱ Every…" adds one)', 9, False, DIM)
            y += 20
        for e in p.background:
            y = self._container(e.uid, 'every', f'⏱ EVERY {e.period_s:g} s', e.text, e.body, 30, y)
        self.setSceneRect(self.scene().itemsBoundingRect().adjusted(-10, -10, 20, 20))
        self.selected_uid = sel
        self.refresh_marks()

    def _node(self, n: Node, x: float, y: float) -> float:
        if n.type in ('repeat', 'foreach'):
            head = f'{ICON[n.type]} {describe_node(n)}'
            return self._container(n.uid, n.type, head, n.text, n.body, x, y)
        sub = ' | '.join(describe_cond(c) for c in n.until)
        if n.type == 'step' and n.timeout_s > 0:
            sub += (' | ' if sub else '') + f'timeout {n.timeout_s:g} s → {OUTCOMES.get(n.on_timeout, n.on_timeout)}'
        if n.type == 'ramp':
            sub = ('waits while ' + ', '.join(f'{p} |err| > {t:g}' for p, t in n.tolerances.items())
                   if n.tolerances else 'no tolerance: never waits')
        if n.type == 'step' and n.action == 'confirm':
            sub = (sub + ' | ' if sub else '') + f'not confirmed → {ON_FAIL.get(n.on_fail, n.on_fail)}'
        h = 42 if sub or n.text else 26
        box = BlockItem(QtCore.QRectF(x, y, W_BOX - x + 30, h - 4), n.uid)
        self.scene().addItem(box)
        self.items_[n.uid] = box
        self.text(x + 8, y + 3, short(describe_node(n), 70), 10, True, parent=box)
        comment = n.text if n.action not in ('none', 'note') or n.type != 'step' else ''   # (already in the title)
        line = ' — '.join(s for s in (comment, ('⏳ ' + sub) if sub else '') if s)
        if line:
            self.text(x + 22, y + 21, short(line, 92), 8.5, False, DIM, parent=box)
        return y + h

    def _container(self, uid: int, kind: str, head: str, text: str, body: List[Node], x: float, y: float) -> float:
        h = 40 if text else 24
        box = BlockItem(QtCore.QRectF(x, y, W_BOX - x + 30, h - 2), uid, fill=HEAD)
        self.scene().addItem(box)
        self.items_[uid] = box
        self.text(x + 8, y + 3, short(head, 70), 10, True, LOOP, parent=box)
        if text:
            self.text(x + 22, y + 20, short(text, 92), 8.5, False, DIM, parent=box)
        y0 = y + h + 2
        y = y0
        if not body:
            self.text(x + 34, y, '(empty: select it and add a block)', 8.5, False, DIM)
            y += 18
        for n in body:
            y = self._node(n, x + 28, y)
        pen = QtGui.QPen(LOOP, 3, QtCore.Qt.DashLine if kind == 'every' else QtCore.Qt.SolidLine)
        path = QtGui.QPainterPath(QtCore.QPointF(x + 14, y0 - 2))
        path.lineTo(x + 8, y0 - 2)
        path.lineTo(x + 8, y + 2)
        path.lineTo(x + 24, y + 2)
        self.scene().addPath(path, pen)
        return y + 10

    def set_run(self, running: Optional[int], frozen: Optional[int]):
        if (running, frozen) != self._run:
            self._run = (running, frozen)
            self.refresh_marks()
            if running in self.items_:
                self.ensureVisible(self.items_[running], 30, 50)

    def refresh_marks(self):
        running, frozen = self._run
        for uid, it in self.items_.items():
            run = 'run' if uid == running else ('frozen' if uid == frozen else None)
            it.set_state(run, uid == self.selected_uid)


# ====================================================================== tab
class RoutinesTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.routines: List[Program] = []
        self._current: Optional[Program] = None
        self.selected: Optional[int] = None
        self._loading = False
        self._rebuild_pending = False
        self._locked: Optional[bool] = None
        self._hist_seen: Tuple = ()
        self._settings: Optional[Parameter] = None
        self._editor_dirty = True
        self._pos_names: Dict[str, Tuple[str, int]] = {}      # parameter name -> (PID id, signal index)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        split.setChildrenCollapsible(False)
        lay.addWidget(split)

        # ---- left: routines
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QtWidgets.QLabel('<b>Routines</b>'))
        self.list = QtWidgets.QListWidget()
        self.list.currentRowChanged.connect(self._on_routine_selected)
        lv.addWidget(self.list, 1)
        grid = QtWidgets.QGridLayout()
        self.btn_new = styled_button('➕ New', None, bold=False)
        self.btn_dup = styled_button('⧉ Duplicate', None, bold=False)
        self.btn_remove = styled_button('❌ Remove', None, bold=False)
        for i, b in enumerate((self.btn_new, self.btn_dup, self.btn_remove)):
            grid.addWidget(b, i // 2, i % 2)
        self.btn_new.clicked.connect(self._on_new)
        self.btn_dup.clicked.connect(self._on_duplicate)
        self.btn_remove.clicked.connect(self._on_remove)
        lv.addLayout(grid)
        split.addWidget(left)

        # ---- middle: diagram + toolbar
        mid = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel('Add:'))
        self.add_buttons = {}
        for key, text, tip in (('step', '▪ Step', 'an action, then wait until… (after the selected step, or into the '
                                                 'selected block)'),
                               ('repeat', '⟲ Repeat', 'a loop'), ('foreach', '⟲ For each', 'a loop over positions'),
                               ('ramp', '↗ Ramp', 'move the setpoints slowly to a position'),
                               ('every', '⏱ Every…', 'a block that runs in the background every N s')):
            b = styled_button(text, None, tip, bold=False)
            b.clicked.connect(lambda _=False, k=key: self._on_add(k))
            bar.addWidget(b)
            self.add_buttons[key] = b
        bar.addSpacing(12)
        self.btn_up = styled_button('▲', None, 'move up', bold=False)
        self.btn_down = styled_button('▼', None, 'move down', bold=False)
        self.btn_copy = styled_button('⧉', None, 'duplicate', bold=False)
        self.btn_del = styled_button('✖', None, 'delete', bold=False)
        for b, fn in ((self.btn_up, lambda: self._on_move(-1)), (self.btn_down, lambda: self._on_move(1)),
                      (self.btn_copy, self._on_copy), (self.btn_del, self._on_delete)):
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        for b in list(self.add_buttons.values()) + [self.btn_up, self.btn_down, self.btn_copy, self.btn_del]:
            b.setStyleSheet('QPushButton { padding: 3px 7px; }')            # compact: the tab fits 1300 px
        for b in (self.btn_up, self.btn_down, self.btn_copy, self.btn_del):
            b.setFixedWidth(34)
        mv.addLayout(bar)
        self.view = BlockView()
        self.view.block_clicked.connect(self.select_block)
        mv.addWidget(self.view, 1)
        split.addWidget(mid)

        # ---- right: editor + run
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.tree = ParameterTree()
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 170)
        rv.addWidget(self.tree, 3)
        head = QtWidgets.QHBoxLayout()
        self.badge = StateBadge()
        head.addWidget(self.badge, 1)
        self.btn_run = styled_button('▶ Run', '#27ae60', 'Try the routine (it is run from the Alignment tab)')
        self.btn_stop = styled_button('⏹ Stop', '#7f8c8d')
        self.btn_resume = styled_button('⏯', '#2980b9', 'Resume after a safety hold during a move')
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_resume.clicked.connect(self._on_resume)
        for b in (self.btn_run, self.btn_stop, self.btn_resume):
            head.addWidget(b)
        rv.addLayout(head)
        self.status = QtWidgets.QLabel('')
        self.status.setWordWrap(True)
        self.status.setFont(mono_font(9))
        rv.addWidget(self.status)
        self.answer_row = QtWidgets.QHBoxLayout()
        self.answer_buttons = {}
        for k, text in ANSWERS.items():
            b = styled_button(text, None, bold=True)
            b.clicked.connect(lambda _=False, c=k: self._on_answer(c))
            self.answer_row.addWidget(b)
            self.answer_buttons[k] = b
            b.setVisible(False)
        rv.addLayout(self.answer_row)
        self.problems = QtWidgets.QLabel('')
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet('font-weight: bold;')
        rv.addWidget(self.problems)
        self.history = QtWidgets.QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setFont(mono_font(8, bold=False))
        self.history.setMaximumBlockCount(400)
        self.history.setPlaceholderText('Run history')
        rv.addWidget(self.history, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        split.setSizes([160, 640, 470])
        self._show_routine()

    # ------------------------------------------------------------------ helpers
    def _auto(self, r: Optional[Program] = None):
        r = r or self._current
        return self.app.routine(r.id) if r is not None else None

    def _state(self, r: Optional[Program] = None) -> AutoState:
        a = self._auto(r)
        return a.state if a is not None else AutoState.OFF

    def _running(self, r: Optional[Program] = None) -> bool:
        return self._state(r) in ENGAGED_STATES

    def _dirty(self):
        self.app.preset_dirty = True

    def _pids(self) -> List[Tuple[str, str, List[str]]]:
        """[(PID:id, label, [signal of each loop])]."""
        tab = self.app.tabs.get('pid')
        out = []
        for c in (tab.configs() if tab is not None else []):
            out.append((f"PID:{c['id']}", c.get('label') or c['id'], [l.get('signal', '') for l in c['loops']]))
        return out

    def _lists(self) -> Dict[str, List[Tuple[str, str]]]:
        opt_tab = self.app.tabs.get('optimizer')
        pids = [(pid, f'PID {label}') for pid, label, _ in self._pids()]
        opts = [(f"OPT:{c['id']}", f"Optimization {c.get('label') or c['id']}")
                for c in (opt_tab.configs() if opt_tab is not None else [])]
        roles = self.app.core.roles
        return {'auto': pids + opts, 'opt': opts, 'pid': pids,
                'motor': [(r, r) for r in sorted(roles.actuators)], 'signal': [(r, r) for r in sorted(roles.signals)]}

    def set_hardware(self):
        self._editor_dirty = True
        if self.isVisible():
            self._build_editor()

    # ------------------------------------------------------------------ routines list
    def _item_text(self, r: Program) -> str:
        mark = {AutoState.ACTIVE: '▶ ', AutoState.HELD: '■ ', AutoState.PAUSED: '‖ ',
                AutoState.ERROR: '✖ '}.get(self._state(r), '')
        a = self._auto(r)
        if a is not None and getattr(a, 'question', None) is not None:
            mark = '⚠ '
        return f"{mark}{r.name()}{' ⚠' if validate_program(r) else ''}"

    def _rebuild_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for r in self.routines:
            self.list.addItem(self._item_text(r))
        if self._current in self.routines:
            self.list.setCurrentRow(self.routines.index(self._current))
        self.list.blockSignals(False)

    def _on_routine_selected(self, row: int):
        self._current = self.routines[row] if 0 <= row < len(self.routines) else None
        self.selected = None
        self._hist_seen = ()
        self._show_routine()

    def select(self, auto_id: str) -> bool:
        rid = auto_id.split(':', 1)[-1]
        for i, r in enumerate(self.routines):
            if r.id == rid:
                self.list.setCurrentRow(i)
                return True
        return False

    def _new_id(self, base: str) -> str:
        rid = sanitize_role_name(base) or 'Routine'
        ids = {r.id for r in self.routines}
        out, n = rid, 2
        while out in ids:
            out, n = f'{rid}_{n}', n + 1
        return out

    def _on_new(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'New routine', 'Name:')
        if not ok or not name.strip():
            return
        r = Program(self._new_id(name.strip()), name.strip(), main=[Node(action='note', text=f'{name.strip()} started')])
        self.routines.append(r)
        self._current = r
        self.selected = None
        self._rebuild_list()
        self._show_routine()
        self._dirty()

    def _on_duplicate(self):
        if self._current is None:
            return
        r = Program.from_dict(copy.deepcopy(self._current.to_dict()))
        r.id = self._new_id(self._current.id + '_copy')
        r.label = self._current.name() + ' (copy)'
        self.routines.append(r)
        self._current = r
        self._rebuild_list()
        self._show_routine()
        self._dirty()

    def _on_remove(self):
        r = self._current
        if r is None:
            return
        if self._running(r):
            warn(self, 'Routine', 'Stop the routine before removing it.')
            return
        if not ask_yes_no(self, 'Remove', f'Remove the routine "{r.name()}" from the preset?'):
            return
        self.app.core.remove_automation(f'RTN:{r.id}')
        self.routines.remove(r)
        self._current = self.routines[0] if self.routines else None
        self.selected = None
        self._rebuild_list()
        self._show_routine()
        self._dirty()

    # ------------------------------------------------------------------ diagram / selection
    def _show_routine(self):
        r = self._current
        for w in (self.btn_dup, self.btn_remove, self.btn_run):
            w.setEnabled(r is not None)
        self.history.clear()
        self.view.selected_uid = self.selected
        self.view.draw(r)
        self._editor_dirty = True
        if self.isVisible() or self._settings is None:
            self._build_editor()
        self._show_problems()
        self.refresh()

    def select_block(self, uid: int):
        if self._current is None or block_of(self._current, uid) is None:
            return
        self.selected = uid
        self.view.selected_uid = uid
        self.view.refresh_marks()
        if uid in self.view.items_:
            self.view.ensureVisible(self.view.items_[uid], 30, 50)
        self._build_editor()

    def _redraw(self):
        self.view.selected_uid = self.selected
        self.view.draw(self._current)
        self._show_problems()
        item = self.list.item(self.routines.index(self._current)) if self._current in self.routines else None
        if item is not None:
            item.setText(self._item_text(self._current))

    def _show_problems(self):
        errs = validate_program(self._current) if self._current is not None else []
        self.problems.setText('\n'.join(f'⚠ {e}' for e in errs[:6]) + (f'\n⚠ … ({len(errs) - 6} more)'
                                                                        if len(errs) > 6 else ''))
        self.problems.setVisible(bool(errs))

    # ------------------------------------------------------------------ editing the structure
    def _edit(self, fn) -> None:
        if self._current is None or self._running():
            return
        fn()
        self._dirty()
        self._redraw()
        self._build_editor()

    def _on_add(self, kind: str):
        r = self._current
        if r is None or self._running():
            return
        if kind == 'every':
            block = Every(30.0, 'background block', [Node(until=[Cond('delay', value=1.0)])])
        elif kind == 'repeat':
            block = Node(type='repeat', body=[Node(until=[Cond('delay', value=1.0)])])
        elif kind == 'foreach':
            block = Node(type='foreach', positions=[Position('P1')], body=[Node(type='ramp', speed=0.2)])
        elif kind == 'ramp':
            block = Node(type='ramp', speed=0.2)
        else:
            block = Node(until=[Cond('delay', value=1.0)])

        def add():
            insert_block(r, block, self.selected)
            self.selected = block.uid
        self._edit(add)

    def _on_move(self, delta: int):
        if self.selected is not None:
            self._edit(lambda: move_block(self._current, self.selected, delta))

    def _on_copy(self):
        if self.selected is None:
            return

        def dup():
            clone = duplicate_block(self._current, self.selected)
            if clone is not None:
                self.selected = clone.uid
        self._edit(dup)

    def _on_delete(self):
        if self.selected is None:
            return

        def rm():
            remove_block(self._current, self.selected)
            self.selected = None
        self._edit(rm)

    # ------------------------------------------------------------------ editor (parameter tree)
    @property
    def settings(self) -> Parameter:
        if self._settings is None or self._editor_dirty:
            self._build_editor()
        return self._settings

    def showEvent(self, e):
        super().showEvent(e)
        if self._editor_dirty:
            self._build_editor()

    def _routine_params(self, r: Program) -> dict:
        return {'title': 'Routine', 'name': 'routine', 'type': 'group', 'children': [
            {'title': 'Display name:', 'name': 'label', 'type': 'str', 'value': r.label},
            {'title': 'Button in Alignment:', 'name': 'show_in_alignment', 'type': 'bool', 'value': r.show_in_alignment},
            {'title': 'On failure / stop: stop what it started:', 'name': 'stop_started_on_abort', 'type': 'bool',
             'value': r.stop_started_on_abort, 'tip': 'what it paused stays paused (nothing moves); the log says so'},
            {'title': 'Description:', 'name': 'description', 'type': 'str', 'value': r.description}]}

    def _tol_group(self, name: str, title: str, tols: Dict[str, float], tip: str) -> dict:
        return {'title': title, 'name': name, 'type': 'group', 'tip': tip, 'children': [
            {'title': f'{label}:', 'name': pid, 'type': 'float', 'value': float(tols.get(pid, 0.0)), 'min': 0.0,
             'decimals': 3, 'tip': '|error| tolerance of every signal of this PID · 0 = not used'}
            for pid, label, _ in self._pids()]}

    def _position_children(self, pos: Position, used: List[str]) -> list:
        kids = [{'title': 'Name:', 'name': 'name', 'type': 'str', 'value': pos.name}]
        sigs = {pid: s for pid, _, s in self._pids()}
        for pid in used:
            values = pos.setpoints.get(pid, [])
            for k, sig in enumerate(sigs.get(pid, [''])):
                pname = f'sp{len(self._pos_names)}'
                self._pos_names[pname] = (pid, k)
                kids.append({'title': f'{pid[4:]} · {sig or k + 1}:', 'name': pname, 'type': 'float',
                             'value': float(values[k]) if k < len(values) else 0.0, 'decimals': 4})
        return kids

    def _block_params(self, b) -> list:
        lists = self._lists()
        if isinstance(b, Every):
            return [{'title': '⏱ EVERY (background)', 'name': 'block', 'type': 'group', 'children': [
                {'title': 'Period (s):', 'name': 'period_s', 'type': 'float', 'value': b.period_s, 'min': 0.1,
                 'tip': 'counted while the main sequence runs (from the end of the previous run of this block)'},
                {'title': 'Comment:', 'name': 'text', 'type': 'str', 'value': b.text}]}]
        if b.type == 'repeat':
            return [{'title': '⟲ REPEAT', 'name': 'block', 'type': 'group', 'children': [
                {'title': 'Times (0 = forever):', 'name': 'times', 'type': 'int', 'value': b.times, 'min': 0,
                 'tip': 'left earlier by a "leave the loop" outcome, an END or a failure'},
                {'title': 'Comment:', 'name': 'text', 'type': 'str', 'value': b.text}]}]
        if b.type in ('foreach', 'ramp'):
            used = list(dict.fromkeys(pid for pos in b.positions for pid in pos.setpoints))
            kids = []
            if b.type == 'ramp':
                kids += [{'title': 'Speed (units / s):', 'name': 'speed', 'type': 'float', 'value': b.speed,
                          'min': 0.0, 'tip': 'of every setpoint'},
                         self._tol_group('tolerances', 'Waits while out of tolerance', b.tolerances,
                                         'the ramp waits while a PID lags (|error| above its tolerance)')]
            kids.append({'title': 'Comment:', 'name': 'text', 'type': 'str', 'value': b.text})
            needs_positions = b.type == 'foreach' or not inside_foreach(self._current, b.uid)
            if needs_positions:
                kids.append({'title': 'PIDs of the positions', 'name': 'pids', 'type': 'checklist',
                             'limits': [pid for pid, _, _ in self._pids()], 'value': used, 'exclusive': False,
                             'delay': 0.1, 'expanded': False})
                positions = b.positions if b.type == 'foreach' else (b.positions or [Position('target')])[:1]
                if b.type == 'ramp' and not b.positions:
                    b.positions = positions
                grp = []
                for k, pos in enumerate(positions):
                    children = self._position_children(pos, used)
                    if b.type == 'foreach':
                        children.append({'title': '✖ Remove this position', 'name': 'remove', 'type': 'action'})
                    grp.append({'title': f'Position {k + 1}: {pos.name or "?"}', 'name': f'pos{k}', 'type': 'group',
                                'children': children})
                if b.type == 'foreach':
                    grp.append({'title': '➕ Add a position', 'name': 'add', 'type': 'action'})
                kids.append({'title': 'Positions' if b.type == 'foreach' else 'Target position', 'name': 'positions',
                             'type': 'group', 'children': grp})
            else:
                kids.append({'title': 'Target:', 'name': 'target_info', 'type': 'str', 'readonly': True,
                             'value': 'the position of the enclosing FOR EACH'})
            title = '⟲ FOR EACH position' if b.type == 'foreach' else '↗ Ramp of the setpoints'
            return [{'title': title, 'name': 'block', 'type': 'group', 'children': kids}]
        # step
        kind = TARGET_KIND.get(b.action)
        show_opt = b.action == 'move' or (b.action == 'start' and b.target.startswith('OPT:'))
        opts = ({'': 'as its button', **START_OPTIONS} if b.action == 'start'
                else {'relative': 'relative', 'absolute': 'absolute'})
        conds = []
        for k, c in enumerate(b.until):
            ck = COND_TARGET.get(c.kind)
            kids = [{'title': 'Condition:', 'name': 'kind', 'type': 'list',
                     'limits': {v: key for key, v in CONDITIONS.items()}, 'value': c.kind}]
            if ck:
                kids.append({'title': 'Target:', 'name': 'target', 'type': 'list',
                             'limits': _limits(lists[ck], c.target), 'value': c.target})
            if c.kind == 'locked':
                kids.append(self._tol_group('tolerances', 'PIDs and tolerances', c.pids(),
                                            'every signal of each PID with a tolerance > 0 must be within it'))
            if c.kind == 'signal':
                kids.append({'title': 'Test:', 'name': 'op', 'type': 'list', 'limits': list(SIGNAL_OPS), 'value': c.op})
            if c.kind not in ('locked', 'finished'):
                kids.append({'title': 'Value:', 'name': 'value', 'type': 'float', 'value': c.value, 'decimals': 4,
                             'tip': COND_TIP.get(c.kind, '')})
            if c.kind in ('locked', 'signal'):
                kids.append({'title': 'Hold (s):', 'name': 'hold_s', 'type': 'float', 'value': c.hold_s, 'min': 0.0,
                             'tip': 'must stay true that long'})
            kids += [{'title': 'Then:', 'name': 'outcome', 'type': 'list',
                      'limits': {v: key for key, v in OUTCOMES.items()}, 'value': c.outcome},
                     {'title': '✖ Remove this condition', 'name': 'remove', 'type': 'action'}]
            conds.append({'title': f'{k + 1} · {describe_cond(c)}', 'name': f'c{k}', 'type': 'group',
                          'expanded': len(b.until) <= 2, 'children': kids})
        conds.append({'title': '➕ Add a condition', 'name': 'add', 'type': 'action'})
        kids = [{'title': 'Action:', 'name': 'action', 'type': 'list',
                 'limits': {v: key for key, v in STEP_ACTIONS.items()}, 'value': b.action}]
        if kind:
            kids.append({'title': 'Target:', 'name': 'target', 'type': 'list', 'limits': _limits(lists[kind], b.target),
                         'value': b.target})
        if show_opt:
            kids.append({'title': 'Mode:', 'name': 'option', 'type': 'list', 'limits': {v: k for k, v in opts.items()},
                         'value': b.option if b.action == 'start' else (b.option or 'relative')})
        if b.action == 'move':
            kids.append({'title': 'Step / position:', 'name': 'value', 'type': 'float', 'value': b.value,
                         'decimals': 4})
        if b.action == 'confirm':
            kids.append({'title': 'If not confirmed:', 'name': 'on_fail', 'type': 'list',
                         'limits': {v: k for k, v in ON_FAIL.items()}, 'value': b.on_fail,
                         'tip': 'ask the operator: what the routine paused is resumed (the PIDs hold the beam), '
                                'Continue / Check again / Stop in the Alignment tab'})
        kids += [{'title': 'Comment:' if b.action != 'note' else 'Note:', 'name': 'text', 'type': 'str',
                  'value': b.text},
                 {'title': 'Then wait until (first true wins; none = go on when done)', 'name': 'until',
                  'type': 'group', 'children': conds},
                 {'title': 'Timeout (s, 0 = none):', 'name': 'timeout_s', 'type': 'float', 'value': b.timeout_s,
                  'min': 0.0, 'tip': 'counted from the end of the action'},
                 {'title': 'On timeout:', 'name': 'on_timeout', 'type': 'list',
                  'limits': {v: k for k, v in OUTCOMES.items()}, 'value': b.on_timeout,
                  'visible': b.timeout_s > 0}]
        return [{'title': '▪ Step', 'name': 'block', 'type': 'group', 'children': kids}]

    def _build_editor(self):
        self._editor_dirty = False
        r = self._current
        self._pos_names = {}
        self._loading = True
        try:
            children = []
            if r is not None:
                children.append(self._routine_params(r))
                b = block_of(r, self.selected) if self.selected is not None else None
                if b is not None:
                    children += self._block_params(b)
                else:
                    children.append({'title': 'Click a block in the diagram to edit it', 'name': 'hint',
                                     'type': 'str', 'value': '', 'readonly': True})
            self._settings = Parameter.create(name='routine', type='group', children=children)
            self.tree.setParameters(self._settings, showTop=False)
            self._settings.sigTreeStateChanged.connect(self._on_tree_changed)
            self._connect_actions()
        finally:
            self._loading = False
        self._locked = None
        self.refresh()

    def _connect_actions(self):
        if self._current is None or self.selected is None:
            return
        b = block_of(self._current, self.selected)
        if b is None:
            return
        blk = self._settings.child('block')
        if isinstance(b, Node) and b.type == 'step':
            until = blk.child('until')
            until.child('add').sigActivated.connect(lambda: self._edit(lambda: b.until.append(Cond('delay', value=1.0))))
            for k in range(len(b.until)):
                until.child(f'c{k}', 'remove').sigActivated.connect(lambda _=None, k=k: self._edit(lambda: b.until.pop(k)))
        if isinstance(b, Node) and b.type == 'foreach':
            pos = blk.child('positions')
            pos.child('add').sigActivated.connect(lambda: self._edit(lambda: b.positions.append(
                Position(f'P{len(b.positions) + 1}', {p: list(v) for p, v in (b.positions[-1].setpoints.items()
                                                                              if b.positions else [])}))))
            for k in range(len(b.positions)):
                pos.child(f'pos{k}', 'remove').sigActivated.connect(
                    lambda _=None, k=k: self._edit(lambda: b.positions.pop(k)))

    def _schedule_rebuild(self):
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QtCore.QTimer.singleShot(0, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_pending = False
        self._build_editor()

    def _on_tree_changed(self, _root, changes):
        if self._loading or self._current is None:
            return
        r = self._current
        b = block_of(r, self.selected) if self.selected is not None else None
        structural = False
        for param, change, data in changes:
            if change != 'value':
                continue
            path = self._settings.childPath(param) or []
            if len(path) == 2 and path[0] == 'routine':
                setattr(r, path[1], data)
                continue
            if not path or path[0] != 'block' or b is None:
                continue
            rest = path[1:]
            if isinstance(b, Every):
                if rest[0] in ('period_s', 'text'):
                    setattr(b, rest[0], float(data) if rest[0] == 'period_s' else data)
                continue
            if b.type == 'repeat':
                if rest[0] == 'times':
                    b.times = int(data)
                elif rest[0] == 'text':
                    b.text = data
                continue
            if b.type in ('foreach', 'ramp'):
                if rest[0] in ('speed', 'text'):
                    setattr(b, rest[0], float(data) if rest[0] == 'speed' else data)
                elif rest[0] == 'tolerances' and len(rest) == 2:
                    self._set_tol(b.tolerances, rest[1], data)
                elif rest[0] == 'pids':
                    used = list(self._settings.child('block', 'pids').value())
                    defaults = {pid: [0.0] * len(sigs) for pid, _, sigs in self._pids()}
                    for pos in b.positions:
                        pos.setpoints = {pid: pos.setpoints.get(pid, defaults.get(pid, [0.0])) for pid in used}
                    structural = True
                elif rest[0] == 'positions' and len(rest) == 3 and rest[1].startswith('pos'):
                    pos = b.positions[int(rest[1][3:])]
                    if rest[2] == 'name':
                        pos.name = data
                    elif rest[2] in self._pos_names:
                        pid, k = self._pos_names[rest[2]]
                        vals = pos.setpoints.setdefault(pid, [])
                        while len(vals) <= k:
                            vals.append(0.0)
                        vals[k] = float(data)
                continue
            # step
            name = rest[0]
            if len(rest) == 1:
                if name == 'action':
                    b.action, b.target, b.option = data, '', ''
                    structural = True
                elif name == 'target':
                    b.target = data or ''
                    structural = b.action == 'start'
                elif name in ('option', 'text', 'on_fail', 'on_timeout'):
                    setattr(b, name, data or '')
                elif name == 'value':
                    b.value = float(data)
                elif name == 'timeout_s':
                    structural = (b.timeout_s > 0) != (float(data) > 0)
                    b.timeout_s = float(data)
            elif name == 'until' and len(rest) >= 3 and rest[1].startswith('c'):
                c = b.until[int(rest[1][1:])]
                key = rest[2]
                if key == 'kind':
                    c.kind, c.target, c.tolerances = data, '', {}
                    if c.kind == 'locked' and c.hold_s == 0:
                        c.hold_s = 3.0
                    structural = True
                elif key == 'tolerances' and len(rest) == 4:
                    if c.target:                                   # short form -> explicit tolerances
                        c.tolerances = c.pids()
                        c.target, c.value = '', 0.0
                    self._set_tol(c.tolerances, rest[3], data)
                elif key in ('target', 'op', 'outcome'):
                    setattr(c, key, data or '')
                elif key in ('value', 'hold_s'):
                    setattr(c, key, float(data))
        self._dirty()
        self._redraw()
        if structural:
            self._schedule_rebuild()

    @staticmethod
    def _set_tol(tols: Dict[str, float], pid: str, value):
        if float(value) > 0:
            tols[pid] = float(value)
        else:
            tols.pop(pid, None)

    def _set_locked(self, locked: bool):
        if self._locked == locked or self._settings is None:
            return
        self._locked = locked

        def walk(p):
            for c in p.children():
                if c.type() == 'action':
                    c.setOpts(enabled=not locked)
                elif c.type() not in ('group',):
                    c.setOpts(readonly=locked)
                walk(c)
        walk(self._settings)
        for b in list(self.add_buttons.values()) + [self.btn_up, self.btn_down, self.btn_copy, self.btn_del,
                                                    self.btn_remove]:
            b.setEnabled(not locked and self._current is not None)

    # ------------------------------------------------------------------ run
    def config_of(self, auto_id: str) -> Optional[dict]:
        rid = auto_id.split(':', 1)[-1]
        for r in self.routines:
            if r.id == rid:
                return r.to_dict()
        return None

    def _on_run(self):
        r = self._current
        if r is None:
            return
        ok, msg = self.app.start_routine(r.to_dict())
        if not ok:
            warn(self, r.name(), 'Cannot start:\n\n' + msg.replace(' ; ', '\n'))
        self.refresh()

    def _on_stop(self):
        a = self._auto()
        if a is not None:
            self.app.core.stop(a.id, 'stopped by the user')
        self.refresh()

    def _on_resume(self):
        a = self._auto()
        if a is not None:
            ok, msg = self.app.core.resume(a.id)
            if not ok:
                warn(self, a.label, f'Resume refused:\n\n{msg}')
        self.refresh()

    def _on_answer(self, choice: str):
        a = self._auto()
        if a is not None:
            self.app.answer_question(a.id, choice)
        self.refresh()

    # ------------------------------------------------------------------ preset
    def load_from_preset(self, preset: dict):
        self.routines = []
        for d in preset.get('routines', []) or []:
            try:
                self.routines.append(Program.from_dict(d))
            except Exception as e:  # an old step routine that cannot be converted: reported, not lost silently
                self.app.core.events.publish(EventKind.INFO, 'UI', f"routine {d.get('id')}: not loaded ({e})",
                                             Level.WARNING)
        self._current = self.routines[0] if self.routines else None
        self.selected = None
        self._rebuild_list()
        self._show_routine()

    def write_to_preset(self, preset: dict):
        preset['routines'] = self.configs()

    def configs(self) -> List[dict]:
        return [r.to_dict() for r in self.routines]

    def quick_items(self) -> List[dict]:
        return [{'id': f'RTN:{r.id}', 'kind': 'routine', 'key': r.id, 'label': r.name(), 'action': ''}
                for r in self.routines if r.show_in_alignment]

    # ------------------------------------------------------------------ refresh (<= 5 Hz)
    def refresh(self):
        for i, r in enumerate(self.routines):
            it = self.list.item(i)
            if it is not None:
                text = self._item_text(r)
                if it.text() != text:
                    it.setText(text)
        r = self._current
        a = self._auto()
        state = a.state if a is not None else AutoState.OFF
        engaged = state in ENGAGED_STATES
        self.badge.set_state(state, a.reason if a is not None else '', prefix=(r.name() + ': ') if r else '')
        self.btn_run.setEnabled(r is not None and not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        self.btn_resume.setEnabled(state == AutoState.HELD)
        self._set_locked(engaged)
        question = getattr(a, 'question', None) if a is not None and engaged else None
        for k, b in self.answer_buttons.items():
            b.setVisible(question is not None)
        if a is None:
            self.status.setText('Not run yet.' if r is not None else '')
        elif engaged:
            self.status.setText(f'▶ {a.status}')
        else:
            outcome = {'done': '✔ Finished', 'failed': '✖ FAILED', 'stopped': '■ Stopped'}.get(a.outcome, 'Stopped')
            self.status.setText(f'{outcome} — {a.reason or a.status}')
        running = frozen = None
        if a is not None and engaged and hasattr(a, 'current'):
            node, fr = a.current
            running = node.uid if node is not None else (a.bg_block.uid if a.bg_block is not None else None)
            frozen = fr.uid if fr is not None else None
        # the run uses a copy of the program: map its blocks to the edited ones by position
        if running is not None or frozen is not None:
            running, frozen = self._map_uid(a, running), self._map_uid(a, frozen)
        self.view.set_run(running, frozen)
        if a is not None:
            hist = tuple(a.history)
            if hist != self._hist_seen:
                new = hist[len(self._hist_seen):] if hist[:len(self._hist_seen)] == self._hist_seen else hist
                if new is hist:
                    self.history.clear()
                for t, text in new:
                    self.history.appendPlainText(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {text}")
                self._hist_seen = hist

    def _map_uid(self, a, uid: Optional[int]) -> Optional[int]:
        """Block of the running copy → same place in the edited program (None when it was edited)."""
        if uid is None or self._current is None or not hasattr(a, 'program'):
            return None
        path = _path_of(a.program, uid)
        return _at_path(self._current, path) if path is not None else None


def _path_of(p: Program, uid: int) -> Optional[tuple]:
    def rec(nodes, pre):
        for i, n in enumerate(nodes):
            if n.uid == uid:
                return pre + (i,)
            r = rec(n.body, pre + (i,))
            if r is not None:
                return r
        return None
    r = rec(p.main, ('main',))
    if r is not None:
        return r
    for j, e in enumerate(p.background):
        if e.uid == uid:
            return ('bg', j)
        r = rec(e.body, ('bg', j))
        if r is not None:
            return r
    return None


def _at_path(p: Program, path: tuple) -> Optional[int]:
    try:
        if path[0] == 'main':
            nodes, rest, cur = p.main, path[1:], None
        else:
            cur = p.background[path[1]]
            nodes, rest = cur.body, path[2:]
        for i in rest:
            cur = nodes[i]
            nodes = cur.body
        return cur.uid if cur is not None else None
    except (IndexError, AttributeError):
        return None
