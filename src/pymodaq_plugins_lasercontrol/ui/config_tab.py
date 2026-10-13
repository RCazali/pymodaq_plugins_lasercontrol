"""Configuration tab (T5.7): one parameter tree for what is set once per machine or per preset.

* **Motors / Signals**: the roles of this machine (T1.2). They are created automatically from the motor
  modules of the dashboard and the received 0D channels (``LaserControlApp.ensure_roles``): here they are only
  renamed (the presets use these names) and documented. A change is applied and saved at once.
* per motor: its safety limits (T3.4, saved in the preset). Max step, max excursion and max rate are required
  to drive a motor automatically (0 = not set); the absolute limits are optional (off = no limit).
* **Safety**: data age, move timeout, intensity guard (preset).
* **Advanced**: frame counters (O9-A), operator, folders.

On the right, what is received live (detectors, 0D channels), to check a new machine.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from qtpy import QtCore, QtGui, QtWidgets
from pymodaq_gui.parameter import Parameter, ParameterTree

from ..core.events import EventKind
from ..core.roles import RoleMap, sanitize_role_name
from ..core.safety import ActuatorLimits, IntensityGuardConfig, SafetyConfig
from ..core.states import ENGAGED_STATES
from .widgets import fmt, styled_button

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

ROLE_FIELDS = ('name', 'units', 'note')
REQUIRED_TIP = ' 0 = not set: the motor cannot be driven by a PID, an optimization or a routine.'


def _copy(safety: SafetyConfig) -> SafetyConfig:
    return SafetyConfig.from_dict(safety.to_dict())


def motor_title(role: str, module: str, present: bool, lim: Optional[ActuatorLimits]) -> str:
    if not present:
        return f'⚠ {role}   ←  {module}   (not in the dashboard)'
    if lim is None or lim.missing():
        return f'⚠ {role}   ←  {module}   (limits not set)'
    return f'✔ {role}   ←  {module}'


def motor_children(role, lim: ActuatorLimits, present: bool) -> list:
    return [
        {'title': 'Name:', 'name': 'name', 'type': 'str', 'value': role.name,
         'tip': 'Name used by the presets (PID, optimizations, axes…): give the same name on every machine'},
        {'title': 'Units:', 'name': 'units', 'type': 'str', 'value': role.units},
        {'title': 'Note:', 'name': 'note', 'type': 'str', 'value': role.note,
         'tip': 'Direction, purpose… e.g. "+ = beam to the right on Cam1"'},
        {'title': 'Max step:', 'name': 'max_step', 'type': 'float', 'value': lim.max_step or 0.0, 'min': 0.0,
         'decimals': 6, 'tip': 'Largest step of one automatic order (S3).' + REQUIRED_TIP},
        {'title': 'Max excursion (±):', 'name': 'max_excursion', 'type': 'float', 'value': lim.max_excursion or 0.0,
         'min': 0.0, 'decimals': 6,
         'tip': 'Around the position at the start of the automation (S4).' + REQUIRED_TIP},
        {'title': 'Max travel per window:', 'name': 'rate_max', 'type': 'float', 'value': lim.rate_max or 0.0,
         'min': 0.0, 'decimals': 6,
         'tip': 'Largest cumulated displacement over the window, 60 s by default (S5).' + REQUIRED_TIP},
        {'title': 'Lower limit:', 'name': 'has_min', 'type': 'bool', 'value': lim.abs_min is not None,
         'tip': 'Off = no lower limit. On: absolute limit, also for the manual moves'},
        {'title': 'Abs min:', 'name': 'abs_min', 'type': 'float', 'value': lim.abs_min or 0.0, 'decimals': 6,
         'visible': lim.abs_min is not None},
        {'title': 'Upper limit:', 'name': 'has_max', 'type': 'bool', 'value': lim.abs_max is not None,
         'tip': 'Off = no upper limit. On: absolute limit, also for the manual moves'},
        {'title': 'Abs max:', 'name': 'abs_max', 'type': 'float', 'value': lim.abs_max or 0.0, 'decimals': 6,
         'visible': lim.abs_max is not None},
        {'title': 'Advanced', 'name': 'more', 'type': 'group', 'expanded': False, 'children': [
            {'title': 'Window (s):', 'name': 'rate_window_s', 'type': 'float', 'value': lim.rate_window_s,
             'min': 1.0, 'tip': 'Sliding window of "Max travel per window"'},
            {'title': 'Resolution:', 'name': 'resolution', 'type': 'float', 'value': lim.resolution, 'min': 0.0,
             'decimals': 6, 'tip': 'Smallest meaningful step (e.g. 1 picomotor step). 0 = none'},
            {'title': '🗑 Remove this role', 'name': 'remove', 'type': 'action', 'visible': not present,
             'tip': 'Its module is not in the dashboard anymore'}]}]


def signal_children(role, present: bool) -> list:
    return [
        {'title': 'Name:', 'name': 'name', 'type': 'str', 'value': role.name,
         'tip': 'Name used by the presets: give the same name on every machine'},
        {'title': 'Units:', 'name': 'units', 'type': 'str', 'value': role.units},
        {'title': 'Note:', 'name': 'note', 'type': 'str', 'value': role.note},
        {'title': '🗑 Remove this role', 'name': 'remove', 'type': 'action', 'visible': not present,
         'tip': 'Its channel is not received (it is created again if the channel comes back)'}]


class ConfigTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self._loading = False
        self._stale = True                        # the tree is built when the tab is shown
        self._roles_pending = False
        self._safety = SafetyConfig()             # the model: the tree only shows / edits it
        self._motors: List[str] = []              # role of the group m<i>
        self._signals: List[str] = []             # role of the group s<i>
        self._detectors: List[str] = []           # detector of the parameter hb<i>
        self._expanded: Dict[Tuple[str, str], bool] = {}
        self.params: Optional[Parameter] = None
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        hint = QtWidgets.QLabel('Motors and signals are found automatically: <b>rename</b> them (the presets use these '
                                'names) and set the limits of the motors. <b>⚠</b> = limits not set, the motor '
                                'cannot be driven automatically. Changes are applied at once.')
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self.msg = QtWidgets.QLabel('')
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet('color: #e74c3c; font-weight: bold;')
        self.msg.hide()
        outer.addWidget(self.msg)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(split, 1)
        self.tree = ParameterTree()
        split.addWidget(self.tree)
        split.addWidget(self._build_live())
        self.tree.setMinimumWidth(420)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([750, 500])

    # ================================================================== tree
    def _tree(self) -> list:
        core = self.app.core
        roles = core.roles
        modules, channels = set(core.broker.modules()), set(core.signals.channels())
        self._motors = sorted(roles.actuators)
        self._signals = sorted(roles.signals)
        motors = []
        for i, name in enumerate(self._motors):
            r = roles.actuators[name]
            lim = self._safety.actuators.get(name)
            present = r.module in modules
            motors.append({'title': motor_title(name, r.module, present, lim), 'name': f'm{i}', 'type': 'group',
                           'expanded': self._expanded.get(('m', r.module), False),
                           'children': motor_children(r, lim or ActuatorLimits(), present)})
        signals = []
        for i, name in enumerate(self._signals):
            r = roles.signals[name]
            present = r.channel in channels
            signals.append({'title': f"{name}   ←  {r.channel}{'' if present else '   (not received)'}",
                            'name': f's{i}', 'type': 'group', 'expanded': self._expanded.get(('s', r.channel), False),
                            'children': signal_children(r, present)})
        g = self._safety.intensity
        sig_names = [''] + self._signals + ([g.role] if g.role and g.role not in self._signals else [])
        self._detectors = sorted(set(core.broker.detectors) | set(roles.heartbeats) | set(core.signals.detectors()))
        heartbeats = []
        for i, d in enumerate(self._detectors):
            cur = roles.heartbeats.get(d, '')
            chans = [ch for ch in sorted(channels) if core.signals.detector_of(ch) == d]
            heartbeats.append({'title': f'{d}:', 'name': f'hb{i}', 'type': 'list', 'value': cur,
                               'limits': [''] + chans + ([cur] if cur and cur not in chans else []),
                               'tip': 'Frame counter channel of this detector: the data are stale when it stops '
                                      'advancing. Empty: the freshness relies on the reception time only'})
        p = self.app.paths
        info = [('Version:', f'{self.app.version}  (git {self.app.git_hash})'), ('Machine:', self.app.machine),
                ('Roles file:', p.roles_file), ('Presets:', p.presets), ('Recordings:', p.recordings),
                ('Technical log:', p.logs), ('Configuration file:', self.app.config_file_path())]
        return [
            {'title': f'Motors ({len(motors)})', 'name': 'motors', 'type': 'group', 'children': motors,
             'expanded': self._expanded.get(('top', 'motors'), True)},
            {'title': f'Signals ({len(signals)})', 'name': 'signals', 'type': 'group', 'children': signals,
             'expanded': self._expanded.get(('top', 'signals'), False)},
            {'title': 'Safety (preset)', 'name': 'safety', 'type': 'group',
             'expanded': self._expanded.get(('top', 'safety'), True), 'children': [
                {'title': 'Max data age (s):', 'name': 'max_data_age_s', 'type': 'float',
                 'value': self._safety.max_data_age_s, 'min': 0.05,
                 'tip': 'Beyond it, the data are stale and the automations held (S1)'},
                {'title': 'Move timeout (s):', 'name': 'move_timeout_s', 'type': 'float',
                 'value': self._safety.move_timeout_s, 'min': 0.5,
                 'tip': 'A longer automatic move holds the automation'},
                {'title': 'Abs limits for manual moves:', 'name': 'manual_abs_limits', 'type': 'bool',
                 'value': self._safety.manual_abs_limits,
                 'tip': 'The absolute limits of the motors also bound the keyboard / Go moves'},
                {'title': 'Intensity guard', 'name': 'intensity', 'type': 'group', 'children': [
                    {'title': 'Enabled:', 'name': 'enabled', 'type': 'bool', 'value': g.enabled,
                     'tip': 'Holds the automations when the intensity drops below the threshold (S2)'},
                    {'title': 'Signal:', 'name': 'role', 'type': 'list', 'limits': sig_names, 'value': g.role or ''},
                    {'title': 'Mode:', 'name': 'mode', 'type': 'list', 'limits': ['relative', 'absolute'],
                     'value': g.mode},
                    {'title': 'Threshold:', 'name': 'threshold', 'type': 'float', 'value': g.threshold, 'min': 0.0,
                     'decimals': 6, 'tip': 'relative: fraction of the reference; absolute: value'},
                    {'title': 'Hysteresis:', 'name': 'hysteresis', 'type': 'float', 'value': g.hysteresis,
                     'min': 0.0, 'decimals': 6, 'tip': 'Added to the threshold to consider the intensity back'},
                    {'title': 'Fixed reference:', 'name': 'fixed', 'type': 'bool', 'value': g.reference is not None,
                     'tip': 'Off: the reference is taken at the first start of an automation'},
                    {'title': 'Reference:', 'name': 'reference', 'type': 'float', 'decimals': 6,
                     'value': g.reference if g.reference is not None else 1.0, 'visible': g.reference is not None},
                    {'title': '📍 Capture reference', 'name': 'capture', 'type': 'action',
                     'tip': 'Reference = current value (relative mode, non-fixed reference)'},
                    {'title': 'Now:', 'name': 'state', 'type': 'str', 'value': '', 'readonly': True}]}]},
            {'title': 'Advanced', 'name': 'advanced', 'type': 'group',
             'expanded': self._expanded.get(('top', 'advanced'), False), 'children': [
                {'title': 'Frame counters', 'name': 'heartbeats', 'type': 'group', 'children': heartbeats},
                {'title': 'Operator:', 'name': 'operator', 'type': 'str', 'value': self.app.operator},
                {'title': 'Files (set in config_lasercontrol.toml)', 'name': 'files', 'type': 'group',
                 'expanded': False, 'children': [
                    {'title': t, 'name': f'f{i}', 'type': 'str', 'value': str(v), 'readonly': True}
                    for i, (t, v) in enumerate(info)] + [
                    {'title': '📂 Open the data folder', 'name': 'open', 'type': 'action'}]}]}]

    def _rebuild(self):
        if not self.isVisible():
            self._stale = True
            return
        self._remember_expanded()
        self._loading = True
        try:
            self.params = Parameter.create(name='config', type='group', children=self._tree())
            self.tree.setParameters(self.params, showTop=False)
            self.params.sigTreeStateChanged.connect(self._on_tree_changed)
            for i in range(len(self._motors)):
                self.params.child('motors', f'm{i}', 'more', 'remove').sigActivated.connect(
                    lambda _=None, i=i: self._remove_role(self._motors[i]))
            for i in range(len(self._signals)):
                self.params.child('signals', f's{i}', 'remove').sigActivated.connect(
                    lambda _=None, i=i: self._remove_role(self._signals[i]))
            self.params.child('safety', 'intensity', 'capture').sigActivated.connect(self._capture_reference)
            self.params.child('advanced', 'files', 'open').sigActivated.connect(self._open_data_folder)
        finally:
            self._loading = False
        self._stale = False
        self._update_guard_info()

    def _remember_expanded(self):
        if self.params is None:
            return
        roles = self.app.core.roles
        for key in ('motors', 'signals', 'safety', 'advanced'):
            self._expanded[('top', key)] = bool(self.params.child(key).opts.get('expanded', True))
        for i, name in enumerate(self._motors):
            if name in roles.actuators:
                self._expanded[('m', roles.actuators[name].module)] = bool(
                    self.params.child('motors', f'm{i}').opts.get('expanded', False))
        for i, name in enumerate(self._signals):
            if name in roles.signals:
                self._expanded[('s', roles.signals[name].channel)] = bool(
                    self.params.child('signals', f's{i}').opts.get('expanded', False))

    def showEvent(self, e):
        super().showEvent(e)
        if self._stale:
            self._rebuild()

    # ================================================================== edits
    def _on_tree_changed(self, _param, changes):
        if self._loading or self.params is None:
            return
        roles_changed = safety_changed = False
        for p, change, _data in changes:
            if change != 'value':
                continue
            path = self.params.childPath(p) or []
            if not path or path[-1] == 'state':
                continue
            if path == ['advanced', 'operator']:
                self.app.set_operator(str(p.value()).strip())
            elif (path[0] in ('motors', 'signals') and path[-1] in ROLE_FIELDS) or path[:2] == ['advanced',
                                                                                                'heartbeats']:
                roles_changed = True
            elif path[0] in ('motors', 'safety'):
                safety_changed = True
        if safety_changed:
            self._safety = self._read_safety()
            self._show_safety_state()
            self.app.apply_safety(self._safety)
            self._update_guard_info()
        if roles_changed and not self._roles_pending:      # the tree is rebuilt: not inside its own signal
            self._roles_pending = True
            QtCore.QTimer.singleShot(0, self._apply_roles)

    def _show_safety_state(self):
        """Titles (⚠ / ✔) and optional fields after an edit of the limits."""
        roles, modules = self.app.core.roles, set(self.app.core.broker.modules())
        self._loading = True
        try:
            for i, name in enumerate(self._motors):
                g = self.params.child('motors', f'm{i}')
                g.child('abs_min').setOpts(visible=g.child('has_min').value())
                g.child('abs_max').setOpts(visible=g.child('has_max').value())
                r = roles.actuators.get(name)
                if r is not None:
                    g.setOpts(title=motor_title(name, r.module, r.module in modules, self._safety.actuators.get(name)))
            inten = self.params.child('safety', 'intensity')
            inten.child('reference').setOpts(visible=inten.child('fixed').value())
        finally:
            self._loading = False

    def _read_safety(self) -> SafetyConfig:
        s = self.params.child('safety')
        inten = s.child('intensity')
        g = IntensityGuardConfig(enabled=inten.child('enabled').value(), role=inten.child('role').value() or None,
                                 mode=inten.child('mode').value(), threshold=inten.child('threshold').value(),
                                 hysteresis=inten.child('hysteresis').value(),
                                 reference=inten.child('reference').value() if inten.child('fixed').value() else None)
        acts = dict(self._safety.actuators)          # limits of roles unknown on this machine are kept
        for i, name in enumerate(self._motors):
            m = self.params.child('motors', f'm{i}')
            lim = ActuatorLimits(max_step=m.child('max_step').value() or None,
                                 max_excursion=m.child('max_excursion').value() or None,
                                 abs_min=m.child('abs_min').value() if m.child('has_min').value() else None,
                                 abs_max=m.child('abs_max').value() if m.child('has_max').value() else None,
                                 rate_window_s=m.child('more', 'rate_window_s').value(),
                                 rate_max=m.child('rate_max').value() or None,
                                 resolution=m.child('more', 'resolution').value())
            if all(v is None for v in (lim.max_step, lim.max_excursion, lim.abs_min, lim.abs_max, lim.rate_max)):
                acts.pop(name, None)
            else:
                acts[name] = lim
        return SafetyConfig(max_data_age_s=s.child('max_data_age_s').value(),
                            move_timeout_s=s.child('move_timeout_s').value(),
                            manual_abs_limits=s.child('manual_abs_limits').value(), intensity=g, actuators=acts)

    # ------------------------------------------------------------------ roles
    def _warn(self, text: str = ''):
        self.msg.setText(f'⚠ {text}' if text else '')
        self.msg.setVisible(bool(text))

    def _engaged(self) -> List[str]:
        return [a.label for a in self.app.core.automations.values() if a.state in ENGAGED_STATES]

    def _apply_roles(self):
        """Names, units, notes and frame counters of the tree → roles of the machine (applied and saved).
        The limits of a renamed motor follow it."""
        self._roles_pending = False
        if self.params is None:
            return
        old = self.app.core.roles
        rm = RoleMap(machine=self.app.machine, description=old.description, path=old.path)
        renamed, errors = {}, []
        for kind, names, source in (('motors', self._motors, old.actuators), ('signals', self._signals, old.signals)):
            for i, name in enumerate(names):
                g = self.params.child(kind, f"{'m' if kind == 'motors' else 's'}{i}")
                r = source.get(name)
                if r is None:
                    continue
                new = sanitize_role_name(g.child('name').value()) if str(g.child('name').value()).strip() else name
                if new in rm.all_roles():
                    errors.append(f'two roles named "{new}"')
                    new = name
                if new != name:
                    renamed[name] = new
                units, note = str(g.child('units').value()).strip(), str(g.child('note').value()).strip()
                if kind == 'motors':
                    rm.set_actuator(new, r.module, units, note)
                else:
                    rm.set_signal(new, r.channel, units, note)
        for i, d in enumerate(self._detectors):
            ch = self.params.child('advanced', 'heartbeats', f'hb{i}').value()
            if ch:
                rm.heartbeats[d] = ch
        if renamed and self._engaged():
            errors.append('stop the automations before renaming a role (' + ', '.join(self._engaged()) + ')')
        if errors:
            self._warn('Not applied: ' + ' ; '.join(errors))
            self._rebuild()                          # back to the roles in use
            return
        self._warn()
        if rm.to_dict() == old.to_dict():
            return
        if renamed:
            safety = _copy(self._safety)
            for a, b in renamed.items():
                if a in safety.actuators and b not in safety.actuators:
                    safety.actuators[b] = safety.actuators.pop(a)
                if safety.intensity.role == a:
                    safety.intensity.role = b
            self.app.apply_safety(safety)
            self.app.core.events.publish(EventKind.INFO, 'UI', 'Roles renamed: ' +
                                         ', '.join(f'{a} → {b}' for a, b in renamed.items()))
        self.app.apply_roles(rm, save=True)          # reloads this tab (set_hardware)

    def _remove_role(self, name: str):
        if self._engaged():
            self._warn('Stop the automations before removing a role (' + ', '.join(self._engaged()) + ')')
            return
        self._warn()
        rm = self.app.core.roles.copy()
        rm.remove(name)
        self.app.apply_roles(rm, save=True)

    def _open_data_folder(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.app.paths.data)))

    # ================================================================== safety (preset)
    def load_safety(self, safety: SafetyConfig):
        self._safety = _copy(safety)
        self._rebuild()

    def safety_config(self) -> SafetyConfig:
        return _copy(self._safety)

    def _on_safety_changed(self, *args):
        self.app.apply_safety(self.safety_config())
        self._update_guard_info()

    def _capture_reference(self):
        if self.app.core.safety.guard.capture_reference():
            self._warn()
            self.app.core.events.publish('safety.info', 'SAFETY',
                                         f"Intensity reference captured: {self.app.core.safety.guard.reference:.4g}")
        else:
            self._warn('Capture impossible (absolute mode, signal missing, stale or zero).')
        self._update_guard_info()

    def _update_guard_info(self):
        if self.params is None or self._stale:
            return
        g = self.app.core.safety.guard
        trip, release = g.thresholds()
        text = (f'{g.evaluate()} · value {fmt(g.value())} · reference {fmt(g.reference)} · '
                f'threshold {fmt(trip)} · release {fmt(release)}')
        p = self.params.child('safety', 'intensity', 'state')
        if p.value() != text:
            self._loading = True
            try:
                p.setValue(text)
            finally:
                self._loading = False

    def write_to_preset(self, preset: dict):
        preset['safety'] = self._safety.to_dict()

    # ================================================================== live
    def _build_live(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.addWidget(QtWidgets.QLabel('<b>Detectors</b>'))
        self.det_table = QtWidgets.QTableWidget(0, 4)
        self.det_table.setHorizontalHeaderLabels(['Detector', 'Acquisition', 'Freshness', 'Rate'])
        self.det_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.det_table.verticalHeader().setVisible(False)
        self.det_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.det_table.setMaximumHeight(120)
        lay.addWidget(self.det_table)
        row = QtWidgets.QHBoxLayout()
        b_start = styled_button('▶ Start acquisition', '#2980b9', 'The selected detectors (none: those of the roles)')
        b_stop = styled_button('⏹ Stop', None, 'The selected detectors')
        b_start.clicked.connect(self._start_grab)
        b_stop.clicked.connect(lambda: self.app.core.broker.stop_grab(self._selected_dets()))
        row.addWidget(b_start)
        row.addWidget(b_stop)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(QtWidgets.QLabel('<b>Received 0D channels</b>'))
        self.chan_table = QtWidgets.QTableWidget(0, 4)
        self.chan_table.setHorizontalHeaderLabels(['Channel', 'Signal', 'Value', 'Age (s)'])
        self.chan_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.chan_table.verticalHeader().setVisible(False)
        self.chan_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.chan_table, 1)
        return w

    def _selected_dets(self) -> List[str]:
        rows = {i.row() for i in self.det_table.selectedIndexes()}
        return [self.det_table.item(r, 0).text() for r in sorted(rows)]

    def _start_grab(self):
        dets = self._selected_dets()
        if dets:
            self.app.core.broker.start_grab(dets)
        else:
            self.app.start_detectors()

    def refresh_detectors(self):
        core = self.app.core
        dets = sorted(core.broker.detectors)
        if self.det_table.rowCount() != len(dets):
            self.det_table.setRowCount(len(dets))
        for i, d in enumerate(dets):
            st = core.signals.detector_status(d)
            if not core.broker.detectors[d].is_initialized():
                grab = 'not initialized'
            else:
                grab = 'running' if core.broker.is_grabbing(d) else 'stopped'
            vals = [d, grab, f'{st.state} {st.reason}', f'{st.rate_hz:.2f} Hz' if st.rate_hz else '—']
            for j, v in enumerate(vals):
                it = self.det_table.item(i, j)
                if it is None:
                    it = QtWidgets.QTableWidgetItem()
                    self.det_table.setItem(i, j, it)
                it.setText(v)
        chans = core.signals.channels()
        if self.chan_table.rowCount() != len(chans):
            self.chan_table.setRowCount(len(chans))
        for i, ch in enumerate(chans):
            role = core.roles.role_for_channel(ch) or ''
            vals = [ch, role, fmt(core.signals.value(ch), 6), f'{core.signals.age(ch):.1f}']
            for j, v in enumerate(vals):
                it = self.chan_table.item(i, j)
                if it is None:
                    it = QtWidgets.QTableWidgetItem()
                    self.chan_table.setItem(i, j, it)
                it.setText(v)

    # ================================================================== preset / refresh
    def load_from_preset(self, preset: dict):
        self.load_safety(SafetyConfig.from_dict(preset.get('safety', {})))

    def set_hardware(self):
        self.load_safety(self.app.core.safety.config)     # the core is the reference

    def refresh(self):
        if self._stale:
            self._rebuild()
        self.refresh_detectors()
        self._update_guard_info()
