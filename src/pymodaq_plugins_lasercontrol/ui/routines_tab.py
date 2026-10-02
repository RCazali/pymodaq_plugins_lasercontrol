"""Routines tab: the SETTINGS of the routines (sequences of execution steps, engines/routine.py). They are
run from the Alignment tab (band SEQ); ▶ Run here is for trying them.

Left: the routines of the preset. Middle: the selected routine in a PyMoDAQ parameter tree — one group per
step (action, target, then "wait until" conditions, timeout). Right: run controls, state, problems, history.

The running step is marked "▶" (never by colour alone: goggles). Editing is locked while the routine runs
(a run uses a copy of the steps taken at its start).
"""
from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from qtpy import QtCore, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.roles import sanitize_role_name
from ..core.states import ENGAGED_STATES, AutoState
from ..engines.routine import (ACTIONS, CONDITIONS, MOVE_OPTIONS, SIGNAL_OPS, START_OPTIONS, THENS, Condition,
                               Routine, Step, default_routine, delete_step, describe_action, describe_condition,
                               duplicate_step, insert_step, move_step, validate_routine)
from .widgets import StateBadge, ask_yes_no, mono_font, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

TARGET_KIND = {'start': 'auto', 'stop': 'auto', 'pause': 'auto', 'resume': 'auto', 'move': 'motor'}
COND_TARGET_KIND = {'locked': 'pid', 'signal': 'signal', 'travel': 'motor', 'finished': 'auto'}
COND_VALUE_TIP = {'delay': 'seconds after the end of the action', 'locked': '|error| tolerance (0 = the '
                  'deadband of the PID)', 'signal': 'threshold', 'travel': 'distance from the position at the '
                  'start of the step', 'count': 'number of times this step was entered (loop guard)',
                  'finished': 'not used'}


def _limits(pairs: List[Tuple[str, str]], current: str) -> Dict[str, str]:
    """{text shown: value} for a list parameter, keeping an unknown current value (⚠)."""
    out = {'— choose —': ''}                     # an empty target is shown as such (never a silent first item)
    out.update({text: value for value, text in pairs})
    if current and current not in out.values():
        out[f'⚠ {current} (unknown)'] = current
    return out


def step_title(i: int, s: Step, current: bool = False) -> str:
    """'3 · Wait · until PID:X converged (…)' — the conditions in short (details inside the group)."""
    until = ' | '.join(describe_condition(c).split(' → ')[0] for c in s.until)
    return f"{'▶ ' if current else ''}{i + 1} · {describe_action(s)}" + (f' · until {until}' if until else '')


def routine_tree(r: Routine, lists: Dict[str, List[Tuple[str, str]]], n_steps: int) -> list:
    steps = []
    for i, s in enumerate(r.steps):
        kind = TARGET_KIND.get(s.action)
        # start of an optimization: '' = the action of its Alignment button; move: '' = relative
        opts = {'': 'as its button', **START_OPTIONS} if s.action == 'start' else MOVE_OPTIONS
        show_opt = s.action == 'move' or (s.action == 'start' and s.target.startswith('OPT:'))
        conds = []
        for k, c in enumerate(s.until):
            ck = COND_TARGET_KIND.get(c.kind)
            conds.append({'title': f'{k + 1} · {describe_condition(c)}', 'name': f'c{k}', 'type': 'group',
                          'expanded': False, 'children': [
                {'title': 'Condition:', 'name': 'kind', 'type': 'list',
                 'limits': {v: k2 for k2, v in CONDITIONS.items()}, 'value': c.kind},
                {'title': 'Target:', 'name': 'target', 'type': 'list', 'visible': ck is not None,
                 'limits': _limits(lists.get(ck, []), c.target) if ck else {'—': ''},
                 'value': c.target if ck else ''},
                {'title': 'Test:', 'name': 'op', 'type': 'list', 'limits': list(SIGNAL_OPS), 'value': c.op,
                 'visible': c.kind == 'signal'},
                {'title': 'Value:', 'name': 'value', 'type': 'float', 'value': c.value, 'decimals': 4,
                 'visible': c.kind != 'finished', 'tip': COND_VALUE_TIP.get(c.kind, '')},
                {'title': 'Hold (s):', 'name': 'hold_s', 'type': 'float', 'value': c.hold_s, 'min': 0.0,
                 'visible': c.kind in ('locked', 'signal'), 'tip': 'must stay true that long'},
                {'title': 'Then:', 'name': 'then', 'type': 'list', 'limits': {v: k2 for k2, v in THENS.items()},
                 'value': c.then},
                {'title': 'Go to step:', 'name': 'goto', 'type': 'int', 'value': c.goto, 'min': 1,
                 'max': max(n_steps, 1), 'visible': c.then == 'goto'},
                {'title': '✖ Remove this condition', 'name': 'remove', 'type': 'action'}]})
        conds.append({'title': '➕ Add a condition', 'name': 'add', 'type': 'action'})
        steps.append({'title': step_title(i, s), 'name': f's{i}', 'type': 'group', 'expanded': False, 'children': [
            {'title': 'Action:', 'name': 'action', 'type': 'list', 'limits': {v: k for k, v in ACTIONS.items()},
             'value': s.action},
            {'title': 'Target:', 'name': 'target', 'type': 'list', 'visible': kind is not None,
             'limits': _limits(lists.get(kind, []), s.target) if kind else {'—': ''},
             'value': s.target if kind else ''},
            {'title': 'Mode:', 'name': 'option', 'type': 'list', 'visible': show_opt,
             'limits': {v: k for k, v in opts.items()},
             'value': s.option if s.action == 'start' else (s.option or 'relative')},
            {'title': 'Step / position:', 'name': 'value', 'type': 'float', 'value': s.value, 'decimals': 4,
             'visible': s.action == 'move'},
            {'title': 'Note:' if s.action == 'note' else 'Comment:', 'name': 'text', 'type': 'str', 'value': s.text},
            {'title': 'Then wait until (first true wins; none = go on when done)', 'name': 'until', 'type': 'group',
             'children': conds},
            {'title': 'Timeout (s, 0 = none):', 'name': 'timeout_s', 'type': 'float', 'value': s.timeout_s,
             'min': 0.0, 'tip': 'counted from the end of the action'},
            {'title': 'On timeout:', 'name': 'on_timeout', 'type': 'list', 'limits': {v: k for k, v in THENS.items()},
             'value': s.on_timeout, 'visible': s.timeout_s > 0},
            {'title': 'Go to step:', 'name': 'timeout_goto', 'type': 'int', 'value': s.timeout_goto, 'min': 1,
             'max': max(n_steps, 1), 'visible': s.timeout_s > 0 and s.on_timeout == 'goto'}]})
    steps.append({'title': '➕ Add a step', 'name': 'add', 'type': 'action'})
    return [
        {'title': 'Routine', 'name': 'routine', 'type': 'group', 'children': [
            {'title': 'Display name:', 'name': 'label', 'type': 'str', 'value': r.label},
            {'title': 'Button in Alignment:', 'name': 'show_in_alignment', 'type': 'bool',
             'value': r.show_in_alignment},
            {'title': 'On failure / stop: stop what it started:', 'name': 'stop_started_on_abort', 'type': 'bool',
             'value': r.stop_started_on_abort,
             'tip': 'What it paused stays paused (nothing moves); the log says so'},
            {'title': 'Description:', 'name': 'description', 'type': 'str', 'value': r.description}]},
        {'title': 'Steps', 'name': 'steps', 'type': 'group', 'children': steps},
    ]


class RoutinesTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self.routines: List[Routine] = []
        self._current: Optional[Routine] = None
        self._hist_seen: Tuple = ()
        self._marked: Tuple = ()
        self._loading = False
        self._rebuild_pending = False
        self._locked: Optional[bool] = None
        self._settings: Optional[Parameter] = None
        self._tree_dirty = True                      # built when the tab is shown / used (fast preset loading)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        split.setChildrenCollapsible(False)
        lay.addWidget(split)

        # ---- left: the routines
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

        # ---- middle: the tree
        mid = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel('Selected step:'))
        self.btn_up = styled_button('▲', None, bold=False, tip='Move up (the "go to" numbers follow)')
        self.btn_down = styled_button('▼', None, bold=False, tip='Move down')
        self.btn_copy = styled_button('⧉', None, bold=False, tip='Duplicate the step')
        self.btn_del = styled_button('✖', None, bold=False, tip='Delete the step')
        for b, fn in ((self.btn_up, lambda: self._on_move_step(-1)), (self.btn_down, lambda: self._on_move_step(1)),
                      (self.btn_copy, self._on_copy_step), (self.btn_del, self._on_delete_step)):
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        mv.addLayout(bar)
        self.tree = ParameterTree()
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 330)
        mv.addWidget(self.tree, 1)
        split.addWidget(mid)

        # ---- right: run
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.name = QtWidgets.QLabel('')
        f = self.name.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self.name.setFont(f)
        self.name.setWordWrap(True)
        rv.addWidget(self.name)
        self.badge = StateBadge()
        rv.addWidget(self.badge)
        row = QtWidgets.QHBoxLayout()
        self.btn_run = styled_button('▶ Run', '#27ae60', 'Try the routine (it is run from the Alignment tab)')
        self.btn_stop = styled_button('⏹ Stop', '#7f8c8d')
        self.btn_resume = styled_button('⏯ Resume', '#2980b9', 'after a safety hold during a move step')
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_resume.clicked.connect(self._on_resume)
        for b in (self.btn_run, self.btn_stop, self.btn_resume):
            row.addWidget(b)
        rv.addLayout(row)
        self.status = QtWidgets.QLabel('')
        self.status.setWordWrap(True)
        self.status.setFont(mono_font(10))
        rv.addWidget(self.status)
        self.problems = QtWidgets.QLabel('')
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet('font-weight: bold;')
        rv.addWidget(self.problems)
        self.history = QtWidgets.QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setFont(mono_font(9, bold=False))
        self.history.setMaximumBlockCount(400)
        self.history.setPlaceholderText('Run history')
        rv.addWidget(self.history, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 1)
        split.setSizes([170, 760, 330])
        self._show_routine()

    @property
    def settings(self) -> Parameter:
        if self._tree_dirty or self._settings is None:
            self._build_tree(force=True)
        return self._settings

    def showEvent(self, e):
        super().showEvent(e)
        if self._tree_dirty:
            self._build_tree(force=True)

    # ------------------------------------------------------------------ helpers
    def _auto(self, r: Optional[Routine] = None):
        r = r or self._current
        return self.app.routine(r.id) if r is not None else None

    def _state(self, r: Optional[Routine] = None) -> AutoState:
        a = self._auto(r)
        return a.state if a is not None else AutoState.OFF

    def _running(self, r: Optional[Routine] = None) -> bool:
        return self._state(r) in ENGAGED_STATES

    def _dirty(self):
        self.app.preset_dirty = True

    def _lists(self) -> Dict[str, List[Tuple[str, str]]]:
        pid_tab, opt_tab = self.app.tabs.get('pid'), self.app.tabs.get('optimizer')
        pids = [(f"PID:{c['id']}", f"PID {c.get('label') or c['id']}")
                for c in (pid_tab.configs() if pid_tab is not None else [])]
        opts = [(f"OPT:{c['id']}", f"Optimization {c.get('label') or c['id']}")
                for c in (opt_tab.configs() if opt_tab is not None else [])]
        roles = self.app.core.roles
        return {'pid': pids, 'auto': pids + opts, 'motor': [(r, r) for r in sorted(roles.actuators)],
                'signal': [(r, r) for r in sorted(roles.signals)]}

    def set_hardware(self):
        self._build_tree()

    # ------------------------------------------------------------------ routines list
    def _item_text(self, r: Routine) -> str:
        mark = {AutoState.ACTIVE: '▶ ', AutoState.HELD: '■ ', AutoState.PAUSED: '‖ ',
                AutoState.ERROR: '✖ '}.get(self._state(r), '')
        return f"{mark}{r.name()}{' ⚠' if validate_routine(r) else ''}"

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
        r = Routine.from_dict(default_routine(self._new_id(name.strip())))
        r.label = name.strip()
        r.steps = [Step('note', text=f'{r.label} started')]
        self.routines.append(r)
        self._current = r
        self._rebuild_list()
        self._show_routine()
        self._dirty()

    def _on_duplicate(self):
        if self._current is None:
            return
        r = Routine.from_dict(copy.deepcopy(self._current.to_dict()))
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
        self._rebuild_list()
        self._show_routine()
        self._dirty()

    # ------------------------------------------------------------------ tree
    def _show_routine(self):
        r = self._current
        for w in (self.btn_dup, self.btn_remove, self.btn_run):
            w.setEnabled(r is not None)
        self.history.clear()
        self._build_tree()
        self.refresh()

    def _build_tree(self, expand_step: Optional[int] = None, force: bool = False):
        if not force and not self.isVisible():
            self._tree_dirty = True
            return
        self._tree_dirty = False
        r = self._current
        expanded = self._expanded() if self._settings is not None else set()
        if expand_step is not None:
            expanded.add(('steps', f's{expand_step}'))
        self._loading = True
        try:
            if r is None:
                self._settings = Parameter.create(name='routine', type='group', children=[
                    {'title': 'No routine: "➕ New"', 'name': 'none', 'type': 'str', 'value': '', 'readonly': True}])
                self.tree.setParameters(self.settings, showTop=False)
                return
            self._settings = Parameter.create(name='routine', type='group',
                                              children=routine_tree(r, self._lists(), len(r.steps)))
            for path in expanded:
                try:
                    self.settings.child(*path).setOpts(expanded=True)
                except Exception:
                    pass
            self.tree.setParameters(self.settings, showTop=False)
            self.settings.sigTreeStateChanged.connect(self._on_tree_changed)
            steps = self.settings.child('steps')
            steps.child('add').sigActivated.connect(self._on_add_step)
            for i, s in enumerate(r.steps):
                until = steps.child(f's{i}', 'until')
                until.child('add').sigActivated.connect(lambda _=None, i=i: self._on_add_condition(i))
                for k in range(len(s.until)):
                    until.child(f'c{k}', 'remove').sigActivated.connect(
                        lambda _=None, i=i, k=k: self._on_remove_condition(i, k))
        finally:
            self._loading = False
        self._locked = None
        self._marked = ()
        self._show_problems()

    def _expanded(self) -> set:
        out = set()
        if self._current is None or self._settings is None:
            return out
        try:
            steps = self.settings.child('steps')
        except Exception:
            return out
        for c in steps.children():
            if c.type() == 'group' and c.opts.get('expanded'):
                out.add(('steps', c.name()))
                until = c.child('until')
                for cc in until.children():
                    if cc.type() == 'group' and cc.opts.get('expanded'):
                        out.add(('steps', c.name(), 'until', cc.name()))
        return out

    def _schedule_rebuild(self):
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QtCore.QTimer.singleShot(0, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_pending = False
        self._build_tree()

    def _on_tree_changed(self, _root, changes):
        if self._loading or self._current is None:
            return
        r = self._current
        structural = False
        for param, change, data in changes:
            if change != 'value':
                continue
            path = self.settings.childPath(param) or []
            if len(path) == 2 and path[0] == 'routine':
                setattr(r, path[1], data)
                if path[1] == 'label':
                    item = self.list.item(self.routines.index(r))
                    if item is not None:
                        item.setText(self._item_text(r))
                continue
            if len(path) < 3 or path[0] != 'steps' or not path[1].startswith('s'):
                continue
            i = int(path[1][1:])
            s = r.steps[i]
            if len(path) == 3:
                name = path[2]
                if name == 'action':
                    s.action = data
                    s.target, s.option = '', ''
                    structural = True
                elif name == 'target':
                    s.target = data or ''
                    structural = s.action == 'start'      # the mode depends on OPT / PID
                elif name == 'value':
                    s.value = float(data)
                elif name in ('option', 'text', 'on_timeout'):
                    setattr(s, name, data or '')
                    structural = name == 'on_timeout'
                elif name == 'timeout_s':
                    structural = (s.timeout_s > 0) != (float(data) > 0)
                    s.timeout_s = float(data)
                elif name == 'timeout_goto':
                    s.timeout_goto = int(data)
            elif len(path) == 5 and path[2] == 'until' and path[3].startswith('c'):
                k = int(path[3][1:])
                c = s.until[k]
                name = path[4]
                if name == 'kind':
                    c.kind, c.target = data, ''
                    if c.kind == 'locked' and c.hold_s == 0:
                        c.hold_s = 3.0
                    structural = True
                elif name in ('target', 'op', 'then'):
                    setattr(c, name, data or '')
                    structural = name == 'then'
                elif name in ('value', 'hold_s'):
                    setattr(c, name, float(data))
                elif name == 'goto':
                    c.goto = int(data)
                if not structural:
                    self.settings.child('steps', f's{i}', 'until', f'c{k}').setOpts(
                        title=f'{k + 1} · {describe_condition(c)}')
            if not structural:
                self.settings.child('steps', f's{i}').setOpts(title=step_title(i, s))
        self._show_problems()
        item = self.list.item(self.routines.index(r))
        if item is not None:
            item.setText(self._item_text(r))
        self._dirty()
        if structural:
            self._schedule_rebuild()

    def _show_problems(self):
        r = self._current
        errs = validate_routine(r) if r is not None else []
        self.problems.setText('\n'.join(f'⚠ {e}' for e in errs[:8]) + (f'\n⚠ … ({len(errs) - 8} more)'
                                                                        if len(errs) > 8 else ''))
        self.problems.setVisible(bool(errs))

    # ------------------------------------------------------------------ steps
    def _selected_step(self) -> int:
        item = self.tree.currentItem()
        p = getattr(item, 'param', None)
        while p is not None:
            parent = p.parent()
            if parent is not None and parent.name() == 'steps' and p.name().startswith('s') and p.name()[1:].isdigit():
                return int(p.name()[1:])
            p = parent
        return -1

    def select_step(self, i: int):
        """Select (and expand) step ``i`` in the tree."""
        try:
            p = self.settings.child('steps', f's{i}')
        except Exception:
            return
        p.setOpts(expanded=True)
        for item in p.items:
            self.tree.setCurrentItem(item)

    def _edit(self, fn, select: int):
        if self._current is None or self._running():
            return
        fn()
        self._dirty()
        self._build_tree(expand_step=select, force=True)
        self.select_step(select)

    def _on_add_step(self):
        r = self._current
        at = len(r.steps)
        self._edit(lambda: insert_step(r, at, Step('none', until=[Condition('delay', value=1.0)])), at)

    def _on_add_condition(self, i: int):
        r = self._current
        self._edit(lambda: r.steps[i].until.append(Condition('delay', value=1.0)), i)

    def _on_remove_condition(self, i: int, k: int):
        r = self._current
        self._edit(lambda: r.steps[i].until.pop(k), i)

    def _on_copy_step(self):
        i = self._selected_step()
        if i >= 0:
            self._edit(lambda: duplicate_step(self._current, i), i + 1)

    def _on_move_step(self, delta: int):
        r = self._current
        i = self._selected_step()
        if r is not None and i >= 0 and 0 <= i + delta < len(r.steps):
            self._edit(lambda: move_step(r, i, i + delta), i + delta)

    def _on_delete_step(self):
        r = self._current
        i = self._selected_step()
        if r is not None and i >= 0:
            self._edit(lambda: delete_step(r, i), max(i - 1, 0))

    def _set_locked(self, locked: bool):
        """Running: nothing editable (values read-only, actions disabled)."""
        if self._locked == locked or self._settings is None or self._tree_dirty:
            return
        self._locked = locked

        def walk(p):
            for c in p.children():
                if c.type() == 'action':
                    c.setOpts(enabled=not locked)
                elif c.type() != 'group':
                    c.setOpts(readonly=locked)
                walk(c)
        walk(self.settings)
        for b in (self.btn_up, self.btn_down, self.btn_copy, self.btn_del, self.btn_remove):
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

    # ------------------------------------------------------------------ preset
    def load_from_preset(self, preset: dict):
        self.routines = [Routine.from_dict(d) for d in preset.get('routines', []) or []]
        self._current = self.routines[0] if self.routines else None
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
        self.name.setText(r.name() if r is not None else 'No routine')
        self.badge.set_state(state, a.reason if a is not None else '')
        self.btn_run.setEnabled(r is not None and not engaged and not self.app.core.estop)
        self.btn_stop.setEnabled(engaged)
        self.btn_resume.setEnabled(state == AutoState.HELD)
        self._set_locked(engaged)
        if a is None:
            self.status.setText('Not run yet.' if r is not None else '')
        elif engaged:
            self.status.setText(f'▶ Step {a.index + 1}/{len(a.routine.steps)}: {a.status}')
        else:
            outcome = {'done': '✔ Finished', 'failed': '✖ FAILED', 'stopped': '■ Stopped'}.get(a.outcome, 'Stopped')
            self.status.setText(f'{outcome} — {a.reason or a.status}')
        mark = (a.index, engaged) if a is not None else ()
        if mark != self._marked and r is not None and self._settings is not None and not self._tree_dirty:
            self._marked = mark
            for i, s in enumerate(r.steps):
                try:
                    p = self.settings.child('steps', f's{i}')
                except Exception:
                    continue
                title = step_title(i, s, current=engaged and a is not None and i == a.index)
                if p.opts.get('title') != title:
                    p.setOpts(title=title)
        if a is not None:
            hist = tuple(a.history)
            if hist != self._hist_seen:
                new = hist[len(self._hist_seen):] if hist[:len(self._hist_seen)] == self._hist_seen else hist
                if new is hist:
                    self.history.clear()
                for t, text in new:
                    self.history.appendPlainText(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {text}")
                self._hist_seen = hist
