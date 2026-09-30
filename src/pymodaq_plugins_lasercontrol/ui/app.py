"""Application object of the Laser Control extension: owns the core, the stores, the recorder and
the widgets; drives the event-driven core from the GUI thread (option O2-A).

Nothing is started here, ever (S9): the last preset is reloaded but automations only start from
an explicit user action.
"""
from __future__ import annotations

import copy
import json
import math
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qtpy import QtCore, QtGui, QtWidgets

from ..core import Clock, LaserCore, RoleMap
from ..core.events import EventKind, Level
from ..core.log import DedupLogger, add_rotating_file_handler, module_logger
from ..core.safety import SafetyConfig
from ..core.states import AutoState, ENGAGED_STATES
from ..engines import CalibrationAutomation, OptimizerAutomation, PIDAutomation, PIDSettings
from ..hardware.pymodaq_ports import discover_ports
from ..hardware.shaker import PiezoShaker
from ..presets import (LegacyImport, PresetStore, SessionStore, content_hash, default_preset, normalize,
                       validate_preset)
from ..recording import EventTriggerWriter, Recorder
from ..utils import config_get
from .manual import VIRTUAL_PREFIX, ManualControl, VirtualActuators
from .widgets import ask_yes_no, info, warn

logger = DedupLogger(module_logger(__file__))

TAB_TITLES = {'overview': '🏠 Overview', 'alignment': '🎯 Alignment', 'xy': '✥ XY', 'pid': '⚙️ PID',
              'optimizer': '📈 Optimization', 'scan': '📊 1D Scan', 'recording': '⏺ Recording',
              'presets': '📂 Presets', 'config': '🔧 Configuration', 'log': '📜 Log'}

try:
    from pymodaq_utils.config import get_set_local_dir
except Exception:  # pragma: no cover
    get_set_local_dir = None


@dataclass
class Paths:
    data: Path
    presets: Path
    recordings: Path
    roles_file: Path
    session_file: Path
    history_file: Path
    logs: Path
    trigger: Optional[Path]


def machine_name(config) -> str:
    name = str(config_get(config, 'machine', 'name', default='') or '').strip()
    return name or socket.gethostname().split('.')[0] or 'machine'


def resolve_paths(config, machine: str, data_override: Optional[Path] = None) -> Paths:
    def cfg_path(*keys) -> Optional[Path]:
        v = str(config_get(config, *keys, default='') or '').strip()
        return Path(v).expanduser() if v else None

    if data_override is not None:
        data = Path(data_override)
    else:
        data = cfg_path('paths', 'data_folder')
        if data is None:
            try:
                base = get_set_local_dir(user=True) if get_set_local_dir else Path.home() / '.pymodaq'
            except Exception:
                base = Path.home() / '.pymodaq'
            data = Path(base) / 'laser_control'
    presets = cfg_path('paths', 'presets_folder') or data / 'presets'
    recordings = cfg_path('paths', 'recordings_folder') or data / 'recordings'
    trigger = cfg_path('paths', 'event_trigger_folder')
    for p in (data, presets, recordings):
        p.mkdir(parents=True, exist_ok=True)
    return Paths(data=data, presets=presets, recordings=recordings, roles_file=data / f'roles_{machine}.toml',
                 session_file=data / f'session_{machine}.json',
                 history_file=data / f'alignment_history_{machine}.json', logs=data / 'logs', trigger=trigger)


def git_version() -> str:
    here = Path(__file__).resolve().parent
    no_window = getattr(subprocess, 'CREATE_NO_WINDOW', 0)     # Windows: no console flash
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=here, capture_output=True, text=True,
                             timeout=3, creationflags=no_window)
        if out.returncode != 0:
            return 'unknown'
        h = out.stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=here,
                               capture_output=True, text=True, timeout=3, creationflags=no_window)
        return h + ('-modified' if dirty.stdout.strip() else '')
    except Exception:
        return 'unknown'


class PIDSetpoints(VirtualActuators):
    """The setpoint of every PID, as a virtual actuator ``SP:<pid id>`` of the Alignment / XY tabs.
    The value shown is the one the PID uses (captured setpoint included); a change goes through the
    PID tab field, so it is applied live, saved in the preset and the tab stays in sync."""

    def __init__(self, app: 'LaserControlApp'):
        self.app = app

    def _widget(self, name: str):
        tab = self.app.tabs.get('pid')
        return tab.widgets.get(name[len(VIRTUAL_PREFIX):]) if tab is not None else None

    def names(self) -> List[str]:
        tab = self.app.tabs.get('pid')
        ids = tab._order if tab is not None else [p['id'] for p in self.app.preset.get('pids', [])]
        return [VIRTUAL_PREFIX + i for i in ids]

    def value(self, name: str) -> Optional[float]:
        a = self.app.core.get('PID:' + name[len(VIRTUAL_PREFIX):])
        if a is not None and a.state in ENGAGED_STATES:
            return float(a.settings.setpoint)
        w = self._widget(name)
        return float(w._param('setpoint').value()) if w is not None else None

    def set_value(self, name: str, value: float) -> Tuple[bool, str]:
        w = self._widget(name)
        if w is None:
            return False, 'PID not found'
        if not math.isfinite(value):
            return False, 'invalid value'
        w._param('setpoint').setValue(float(value))
        return True, ''

    def units(self, name: str) -> str:
        w = self._widget(name)
        sig = self.app.core.roles.signals.get(w.combo_signal.currentText()) if w is not None else None
        return sig.units if sig is not None else ''

    def label(self, name: str) -> str:
        w = self._widget(name)
        return f'setpoint of {w.label()}' if w is not None else 'setpoint'


class LaserControlApp(QtCore.QObject):
    shake_done = QtCore.Signal(bool, str)
    preset_applied = QtCore.Signal()

    def __init__(self, modules_manager=None, dockarea=None, window: Optional[QtWidgets.QWidget] = None,
                 dashboard_preset: str = '', config=None, clock: Optional[Clock] = None,
                 data_folder: Optional[Path] = None, start_timers: bool = True):
        super().__init__()
        from .. import __version__
        self.version = __version__
        self.git_hash = git_version()
        self.config = config
        self.machine = machine_name(config)
        self.paths = resolve_paths(config, self.machine, data_folder)
        add_rotating_file_handler(self.paths.logs)
        self.operator = str(config_get(config, 'machine', 'operator', default='') or '')
        self.dashboard_preset = dashboard_preset
        self.modules_manager = modules_manager
        self.window = window
        self.preset_dirty = False
        self._closing = False

        roles = RoleMap.load(self.paths.roles_file)
        if not roles.machine:
            roles.machine = self.machine
        self.core = LaserCore(clock or Clock(), roles)
        self.core.confirm_handler = self._confirm_moves
        self.core.shake_handler = self._fire_shaker
        self.manual = ManualControl(self.core, feedback=self._manual_feedback, virtual=PIDSetpoints(self),
                                    defer=lambda ms, fn: QtCore.QTimer.singleShot(ms, fn))
        self.store = PresetStore(self.paths.presets)
        self.session_store = SessionStore(self.paths.session_file)
        self.preset: dict = default_preset('New preset')
        self.recorder = Recorder(self.core, self.paths.recordings, self.preset['recording'], self.metadata,
                                 max_mb=float(config_get(config, 'recording', 'max_file_mb', default=500)),
                                 max_hours=float(config_get(config, 'recording', 'max_file_hours', default=12)))
        self.triggers = EventTriggerWriter(self.core.events, self.paths.trigger,
                                           self.preset['recording']['trigger_events'])
        self.shaker = PiezoShaker(str(config_get(config, 'shaker', 'ip', default='rp-f01dda.local')),
                                  float(config_get(config, 'shaker', 'amplitude_v', default=0.1)),
                                  extra_folders=[self.paths.data])
        self.shake_done.connect(self._on_shake_done)
        self.tabs: Dict[str, QtWidgets.QWidget] = {}
        self.status_bar = None
        self.tab_widget: Optional[QtWidgets.QTabWidget] = None
        self.event_log = None

        self.attach_hardware()

        tick_hz = float(config_get(config, 'core', 'tick_hz', default=20) or 20)
        ui_hz = min(float(config_get(config, 'ui', 'refresh_hz', default=5) or 5), 5.0)
        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.setInterval(int(1000 / max(tick_hz, 1.0)))
        self.tick_timer.timeout.connect(self._tick)
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.setInterval(int(1000 / max(ui_hz, 0.5)))
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.session_timer = QtCore.QTimer(self)
        self.session_timer.setInterval(30000)
        self.session_timer.timeout.connect(self.save_session)
        self._start_timers = start_timers
        self.core.events.publish(EventKind.INFO, 'UI', f"Laser Control {self.version} (git {self.git_hash}) "
                                                        f"on '{self.machine}'")

    # ================================================================== hardware
    def attach_hardware(self):
        acts, dets = discover_ports(self.modules_manager)
        self.core.broker.attach(acts, dets)
        self.core.broker.request_positions()
        self.core.events.publish(EventKind.INFO, 'BROKER',
                                 f"{len(acts)} actuator(s), {len(dets)} detector(s) from the dashboard")

    def refresh_hardware(self):
        """Modules list changed (PyMoDAQ 5.2 experiment manager...): rebind the ports."""
        if any(a.state in ENGAGED_STATES for a in self.core.automations.values()):
            warn(self.window, 'Hardware', 'Stop the automations before re-attaching the hardware.')
            return
        self.attach_hardware()
        self._on_roles_or_hardware_changed()

    def role_detectors(self) -> List[str]:
        dets = set()
        for r in self.core.roles.signals.values():
            det = r.channel.split('/')[0]
            dets.add(det)
        dets |= set(self.core.roles.heartbeats)
        known = set(self.core.broker.detectors) | set(self.core.signals.detectors())
        return sorted(d for d in dets if d in known) or sorted(self.core.broker.detectors)

    def start_detectors(self):
        self.core.broker.start_grab(self.role_detectors())

    # ================================================================== UI construction
    def build(self, dockarea) -> None:
        from pymodaq_gui.utils.dock import Dock
        from .status_bar import StatusBar
        from .event_log import EventLogWidget
        from .overview import OverviewTab
        from .alignment import AlignmentWidget
        from .xy import XYTab
        from .pid_tab import PIDTab
        from .optimizer_tab import OptimizerTab
        from .scan_tab import ScanTab
        from .recording_tab import RecordingTab
        from .presets_tab import PresetsTab
        from .config_tab import ConfigTab

        self.status_bar = StatusBar(self)
        self.status_bar.automation_clicked.connect(self.select_automation_tab)
        self.tab_widget = QtWidgets.QTabWidget()
        self.tabs['overview'] = OverviewTab(self)
        self.tabs['alignment'] = AlignmentWidget(self.manual, shake_callback=lambda: self.shake('keyboard'),
                                                 history_path=self.paths.history_file)
        self.tabs['alignment'].config_changed.connect(self._mark_dirty)
        from .quick_panel import QuickPanel
        self.quick_panel = QuickPanel(self)
        self.tabs['alignment'].set_bottom_panel(self.quick_panel)
        self.tabs['xy'] = XYTab(self)
        self.tabs['pid'] = PIDTab(self)
        self.tabs['optimizer'] = OptimizerTab(self)
        self.tabs['scan'] = ScanTab(self)
        self.tabs['recording'] = RecordingTab(self)
        self.tabs['presets'] = PresetsTab(self)
        self.tabs['config'] = ConfigTab(self)
        # the journal is only needed to investigate a problem: its own tab (the title counts the
        # warnings / alarms not seen yet) instead of a dock taking height under the tabs
        self.event_log = EventLogWidget(self.core.events)
        self.tabs['log'] = self.event_log
        self.event_log.unseen_changed.connect(self._on_log_unseen)
        for key, w in self.tabs.items():
            self.tab_widget.addTab(w, TAB_TITLES[key])
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        if dockarea is not None:
            self.docks = {'status': Dock('Status', size=(1000, 60), hideTitle=True),
                          'main': Dock('Laser Control', size=(1000, 700))}
            dockarea.addDock(self.docks['status'], 'top')
            dockarea.addDock(self.docks['main'], 'bottom', self.docks['status'])
            self.docks['status'].addWidget(self.status_bar)
            self.docks['main'].addWidget(self.tab_widget)
        self._install_shortcuts(dockarea)
        self.restore_session()
        if self._start_timers:
            self.start_timers()

    def _on_log_unseen(self, n: int):
        i = self.tab_widget.indexOf(self.event_log)
        if i >= 0:
            self.tab_widget.setTabText(i, TAB_TITLES['log'] + (f' ({n} ⚠)' if n else ''))

    def start_timers(self):
        self.tick_timer.start()
        self.ui_timer.start()
        self.session_timer.start()

    def estop_shortcut_text(self) -> str:
        return f"{config_get(self.config, 'ui', 'estop_shortcut', default='F12') or 'F12'} / Pause"

    def _install_shortcuts(self, dockarea):
        host = self.window or dockarea
        if host is None:
            return
        self._shortcuts = []
        keys = [str(config_get(self.config, 'ui', 'estop_shortcut', default='F12') or 'F12'), 'Pause']
        for k in keys:
            sc = QtWidgets.QShortcut(QtGui.QKeySequence(k), host)
            sc.setContext(QtCore.Qt.WindowShortcut)
            sc.activated.connect(lambda: self.emergency_stop('keyboard shortcut'))
            self._shortcuts.append(sc)

    # ================================================================== timers
    def _tick(self):
        try:
            self.core.tick()
            self.recorder.tick()
        except Exception as e:  # pragma: no cover - the scheduler itself must never die
            logger.exception(f'core tick failed: {e}')

    def refresh_ui(self):
        try:
            if self.status_bar is not None:
                self.status_bar.refresh()
            if self.tab_widget is not None:
                w = self.tab_widget.currentWidget()
                if hasattr(w, 'refresh'):
                    w.refresh()
                align = self.tabs.get('alignment')
                if align is not None and w is not align:
                    align.refresh()
        except Exception as e:  # pragma: no cover
            logger.exception(f'UI refresh failed: {e}')

    def _on_tab_changed(self, index: int):
        w = self.tab_widget.widget(index)
        if w is self.tabs.get('alignment'):
            QtCore.QTimer.singleShot(0, w.activate_keyboard)
        elif w is self.tabs.get('xy'):
            QtCore.QTimer.singleShot(0, w._activate_current)
        elif w is self.tabs.get('presets'):
            w.refresh_list()
        self.refresh_ui()

    def select_automation_tab(self, auto_id: str):
        if self.tab_widget is None:
            return
        if auto_id.startswith('PID:'):
            self.tab_widget.setCurrentWidget(self.tabs['pid'])
            self.tabs['pid'].select(auto_id)
        elif auto_id.startswith('OPT:'):
            self.tab_widget.setCurrentWidget(self.tabs['optimizer'])
            self.tabs['optimizer'].select(auto_id)
        elif auto_id == 'SCAN':
            self.tab_widget.setCurrentWidget(self.tabs['scan'])
        elif auto_id.startswith('XYSCAN:'):
            self.tab_widget.setCurrentWidget(self.tabs['xy'])

    # ================================================================== feedback
    def _manual_feedback(self, msg: str):
        self.core.events.publish(EventKind.MOVE_MANUAL, 'HUMAN', msg, Level.WARNING)

    def _mark_dirty(self, *args):
        self.preset_dirty = True

    def set_operator(self, name: str):
        self.operator = name
        self.recorder.update_metadata()

    def config_file_path(self) -> Optional[Path]:
        try:
            return Path(self.config.config_path)
        except Exception:
            return None

    # ================================================================== automations
    def ensure_pid(self, cfg: dict) -> PIDAutomation:
        aid = f"PID:{cfg['id']}"
        a = self.core.get(aid)
        settings = PIDSettings.from_dict(cfg.get('settings', {}))
        label = cfg.get('label') or f"PID {cfg['id']}"
        if a is None or not isinstance(a, PIDAutomation):
            a = PIDAutomation(cfg['id'], cfg.get('signal', ''), cfg.get('motors', {}), settings, label,
                              cfg.get('in_stabilization', True))
            self.core.add_automation(a)
        elif a.state == AutoState.OFF:
            a.signal_role = cfg.get('signal', '')
            a.motors = {r: float(w) for r, w in (cfg.get('motors') or {}).items()}
            a.set_settings(settings)
            a.label = label
            a.in_stabilization = cfg.get('in_stabilization', True)
        return a

    def start_pid(self, cfg: dict) -> Tuple[bool, str]:
        a = self.ensure_pid(cfg)
        return self.core.start(a.id)

    def calibration(self, pid_id: str) -> Optional[CalibrationAutomation]:
        a = self.core.get(f'CAL:{pid_id}')
        return a if isinstance(a, CalibrationAutomation) else None

    def start_calibration(self, cfg: dict) -> Tuple[bool, str]:
        """Calibrate the motors of the PID ``cfg`` (one after the other, the others still). A previous
        run is replaced; nothing changes in the PID settings until the user applies the proposal."""
        aid = f"CAL:{cfg['id']}"
        old = self.core.get(aid)
        if old is not None:
            if old.state in ENGAGED_STATES:
                return False, 'a calibration is already running'
            self.core.remove_automation(aid)
        pid = self.core.get(f"PID:{cfg['id']}")
        if pid is not None and pid.state in ENGAGED_STATES:
            return False, 'stop the PID before calibrating its motors'
        st = PIDSettings.from_dict(cfg.get('settings', {}))
        roles = [r for r, w in (cfg.get('motors') or {}).items() if w]
        label = cfg.get('label') or cfg['id']
        cal = CalibrationAutomation(cfg.get('signal', ''), roles, settle_s=st.settle_s, period_s=st.period_s,
                                    auto_id=aid, label=f'Calibration {label}')
        self.core.add_automation(cal)
        return self.core.start(aid)

    DECOUPLING_ID = 'DEC'

    def decoupling(self) -> Optional[CalibrationAutomation]:
        a = self.core.get(self.DECOUPLING_ID)
        return a if isinstance(a, CalibrationAutomation) else None

    def start_decoupling(self, cfgs: List[dict], cycles: int = 3) -> Tuple[bool, str]:
        """Measure every motor of the PIDs ``cfgs`` on every one of their signals (one run, one motor at
        a time, back to the start positions). Nothing changes in the PIDs until the user applies the
        proposed weights (engines/decoupling.py)."""
        if len(cfgs) < 2:
            return False, 'choose at least two PIDs'
        signals = [c.get('signal', '') for c in cfgs]
        if '' in signals or len(set(signals)) != len(signals):
            return False, 'each PID needs its own signal (two PIDs on the same signal cannot be decoupled)'
        motors = list(dict.fromkeys(r for c in cfgs for r, w in (c.get('motors') or {}).items() if w))
        if len(motors) < len(signals):
            return False, (f'{len(motors)} motors for {len(signals)} signals: add motors to the PIDs '
                           f'(at least as many motors as signals)')
        for c in cfgs:
            for aid in (f"PID:{c['id']}", f"CAL:{c['id']}"):
                a = self.core.get(aid)
                if a is not None and a.state in ENGAGED_STATES:
                    return False, f'stop {a.label} first'
        old = self.core.get(self.DECOUPLING_ID)
        if old is not None:
            if old.state in ENGAGED_STATES:
                return False, 'a decoupling measurement is already running'
            self.core.remove_automation(self.DECOUPLING_ID)
        settle = max(PIDSettings.from_dict(c.get('settings', {})).settle_s for c in cfgs)
        labels = ' / '.join(c.get('label') or c['id'] for c in cfgs)
        cal = CalibrationAutomation(signals[0], motors, cycles=cycles, extra_signals=signals[1:], settle_s=settle,
                                    auto_id=self.DECOUPLING_ID, label=f'Decoupling {labels}')
        self.core.add_automation(cal)
        return self.core.start(self.DECOUPLING_ID)

    def optimizer(self, opt_id: str) -> Optional[OptimizerAutomation]:
        a = self.core.get(f'OPT:{opt_id}')
        return a if isinstance(a, OptimizerAutomation) else None

    def start_optimizer(self, cfg: dict, single_shot: bool) -> Tuple[bool, str]:
        """Start the optimisation ``cfg['id']`` (one pass, periodic, or full if cfg['config'] says so).
        The automation is rebuilt from the current settings each time it starts (its cost history is kept)."""
        aid = f"OPT:{cfg['id']}"
        label = cfg.get('label') or cfg['id']
        opt = self.core.get(aid)
        if opt is None or opt.state == AutoState.OFF:
            new = OptimizerAutomation(cfg['config'], cfg['motors'], cfg['signals'], auto_id=aid, label=label)
            if opt is not None:
                new.history = opt.history
                new.iteration = opt.iteration
                new.pass_count = opt.pass_count
                self.core.remove_automation(aid)
            opt = new
            self.core.add_automation(opt)
        opt.single_shot = single_shot
        opt.label = label + (' (full)' if cfg['config'].get('full_optimization') else '')
        return self.core.start(opt.id)

    # ================================================================== Alignment buttons
    def quick_items(self) -> List[dict]:
        """Automations shown as buttons in the Alignment tab ('Button in Alignment' ticked), in the
        order of the PID then Optimization tabs: [{'id', 'kind', 'key', 'label', 'action'}]."""
        out = []
        pid_tab = self.tabs.get('pid')
        if pid_tab is not None:
            out += pid_tab.quick_items()
        opt_tab = self.tabs.get('optimizer')
        if opt_tab is not None:
            out += opt_tab.quick_items()
        return out

    def quick_click(self, auto_id: str, action: str = 'toggle') -> Tuple[bool, str]:
        """One click on an Alignment button, no confirmation: OFF -> start, HELD -> resume, engaged -> stop.
        ``action`` forces 'start' / 'stop' / 'resume' (context menu). Refusals go to the journal."""
        a = self.core.get(auto_id)
        state = a.state if a is not None else AutoState.OFF
        if action == 'toggle':
            action = 'resume' if state == AutoState.HELD else ('stop' if state in ENGAGED_STATES else 'start')
        if action == 'stop':
            self.core.stop(auto_id, 'stopped from Alignment')
            return True, ''
        if action == 'resume':
            ok, msg = self.core.resume(auto_id)
            if not ok:
                self.core.events.publish(EventKind.AUTOMATION_START, auto_id, f'Resume refused: {msg}', Level.WARNING)
            return ok, msg
        if auto_id.startswith('PID:'):
            cfg = self.tabs['pid'].config_of(auto_id)
            if cfg is None:
                return False, 'PID not found'
            if state in ENGAGED_STATES:
                self.core.stop(auto_id, 'restart from Alignment')
            return self.start_pid(cfg)
        if auto_id.startswith('OPT:'):
            cfg = self.tabs['optimizer'].config_of(auto_id)
            if cfg is None:
                return False, 'optimization not found'
            if not cfg['motors'] or not cfg['signals']:
                msg = 'tick at least one motor and one objective signal (Optimization tab)'
                self.core.events.publish(EventKind.AUTOMATION_START, auto_id, f'Start refused: {msg}',
                                         Level.WARNING)
                return False, msg
            if state in ENGAGED_STATES:
                self.core.stop(auto_id, 'restart from Alignment')
            how = cfg.get('button_action', 'single')
            cfg['config']['full_optimization'] = how == 'full'
            return self.start_optimizer(cfg, single_shot=how != 'periodic')
        return False, f'unknown automation: {auto_id}'

    def stabilization_pids(self) -> List[dict]:
        pid_tab = self.tabs.get('pid')
        cfgs = pid_tab.configs() if pid_tab is not None else self.preset.get('pids', [])
        return [c for c in cfgs if c.get('in_stabilization', True)]

    def stabilization_problems_text(self) -> str:
        problems = []
        for cfg in self.stabilization_pids():
            a = self.core.get(f"PID:{cfg['id']}")
            if a is not None and a.state in ENGAGED_STATES:
                continue
            try:
                a = self.ensure_pid(cfg)
            except RuntimeError:
                continue
            errs = self.core.validate(a)
            if errs:
                problems.append(f"{a.label}: " + ' ; '.join(errs))
        return '\n'.join(problems)

    def activate_stabilization(self, parent=None):
        """T5.3: summary of what will move, confirmation, then start every stabilisation PID."""
        cfgs = self.stabilization_pids()
        if not cfgs:
            info(parent, 'Stabilization', 'No PID marked "stabilization" in the preset.')
            return
        autos = [self.ensure_pid(c) for c in cfgs]
        todo = [a for a in autos if a.state not in ENGAGED_STATES]
        errors = {a.label: self.core.validate(a) for a in todo}
        errors = {k: v for k, v in errors.items() if v}
        if errors:
            self.core.events.publish(EventKind.AUTOMATION_START, 'UI', 'Stabilization refused (nothing was started): ' +
                                     ' | '.join(f"{k}: {' ; '.join(v)}" for k, v in errors.items()), Level.WARNING)
            warn(parent, 'Stabilization impossible',
                 'Nothing was started:\n\n' + '\n\n'.join(f'{k}:\n  ' + '\n  '.join(v) for k, v in errors.items()))
            return
        lines = []
        for a in todo:
            lines.append('• ' + a.summary())
            for r in a.motor_roles():
                lim = self.core.safety.limits(r)
                if lim:
                    lines.append(f'     {r}: step ≤ {lim.max_step:g}, excursion ± {lim.max_excursion:g}, '
                                 f'rate ≤ {lim.rate_max:g} / {lim.rate_window_s:g} s')
        draft = '' if self.preset.get('status') == 'validated' else '\n\n⚠ This preset is NOT validated.'
        if not ask_yes_no(parent, 'Activate stabilization',
                          'What will move:\n\n' + '\n'.join(lines) + draft + '\n\nActivate?'):
            return
        failed = []
        for a in todo:
            ok, msg = self.core.start(a.id)
            if not ok:
                failed.append(f'{a.label}: {msg}')
        if failed:
            for a in todo:
                self.core.stop(a.id, 'incomplete activation')
            self.core.events.publish(EventKind.AUTOMATION_START, 'UI',
                                     'Stabilization cancelled (all or nothing): ' + ' | '.join(failed), Level.WARNING)
            warn(parent, 'Stabilization', 'Activation cancelled:\n\n' + '\n'.join(failed))
        else:
            self.core.events.publish(EventKind.AUTOMATION_START, 'UI',
                                     f"Stabilization activated ({', '.join(a.label for a in todo)})")

    def held_stabilization(self) -> list:
        """Stabilisation PIDs frozen by the safety supervisor (HELD, waiting for a manual resume, O10-A)."""
        out = []
        for cfg in self.stabilization_pids():
            a = self.core.get(f"PID:{cfg['id']}")
            if a is not None and a.state == AutoState.HELD:
                out.append(a)
        return out

    def resume_stabilization(self, parent=None):
        """Manual resume (O10-A) of every held stabilisation PID; each one is checked again by the core."""
        failed = []
        for a in self.held_stabilization():
            ok, msg = self.core.resume(a.id)
            if not ok:
                failed.append(f'{a.label}: {msg}')
        if failed:
            warn(parent, 'Resume', 'Resume refused:\n\n' + '\n'.join(failed))

    def deactivate_stabilization(self):
        names = []
        for cfg in self.stabilization_pids():
            if self.core.stop(f"PID:{cfg['id']}", 'stabilization deactivated'):
                names.append(cfg.get('label') or cfg['id'])
        if names:
            self.core.events.publish(EventKind.AUTOMATION_STOP, 'UI', f"Stabilization deactivated ({', '.join(names)})")

    def emergency_stop(self, source: str = 'UI'):
        for key in ('alignment',):
            w = self.tabs.get(key)
            if w is not None:
                w.release_all_jogs()
        xy = self.tabs.get('xy')
        if xy is not None:
            for g in xy.groups:
                g.release_all_jogs()
        self.core.emergency_stop(source)
        self.refresh_ui()

    # ================================================================== shaker
    def shake(self, source: str = 'UI'):
        if not bool(config_get(self.config, 'shaker', 'enabled', default=True)):
            self.core.events.publish(EventKind.SHAKE, source, 'Shaker disabled in the configuration', Level.WARNING)
            return
        self.core.shake(source)

    def _fire_shaker(self):
        self.shaker.shake_async(lambda ok, msg: self.shake_done.emit(ok, msg))

    def _on_shake_done(self, ok: bool, msg: str):
        if not ok:
            self.core.events.publish(EventKind.ERROR, 'SHAKER', f'Shake failed: {msg}', Level.ERROR)

    def _confirm_moves(self, a, text: str, answer):
        """'Confirm moves' dialog: shown asynchronously; the automation is PAUSED meanwhile."""
        def ask():
            box = QtWidgets.QMessageBox(self.window)
            box.setWindowTitle(f'{a.label} – confirm the move')
            box.setText(text)
            box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            box.setDefaultButton(QtWidgets.QMessageBox.No)
            fn = getattr(box, 'exec', None) or box.exec_
            answer(fn() == QtWidgets.QMessageBox.Yes)
        QtCore.QTimer.singleShot(0, ask)

    # ================================================================== recording
    def toggle_recording(self, on: bool):
        if on and not self.recorder.active:
            self.recorder.configure(self.preset.get('recording', {}) if 'recording' not in self.tabs
                                    else self.tabs['recording'].settings())
            self.recorder.start()
        elif not on and self.recorder.active:
            self.recorder.stop()

    def metadata(self) -> dict:
        p = self.preset
        return {'code_version': self.version, 'git_hash': self.git_hash, 'machine': self.machine,
                'operator': self.operator, 'dashboard_preset': self.dashboard_preset,
                'preset_name': p.get('name', ''), 'preset_hash': content_hash(p), 'preset_status': p.get('status'),
                'conditions': p.get('conditions', {}), 'preset': p, 'roles': self.core.roles.to_dict()}

    # ================================================================== roles / safety
    def apply_roles(self, roles: RoleMap, save: bool = True):
        roles.path = self.paths.roles_file
        roles.machine = self.machine
        if save:
            roles.save(self.paths.roles_file)
            self.core.events.publish(EventKind.INFO, 'UI', f'Roles saved: {self.paths.roles_file.name}')
        self.core.set_roles(roles)
        self.core.set_safety_config(self.core.safety.config)
        self._on_roles_or_hardware_changed()

    def reload_roles(self):
        self.apply_roles(RoleMap.load(self.paths.roles_file), save=False)

    def apply_safety(self, cfg: SafetyConfig):
        self.core.set_safety_config(cfg)
        self.preset_dirty = True

    def _on_roles_or_hardware_changed(self):
        for key in ('pid', 'optimizer', 'scan', 'xy', 'alignment', 'config'):
            w = self.tabs.get(key)
            if w is not None and hasattr(w, 'set_hardware'):
                w.set_hardware()
        ov = self.tabs.get('overview')
        if ov is not None:
            ov.rebuild()

    # ================================================================== presets
    def collect_preset(self) -> dict:
        p = copy.deepcopy(self.preset)
        for w in self.tabs.values():
            if hasattr(w, 'write_to_preset'):
                w.write_to_preset(p)
        return normalize(p)

    def _any_engaged(self) -> List[str]:
        return [a.label for a in self.core.automations.values() if a.state in ENGAGED_STATES]

    def apply_preset(self, preset: dict, source: str = 'UI') -> Tuple[bool, str]:
        engaged = self._any_engaged()
        if engaged:
            return False, 'engaged automations: ' + ', '.join(engaged)
        p = normalize(preset)
        self.preset = p
        self.core.set_safety_config(SafetyConfig.from_dict(p['safety']))
        for a in list(self.core.automations.values()):
            if a.kind in ('pid', 'optimizer', 'calibration'):
                self.core.remove_automation(a.id)
        for cfg in p.get('pids', []):
            self.ensure_pid(cfg)
        self.core.preset_name = p.get('name', '')
        self.core.preset_hash = content_hash(p)
        self.recorder.configure(p.get('recording', {}))
        self.triggers.set_triggers(p.get('recording', {}).get('trigger_events', []))
        for w in self.tabs.values():
            if hasattr(w, 'load_from_preset'):
                w.load_from_preset(p)
        for key in ('alignment', 'xy'):          # PID setpoints (SP:<id>) exist once the PID tab is loaded
            w = self.tabs.get(key)
            if w is not None and hasattr(w, 'set_hardware'):
                w.set_hardware()
        self.preset_dirty = False
        rep = validate_preset(p, self.core.roles, self.core.broker.modules(), self.core.signals.channels())
        self.core.events.publish(EventKind.PRESET, source,
                                 f"Preset loaded: {p.get('name')} ({p.get('status')}, {self.core.preset_hash}) "
                                 f"— nothing is started", Level.INFO,
                                 name=p.get('name'), hash=self.core.preset_hash, problems=rep.text())
        self.preset_applied.emit()
        return True, ''

    def load_preset_interactive(self, name: str, parent=None):
        if self.preset_dirty and not ask_yes_no(parent, 'Unsaved changes',
                                                f"The preset '{self.preset.get('name')}' has unsaved "
                                                f"changes.\nDiscard them and load '{name}'?"):
            return
        try:
            p = self.store.load(name)
        except Exception as e:
            warn(parent, 'Preset', f'Cannot read: {e}')
            return
        ok, msg = self.apply_preset(p)
        if not ok:
            warn(parent, 'Preset', f'Loading refused ({msg}). Stop the automations first.')

    def new_preset(self, name: str, parent=None):
        if self._any_engaged():
            warn(parent, 'Preset', 'Stop the automations first.')
            return
        p = self.collect_preset()           # start from the current configuration
        p.update({k: v for k, v in default_preset(name).items()
                  if k in ('name', 'description', 'status', 'validated_on', 'validated_by', 'validation_notes',
                           'validated_hash', 'created', 'modified')})
        self.apply_preset(p)
        self.preset_dirty = True

    def save_preset(self, name: Optional[str] = None) -> Optional[Path]:
        p = self.collect_preset()
        if name:
            p['name'] = name
        path = self.store.save(p)
        self.preset = p
        self.core.preset_name = p['name']
        self.core.preset_hash = content_hash(p)
        self.preset_dirty = False
        self.core.events.publish(EventKind.PRESET, 'UI', f"Preset saved: {p['name']} ({self.core.preset_hash})")
        if 'presets' in self.tabs:
            self.tabs['presets'].load_from_preset(p)
        self.recorder.update_metadata()
        return path

    def validate_current_preset(self, by: str, notes: str):
        p = self.collect_preset()
        rep = validate_preset(p, self.core.roles, self.core.broker.modules(), self.core.signals.channels())
        if not rep.ok and not ask_yes_no(self.window, 'Validate', 'The preset has problems on this machine:\n\n'
                                         + rep.text() + '\n\nMark it as validated anyway?'):
            return
        self.store.mark_validated(p, by, notes)
        self.preset = p
        self.store.save(p)
        self.preset = p
        self.preset_dirty = False
        self.core.events.publish(EventKind.PRESET, 'UI', f"Preset '{p['name']}' marked as validated by {by or '?'}")
        if 'presets' in self.tabs:
            self.tabs['presets'].load_from_preset(p)

    def import_legacy(self, paths: List[str], parent=None):
        if self._any_engaged():
            warn(parent, 'Import', 'Stop the automations first.')
            return
        roles = self.core.roles.copy()
        imp = LegacyImport(roles)
        p = self.collect_preset()
        try:
            for path in paths:
                p = imp.import_file(Path(path), into=p)
        except Exception as e:
            warn(parent, 'Import', f'Import failed: {e}')
            return
        if imp.created_roles:
            self.apply_roles(roles, save=True)
        self.apply_preset(p, 'import')
        self.preset_dirty = True
        info(parent, 'Import', '\n'.join(imp.report) + '\n\nCheck, then save the preset.')

    # ================================================================== session
    def save_session(self):
        if self.tab_widget is None:
            return
        data = {'preset_name': self.preset.get('name'), 'preset_dirty': self.preset_dirty,
                'current_tab': self.tab_widget.currentIndex(), 'operator': self.operator,
                'ml_state': self.core.ml_state}
        if self.preset_dirty:
            data['unsaved_preset'] = self.collect_preset()
        self.session_store.save(data)

    def restore_session(self):
        s = self.session_store.load()
        preset = None
        if s.get('unsaved_preset'):
            preset = s['unsaved_preset']
        elif s.get('preset_name') and self.store.exists(s['preset_name']):
            try:
                preset = self.store.load(s['preset_name'])
            except Exception as e:
                logger.warning(f'last preset unreadable: {e}')
        if preset is None:
            names = self.store.names()
            preset = self.store.load(names[0]) if names else default_preset('New preset')
        self.apply_preset(preset, 'session')
        self.preset_dirty = bool(s.get('unsaved_preset'))
        if s.get('operator'):
            self.operator = s['operator']
        if self.tab_widget is not None and isinstance(s.get('current_tab'), int):
            self.tab_widget.setCurrentIndex(min(max(s['current_tab'], 0), self.tab_widget.count() - 1))
        if bool(config_get(self.config, 'detectors', 'autostart_grab', default=False)):
            QtCore.QTimer.singleShot(500, self.start_detectors)
        if bool(config_get(self.config, 'recording', 'autostart', default=False)):
            QtCore.QTimer.singleShot(1000, lambda: self.toggle_recording(True))
        if not self.core.roles.all_roles():
            self.core.events.publish(EventKind.INFO, 'UI', "No role declared on this machine: Configuration "
                                                            "tab → 'Create missing roles'",
                                     Level.WARNING)

    # ================================================================== shutdown
    def engaged_labels(self) -> List[str]:
        return self._any_engaged()

    def shutdown(self):
        """Closing the extension: automations stopped (no motor moves, D6), files flushed."""
        if self._closing:
            return
        self._closing = True
        for t in (self.tick_timer, self.ui_timer, self.session_timer):
            t.stop()
        try:
            self.save_session()
        except Exception as e:  # pragma: no cover
            logger.warning(f'session not saved: {e}')
        for key in ('alignment', 'xy'):
            w = self.tabs.get(key)
            if w is not None and hasattr(w, 'shutdown'):
                try:
                    w.shutdown()
                except Exception:  # pragma: no cover
                    pass
        self.core.stop_all_automations('extension closed')
        try:
            self.recorder.stop()
        except Exception as e:  # pragma: no cover
            logger.warning(f'recorder stop: {e}')
        self.triggers.close()
        if self.event_log is not None:
            self.event_log.close_bus()
        self.manual.detach()
        self.core.broker.detach_all()
