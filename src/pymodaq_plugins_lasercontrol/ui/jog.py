"""Jog window of Laser Control: the Keyboard Jog interface (``pymodaq_plugins_keyboardjog``) on the broker.

Keyboard Jog brings the window (green / red border, numbered groups and pairs, W A S D, + / -, step or
continuous mode, history, saved positions); Laser Control brings everything that protects the laser:

* every move goes through :class:`ManualControl`, so through the broker: a key moving a motor of an engaged
  automation **stops that automation first** (D5/S7), the abs limits of the roles apply, nothing moves while the
  EMERGENCY STOP is latched;
* the actuators are the **roles** (machine independent, like the rest of the preset) and the PID setpoints
  ``SP:<pid>`` (hidden until shown from the settings: moving a setpoint steers the beam with the PID running);
* the configuration is saved in the Laser Control preset (``preset['jog']``); a setup made in the Keyboard Jog of
  the Dashboard can be imported (its modules matched to the roles), and this configuration exported as one;
* the automation buttons ("Button in Alignment") and E-STOP / Re-arm are on top of the window;
* per role, **Invert keys** (right-click menu) swaps the - / + keys, as the old Alignment axes did.

A key pressed while an automation is moving the motor (a PID step in progress) stops the automation but sends
nothing: the step in progress finishes, the next key press moves the motor (never two orders stacked on a motor).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List, Optional

from qtpy import QtCore, QtGui, QtWidgets

from pymodaq_gui.utils.dock import DockArea

from pymodaq_plugins_keyboardjog.core.model import HIDDEN, HORIZONTAL, VERTICAL, ExtraKeys, JogSetup
from pymodaq_plugins_keyboardjog.core.ports import ActuatorPort
from pymodaq_plugins_keyboardjog.core.setups import (SetupStore, clean_name, default_folder, setup_from_dict,
                                                     setup_to_dict)
from pymodaq_plugins_keyboardjog.extensions.keyboard_jog import KeyboardJog, plugin_config as jog_plugin_config
from pymodaq_plugins_keyboardjog.ui import settings as jog_settings

from ..core.events import EventKind, Level
from ..core.log import DedupLogger, module_logger
from ..core.ownership import HUMAN
from .manual import is_virtual

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp
    from .manual import ManualControl

logger = DedupLogger(module_logger(__file__))

SOURCE = 'jog'
WINDOW_TITLE = 'Laser Control - Jog'
MESSAGE_KINDS = (EventKind.PREEMPTION, EventKind.MOVE_MANUAL, EventKind.ESTOP, EventKind.ESTOP_RESET)


class LaserJogPort(ActuatorPort):
    """A role (or a PID setpoint) of Laser Control, moved through ManualControl (the broker)"""

    def __init__(self, manual: 'ManualControl', role: str, inverted: bool = False):
        self.manual = manual
        self.name = role
        self.inverted = inverted
        self.on_value = None
        self.on_done = None

    @property
    def broker(self):
        return self.manual.core.broker

    def _module(self) -> Optional[str]:
        return self.manual.module(self.name)

    def units(self) -> str:
        return self.manual.units(self.name) or ''

    def is_initialized(self) -> bool:
        if is_virtual(self.name):
            return self.manual.exists(self.name)
        return self.broker.is_initialized(self._module())

    def _automation_move(self) -> bool:
        """A move ordered by an automation is running on the motor"""
        st = self.broker.state(self._module()) if not is_virtual(self.name) else None
        return bool(st is not None and st.busy and not str(st.mover or '').startswith(HUMAN))

    def is_moving(self) -> bool:
        """Moved by something else than this window: a human source (another widget) or an automation. The
        automation case is not 'moving' here: move_rel stops the automation (D5/S7)"""
        if is_virtual(self.name):
            return self.manual.is_busy(self.name)
        return self.manual.is_busy(self.name) and not self._automation_move()

    def move_rel(self, delta: float) -> bool:
        if self._automation_move():
            # D5/S7: the key stops the automation; its step in progress finishes, nothing is stacked on it
            self.manual.core.human_preempt(self._module(), SOURCE)
            return False
        if self.inverted:
            delta = -delta
        return bool(self.manual.move_rel(self.name, float(delta), SOURCE))

    def move_abs(self, value: float) -> Optional[bool]:
        position = self.manual.position(self.name)
        if position is not None and abs(float(value) - position) <= 1e-12 * max(1.0, abs(position)):
            return False                       # already there: no move, no move-done to wait for
        if self._automation_move():
            self.manual.core.human_preempt(self._module(), SOURCE)
            return False
        return bool(self.manual.move_abs(self.name, float(value), SOURCE))

    def stop(self):
        self.manual.stop(self.name, SOURCE)

    def request_value(self):
        self.manual.read_positions([self.name])


def setup_from_alignment(alignment: Optional[dict]) -> (JogSetup, List[str]):
    """Starting point from the axes of the old Alignment tab: one numbered group per axis (its number = its old
    key), horizontal / vertical channels, step = the 'slow' step. Returns the setup and the inverted roles."""
    setup = JogSetup()
    inverted = []
    used = set()
    for i, ax in enumerate((alignment or {}).get('axes') or []):
        if i >= 10 or not isinstance(ax, dict):
            break
        number = i + 1
        roles = []
        for key, axis in (('h', HORIZONTAL), ('v', VERTICAL)):
            ch = ax.get(key)
            role = ch.get('actuator') if isinstance(ch, dict) else None
            if not role or role == 'None' or role in used:
                continue
            used.add(role)
            roles.append(role)
            setup.pair_numbers[role] = number
            setup.axes[role] = axis
            try:
                step = abs(float(ch.get('slow', 0) or 0))
            except (TypeError, ValueError):
                step = 0
            if step > 0:
                setup.steps[role] = step
            if ch.get('invert'):
                inverted.append(role)
        if len(roles) == 2 and str(ax.get('name', '')).strip():
            setup.pair_names[number] = str(ax['name']).strip()
    return setup, inverted


def rename_actuators(setup: JogSetup, mapping: Dict[str, str]) -> JogSetup:
    """Copy of a setup with its actuators renamed (Dashboard module <-> role); names absent from the mapping are
    kept (they are skipped with a warning when the setup is loaded)"""
    m = lambda name: mapping.get(name, name)  # noqa: E731
    changes = {'hidden': [m(a) for a in setup.hidden], 'headless': [m(a) for a in setup.headless],
               'continuous': [m(a) for a in setup.continuous],
               'steps': {m(k): v for k, v in setup.steps.items()},
               'pair_numbers': {m(k): v for k, v in setup.pair_numbers.items()},
               'axes': {m(k): v for k, v in setup.axes.items()},
               'pair_names': dict(setup.pair_names),
               'extra_keys': [ExtraKeys(m(e.actuator) if e.actuator else '', e.key_minus, e.key_plus)
                              for e in setup.extra_keys]}
    if hasattr(setup, 'actuator_names'):      # Keyboard Jog >= 0.3
        changes['actuator_names'] = {m(k): v for k, v in setup.actuator_names.items()}
    return JogSetup(**{**setup.__dict__, **changes})


def setup_params() -> dict:
    """Setup group of the jog settings (the one of Keyboard Jog is hidden: the configuration is in the preset)"""
    return {'title': 'Setup', 'name': 'lc_setup', 'type': 'group', 'children': [
        {'title': 'Preset:', 'name': 'preset', 'type': 'str', 'value': '', 'readonly': True,
         'tip': 'The jog configuration is saved with the Laser Control preset (* = changes not saved)'},
        {'title': 'Save preset', 'name': 'save_preset', 'type': 'action',
         'tip': 'Save the Laser Control preset, with this jog configuration (the Save button of the Presets tab)'},
        {'title': 'Keyboard Jog setups:', 'name': 'jog_setups', 'type': 'list', 'limits': [''], 'value': '',
         'tip': 'Setups saved by the Keyboard Jog extension of the Dashboard'},
        {'title': 'Import', 'name': 'import', 'type': 'action',
         'tip': 'Use the Keyboard Jog setup selected above (its Dashboard modules are matched to the roles), then '
                'save the preset to keep it'},
        {'title': 'Export as:', 'name': 'export_name', 'type': 'str', 'value': ''},
        {'title': 'Export', 'name': 'export', 'type': 'action',
         'tip': 'Save this configuration as a Keyboard Jog setup, usable by the Keyboard Jog of the Dashboard'},
    ]}


class LaserJog(KeyboardJog):
    """Keyboard Jog in its own window, driven by Laser Control (see the module docstring)"""

    def __init__(self, app: 'LaserControlApp'):
        self.app = app
        self.manual = app.manual
        self.inverted: set = set()
        self._configured: set = set()       # actuators the loaded configuration talks about
        self._applying = False
        self._disposed = False
        window = QtWidgets.QMainWindow()
        window.setWindowTitle(WINDOW_TITLE)
        dockarea = DockArea()
        window.setCentralWidget(dockarea)
        super().__init__(dockarea, None)
        self.window = window
        self.settings.child('setup').setOpts(visible=False)      # saved with the Laser Control preset
        self.jog_store = SetupStore(default_folder(jog_plugin_config))   # setups of the Dashboard's Keyboard Jog
        self.settings.insertChild(0, setup_params())
        for name, slot in (('save_preset', self.save_preset), ('import', self._import_selected),
                           ('export', self._export)):
            self.settings.child('lc_setup', name).sigActivated.connect(lambda *_, s=slot: s())
        self.panel.settings_toggled.connect(lambda shown: shown and self._refresh_setup_group())
        self._refresh_setup_group()
        self.panel.close_button.setToolTip('Hide the jog window (Laser Control toolbar: show it again)')
        from .quick_panel import QuickPanel
        self.quick = QuickPanel(app, compact=True)
        self.panel.set_top_widget(self.quick)
        self.quick.height_changed.connect(lambda: QtCore.QTimer.singleShot(0, self._fit_window))
        self.manual.add_listener(self._on_manual_position)
        self._event_token = app.core.events.subscribe(self._on_event, kinds=MESSAGE_KINDS, min_level=Level.WARNING)
        self._shortcuts = []
        for key in (str(app.estop_shortcut_text().split(' / ')[0]), 'Pause'):
            sc = QtWidgets.QShortcut(QtGui.QKeySequence(key), window)
            sc.setContext(QtCore.Qt.WindowShortcut)
            sc.activated.connect(lambda: self.app.emergency_stop('jog window (keyboard)'))
            self._shortcuts.append(sc)
        window.setWindowTitle(WINDOW_TITLE)

    # ---------------------------------------------------------------- Keyboard Jog hooks
    def _storage_folder(self):
        return self.app.paths.data / 'jog' / 'setups'

    def _port_sources(self) -> list:
        """The roles present on this machine, one per motor module (the one of the configuration if several roles
        name the same module), then the PID setpoints"""
        roles = [r for r in self.manual.actuator_roles() if self.manual.exists(r)]
        by_module: Dict[str, str] = {}
        for r in roles:
            if is_virtual(r):
                continue
            module = self.manual.module(r)
            if module not in by_module or (r in self._configured and by_module[module] not in self._configured):
                by_module[module] = r
        keep = set(by_module.values())
        return [r for r in roles if is_virtual(r) or r in keep]

    def _sources_signature(self, sources: list):
        return tuple((r, self.manual.module(r)) for r in sources) + (tuple(sorted(self.inverted)),)

    def _build_ports(self, sources: list) -> Dict[str, ActuatorPort]:
        return {r: LaserJogPort(self.manual, r, r in self.inverted) for r in sources}

    def _check_actuators(self):
        if not self._closing:
            self.refresh_actuators()

    def refresh_actuators(self, force: bool = False):
        before = set(self.ports)
        super().refresh_actuators(force)
        new_virtual = [n for n in self.ports if is_virtual(n) and n not in before and n not in self._configured]
        if new_virtual:              # a PID setpoint appears hidden: shown from the settings if wanted
            self._loading = True
            try:
                for name in new_virtual:
                    jog_settings.set_actuator_value(self.settings, name, 'pair', HIDDEN)
            finally:
                self._loading = False
            self.apply_settings()
        for name in self.ports:      # the broker knows the positions already
            value = self.manual.position(name)
            if value is not None and math.isfinite(value):
                self._on_value(name, value)

    def _load_startup_setup(self):
        pass                         # the configuration comes with the Laser Control preset

    def _set_dirty(self, dirty: bool):
        self._dirty = dirty
        if dirty and not self._applying and not self._loading:
            self.app._mark_dirty()
        self._refresh_setup_group(lists=False)

    def value_changed(self, param):
        path = self.settings.childPath(param) or []
        if path and path[0] == 'lc_setup':
            return
        super().value_changed(param)

    # ---------------------------------------------------------------- preset, Keyboard Jog setups
    def _refresh_setup_group(self, lists: bool = True):
        if getattr(self, 'jog_store', None) is None:
            return                   # Keyboard Jog __init__: the group is not there yet
        loading, self._loading = self._loading, True
        try:
            name = str(self.app.preset.get('name', '') or '(no name)')
            self.settings.child('lc_setup', 'preset').setValue(f'{name} *' if self.app.preset_dirty else name)
            if lists:
                param = self.settings.child('lc_setup', 'jog_setups')
                names = self.jog_store.names() or ['']
                current = param.value()
                param.setLimits(names)
                param.setValue(current if current in names else names[0])
        finally:
            self._loading = loading

    def save_preset(self):
        self.app.save_preset()
        self._refresh_setup_group(lists=False)
        self.show_message(f"Preset '{self.app.preset.get('name', '')}' saved (with the jog configuration)")

    def _module_of_role(self) -> Dict[str, str]:
        """role -> Dashboard module, for the motors of this window (not the PID setpoints)"""
        out = {}
        for role in self.ports:
            module = None if is_virtual(role) else self.manual.module(role)
            if module:
                out[role] = module
        return out

    def import_jog_setup(self, name: str) -> List[str]:
        """Use a setup of the Dashboard's Keyboard Jog: its modules become the roles; the preset is modified (saved
        with it). Returns the warnings (modules absent here)"""
        role_of = {module: role for role, module in self._module_of_role().items()}
        setup = rename_actuators(self.jog_store.load(name), role_of)
        self._configured = set(setup.used_actuators())
        setup.hidden += [n for n in self.ports if is_virtual(n) and n not in self._configured
                         and n not in setup.hidden]
        preset_name = self.setup_name
        warnings = self.load_setup(name, setup)
        self.setup_name = preset_name
        self._set_dirty(True)
        if not warnings:
            self.show_message(f"Keyboard Jog setup '{name}' imported: save the preset to keep it")
        return warnings

    def export_jog_setup(self, name: str) -> str:
        """Save this configuration as a setup of the Dashboard's Keyboard Jog (roles -> modules)"""
        module_of = self._module_of_role()
        setup, _ = rename_actuators(self._read_setup(), module_of).restricted_to(list(module_of.values()))
        name = self.jog_store.save(name, setup)
        self._refresh_setup_group()
        self.show_message(f"Exported as the Keyboard Jog setup '{name}'")
        return name

    def _import_selected(self):
        name = self.settings['lc_setup', 'jog_setups']
        if not name:
            self.show_message('No Keyboard Jog setup saved yet (Dashboard > Keyboard Jog > Settings > Save as)',
                              warning=True)
            return
        if not self._ask('Import setup', f"Replace the jog configuration by the Keyboard Jog setup '{name}'?"):
            return
        try:
            self.import_jog_setup(name)
        except Exception as e:
            logger.warning(f"Keyboard Jog setup '{name}' not imported: {e}")
            self.show_message(f"Setup '{name}' not imported: {e}", warning=True)

    def _export(self):
        name = clean_name(self.settings['lc_setup', 'export_name'])
        if not name:
            self.show_message("Type a name in 'Export as' first", warning=True)
            return
        if self.jog_store.exists(name) and not self._ask('Export setup', f"Replace the Keyboard Jog setup '{name}'?"):
            return
        self.export_jog_setup(name)
        self._loading = True
        self.settings.child('lc_setup', 'export_name').setValue('')
        self._loading = False

    def _decorate_window(self):
        super()._decorate_window()
        if self.mainwindow is not None:
            self.mainwindow.setWindowTitle(WINDOW_TITLE)

    def show_dashboard(self, show: bool = None):
        pass

    def _fit_window(self):
        # the automation strip is below the panel: its layouts are refreshed first (Keyboard Jog refreshes the
        # layouts from the panel up to the window only)
        if self.panel is not None and getattr(self, 'quick', None) is not None:
            for widget in (self.quick, self.panel.top_container):
                layout = QtWidgets.QWidget.layout(widget)
                if layout is not None:
                    layout.invalidate()
                    layout.activate()
        super()._fit_window()

    # ---------------------------------------------------------------- invert keys (Laser Control only)
    def _add_actuator_actions(self, menu: QtWidgets.QMenu, name: str, *args, **kwargs):
        super()._add_actuator_actions(menu, name, *args, **kwargs)
        if is_virtual(name):
            return
        action = menu.addAction('Invert keys', lambda: self.set_inverted(name, name not in self.inverted))
        action.setCheckable(True)
        action.setChecked(name in self.inverted)
        action.setToolTip('Swap the - / + keys of this motor (as the old Alignment axes)')

    def set_inverted(self, name: str, inverted: bool):
        if inverted:
            self.inverted.add(name)
        else:
            self.inverted.discard(name)
        port = self.ports.get(name)
        if isinstance(port, LaserJogPort):
            port.inverted = inverted
        self._sources_key = self._sources_signature(self._port_sources())
        self.apply_settings()
        self._set_dirty(True)

    def _line_tooltip(self, group) -> str:
        text = super()._line_tooltip(group)
        inverted = [a for a in group.actuators if a in self.inverted]
        return text + (f"\nkeys inverted: {', '.join(inverted)}" if inverted else '')

    # ---------------------------------------------------------------- broker -> ports
    def _on_manual_position(self, role: str, value: float, done: bool):
        port = self.ports.get(role)
        if port is None or self._disposed:
            return
        value = float(value) if value is not None and math.isfinite(value) else None
        if done and port.on_done is not None:
            port.on_done(value)
        if value is not None and port.on_value is not None:
            port.on_value(value)

    def _on_event(self, ev):
        if self._disposed or self.panel is None:
            return
        self.show_message(ev.message, warning=True, duration_ms=8000)

    # ---------------------------------------------------------------- Laser Control API
    def load_from_preset(self, preset: dict):
        cfg = preset.get('jog') or {}
        message = ''
        if cfg.get('setup'):
            setup = setup_from_dict(cfg['setup'])
            self.inverted = {str(r) for r in cfg.get('invert') or []}
        else:
            setup, inverted = setup_from_alignment(preset.get('alignment'))
            self.inverted = set(inverted)
            if setup.pair_numbers:
                message = ('Jog: groups taken from the axes of the old Alignment tab, the other motors are hidden '
                           '(Settings > Actuators). Check them, then save the preset')
        converted = not cfg.get('setup') and bool(setup.pair_numbers)
        self._configured = set(setup.used_actuators())
        names = self._port_sources()
        setup.hidden += [n for n in names if (converted or is_virtual(n)) and n not in self._configured
                         and n not in setup.hidden]
        self._applying = True
        try:
            self._sources_key = None
            self.refresh_actuators(force=True)
            self.setup = setup
            self.load_setup(preset.get('name', '') or 'preset', setup)
        finally:
            self._applying = False
        self._dirty = False
        if message:
            self.show_message(message, duration_ms=0)

    def write_to_preset(self, preset: dict):
        preset['jog'] = {'setup': setup_to_dict(self._read_setup()),
                         'invert': sorted(r for r in self.inverted if r in self.ports)}

    def set_hardware(self):
        self.refresh_actuators()

    def show_window(self):
        if self.window is None or self._disposed:
            return
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self._decorate_window()
        self.panel.setFocus(QtCore.Qt.OtherFocusReason)

    def refresh(self):
        if not self._disposed and self.window is not None and self.window.isVisible():
            self.quick.refresh()
            if self.panel.settings_visible():
                self._refresh_setup_group(lists=False)      # the preset may be saved from the Presets tab

    def release(self, stop: bool = True):
        """Forget the held keys; with ``stop``, stop the motors this window is moving"""
        if self.panel is not None:
            self.keyboard.reset()
            self.controller.release_all(stop=stop)

    # ---------------------------------------------------------------- closing: hide, dispose with Laser Control
    def eventFilter(self, obj, event):
        if obj is getattr(self, 'mainwindow', None) and event.type() == QtCore.QEvent.Close and not self._disposed:
            self.release(stop=True)          # hidden, not destroyed: shown again from the Laser Control toolbar
            self.history.save()
            return False
        return QtCore.QObject.eventFilter(self, obj, event)

    def quit_fun(self) -> bool:
        if self.mainwindow is not None:
            self.mainwindow.close()
        return True

    def lc_shutdown(self):
        """Laser Control closes: stop the moves started here, save the history, close the window"""
        if self._disposed:
            return
        self.shutdown()
        self._disposed = True
        self.manual.remove_listener(self._on_manual_position)
        self.app.core.events.unsubscribe(self._event_token)
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
