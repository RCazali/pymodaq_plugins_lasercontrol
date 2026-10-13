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
from ..engines import (CalibrationAutomation, OptimizerAutomation, PIDAutomation, Program, ProgramAutomation,
                       RoutineAutomation)
from ..engines.program import ConversionError
from ..hardware.pymodaq_ports import discover_ports
from ..hardware.shaker import PiezoShaker
from ..presets import (LegacyImport, PresetStore, SessionStore, content_hash, default_preset, normalize,
                       validate_preset)
from ..recording import EventTriggerWriter, Recorder
from ..utils import config_get
from .manual import VIRTUAL_PREFIX, ManualControl, VirtualActuators
from .widgets import ask_yes_no, info, warn

logger = DedupLogger(module_logger(__file__))

TAB_TITLES = {'alignment': '🎯 Alignment', 'axes': '🕹 Axes', 'pid': '⚙️ PID',
              'optimizer': '📈 Optimization', 'routines': '🧭 Routines', 'scan': '📊 1D Scan', 'recording': '⏺ Recording',
              'presets': '📂 Presets', 'config': '🔧 Configuration', 'log': '📜 Log'}
HIDDEN_TABS = ('recording',)     # built (its settings live in the preset) but not shown: not tested on the laser yet

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


def setpoint_name(pid_id: str, k: int = 0) -> str:
    """Virtual actuator of the setpoint of loop ``k`` of a PID: ``SP:<id>`` (first signal), ``SP:<id>/2``…"""
    return f'{VIRTUAL_PREFIX}{pid_id}' + (f'/{k + 1}' if k else '')


class PIDSetpoints(VirtualActuators):
    """The setpoint of every signal of every PID, as a virtual actuator of the Alignment tab
    (``SP:<pid id>`` for its first signal, ``SP:<pid id>/2`` for the second…). The value shown is the one
    the PID uses (captured setpoint included); a change goes through the PID tab field, so it is applied
    live, saved in the preset and the tab stays in sync."""

    def __init__(self, app: 'LaserControlApp'):
        self.app = app

    def _resolve(self, name: str):
        """(PID widget, loop index) or (None, 0)."""
        tab = self.app.tabs.get('pid')
        key = name[len(VIRTUAL_PREFIX):]
        pid_id, _, k = key.partition('/')
        w = tab.widgets.get(pid_id) if tab is not None else None
        try:
            k = int(k) - 1 if k else 0
        except ValueError:
            return None, 0
        if w is None or not 0 <= k < len(w.loops()):
            return None, 0
        return w, k

    def names(self) -> List[str]:
        tab = self.app.tabs.get('pid')
        if tab is None:
            return [setpoint_name(p['id'], k) for p in self.app.preset.get('pids', [])
                    for k in range(len(p.get('loops', [])) or 1)]
        return [setpoint_name(i, k) for i in tab._order for k in range(len(tab.widgets[i].loops()))]

    def value(self, name: str) -> Optional[float]:
        w, k = self._resolve(name)
        if w is None:
            return None
        a = self.app.core.get(w.auto_id)
        if isinstance(a, PIDAutomation) and a.state in ENGAGED_STATES and k < len(a.loops):
            return float(a.loops[k].settings.setpoint)
        return float(w.loops()[k]['setpoint'])

    def set_value(self, name: str, value: float) -> Tuple[bool, str]:
        w, k = self._resolve(name)
        if w is None:
            return False, 'PID not found'
        if not math.isfinite(value):
            return False, 'invalid value'
        w.set_setpoint(k, float(value))
        return True, ''

    def units(self, name: str) -> str:
        w, k = self._resolve(name)
        sig = self.app.core.roles.signals.get(w.loops()[k]['signal']) if w is not None else None
        return sig.units if sig is not None else ''

    def label(self, name: str) -> str:
        w, k = self._resolve(name)
        return f"setpoint of {w.label()} ({w.loops()[k]['signal'] or '?'})" if w is not None else 'setpoint'


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
        self._roles_seen: Optional[tuple] = None      # modules and channels already given a role (ensure_roles)

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

    def ensure_roles(self) -> List[str]:
        """Roles are created automatically: one per motor module of the dashboard and per received 0D channel
        that has none, named after the module / channel (renamed in the Configuration tab). Saved at once.
        Nothing while an automation is engaged: done at the next call."""
        modules, channels = self.core.broker.modules(), self.core.signals.channels()
        seen = (tuple(modules), tuple(channels))
        if seen == self._roles_seen or self._any_engaged():
            return []
        self._roles_seen = seen
        roles = self.core.roles.copy()
        created = roles.auto_complete(modules, channels, units_of=self.core.broker.units)
        if created:
            self.apply_roles(roles, save=True)
            self.core.events.publish(EventKind.INFO, 'UI', f"{len(created)} role(s) created automatically "
                                                            f"({', '.join(created)}): rename them in Configuration")
        return created

    # ================================================================== UI construction
    def build(self, dockarea) -> None:
        from pymodaq_gui.utils.dock import Dock
        from .status_bar import StatusBar
        from .event_log import EventLogWidget
        from .alignment import AlignmentWidget
        from .pid_tab import PIDTab
        from .optimizer_tab import OptimizerTab
        from .routines_tab import RoutinesTab
        from .scan_tab import ScanTab
        from .recording_tab import RecordingTab
        from .presets_tab import PresetsTab
        from .config_tab import ConfigTab

        self.status_bar = StatusBar(self)
        self.status_bar.automation_clicked.connect(self.select_automation_tab)
        self.tab_widget = QtWidgets.QTabWidget()
        self.tabs['alignment'] = AlignmentWidget(self.manual, shake_callback=lambda: self.shake('keyboard'),
                                                 history_path=self.paths.history_file)
        self.tabs['alignment'].config_changed.connect(self._mark_dirty)
        from .quick_panel import QuickPanel
        self.quick_panel = QuickPanel(self)
        self.tabs['alignment'].set_bottom_panel(self.quick_panel)
        self.tabs['axes'] = self.tabs['alignment'].config_page      # settings of the Alignment axes
        self.tabs['pid'] = PIDTab(self)
        self.tabs['optimizer'] = OptimizerTab(self)
        self.tabs['routines'] = RoutinesTab(self)
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
            if key in HIDDEN_TABS:
                w.setParent(self.tab_widget)
                w.hide()
            else:
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
            self.ensure_roles()
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
        elif auto_id.startswith('RTN:'):
            self.tab_widget.setCurrentWidget(self.tabs['routines'])
            self.tabs['routines'].select(auto_id)
        elif auto_id == 'SCAN':
            self.tab_widget.setCurrentWidget(self.tabs['scan'])

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
        """The automation of the PID ``cfg`` (preset format), created or updated. Engaged: only the live
        settings (gains, setpoints, common settings) are applied, never the signals or the weights."""
        aid = f"PID:{cfg['id']}"
        a = self.core.get(aid)
        if not isinstance(a, PIDAutomation):
            if a is not None:
                self.core.remove_automation(aid)
            a = self.core.add_automation(PIDAutomation.from_config(cfg))
        else:
            a.configure(cfg, structure=a.state == AutoState.OFF)
        return a

    def pid_driver(self, pid: str) -> Optional[PIDAutomation]:
        """The automation of the PID ``pid`` (id or 'PID:<id>')."""
        a = self.core.get(pid if pid.startswith('PID:') else f'PID:{pid}')
        return a if isinstance(a, PIDAutomation) else None

    def pid_state(self, a) -> AutoState:
        return a.state if a is not None else AutoState.OFF

    def start_pid(self, cfg: dict) -> Tuple[bool, str]:
        return self.core.start(self.ensure_pid(cfg).id)

    def calibration(self, pid_id: str) -> Optional[CalibrationAutomation]:
        a = self.core.get(f'CAL:{pid_id}')
        return a if isinstance(a, CalibrationAutomation) else None

    def start_calibration(self, cfg: dict, cycles: int = 3) -> Tuple[bool, str]:
        """Measure every motor of the PID ``cfg`` on every one of its signals (one motor at a time, the
        others still, back to the start positions): the sensitivity matrix of the PID. A previous run is
        replaced; nothing changes in the PID until the user applies the proposal."""
        aid = f"CAL:{cfg['id']}"
        old = self.core.get(aid)
        if old is not None:
            if old.state in ENGAGED_STATES:
                return False, 'a calibration is already running'
            self.core.remove_automation(aid)
        pid = self.core.get(f"PID:{cfg['id']}")
        if pid is not None and pid.state in ENGAGED_STATES:
            return False, 'stop the PID before calibrating its motors'
        signals = [l.get('signal', '') for l in cfg.get('loops', [])]
        if not signals or '' in signals or len(set(signals)) != len(signals):
            return False, 'each signal of the PID must be chosen, and different'
        roles = list(cfg.get('motors') or [])
        if not roles:
            return False, 'choose the motors of the PID first'
        if len(roles) < len(signals):
            return False, f'{len(roles)} motor(s) for {len(signals)} signals: at least as many motors as signals'
        st = cfg.get('settings', {}) or {}
        label = cfg.get('label') or cfg['id']
        cal = CalibrationAutomation(signals[0], roles, cycles=cycles, extra_signals=signals[1:],
                                    settle_s=float(st.get('settle_s', 0.0)), period_s=float(st.get('period_s', 0.5)),
                                    auto_id=aid, label=f'Calibration {label}')
        self.core.add_automation(cal)
        return self.core.start(aid)

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

    def routine(self, routine_id: str) -> Optional[ProgramAutomation]:
        a = self.core.get(routine_id if routine_id.startswith('RTN:') else f'RTN:{routine_id}')
        return a if isinstance(a, (ProgramAutomation, RoutineAutomation)) else None

    def start_routine(self, cfg: dict) -> Tuple[bool, str]:
        """Start the routine ``cfg`` (Routines tab: a step routine, converted; or a structured program).
        Rebuilt at each start (a copy: editing never changes a running routine); its history is kept."""
        from .routine_host import AppRoutineHost
        aid = f"RTN:{cfg['id']}"
        old = self.core.get(aid)
        if old is not None and old.state in ENGAGED_STATES:
            return False, f'{old.label} is already running'
        try:
            program = Program.from_dict(copy.deepcopy(cfg))
        except ConversionError as e:
            return False, f'this routine cannot be run: {e}'
        new = ProgramAutomation(program, AppRoutineHost(self))
        if old is not None:
            new.history = old.history
            self.core.remove_automation(aid)
        self.core.add_automation(new)
        return self.core.start(aid)

    def questions(self) -> List[Tuple[str, str, dict]]:
        """Routines waiting for the operator: [(automation id, label, question)]."""
        return [(a.id, a.label, a.question) for a in self.core.automations.values()
                if getattr(a, 'question', None) is not None and a.state in ENGAGED_STATES]

    def answer_question(self, auto_id: str, choice: str) -> Tuple[bool, str]:
        a = self.core.get(auto_id)
        if a is None or getattr(a, 'question', None) is None:
            return False, 'no question pending'
        return a.answer(self.core, choice)

    # ================================================================== Alignment buttons
    def quick_items(self) -> List[dict]:
        """Automations shown as buttons in the Alignment tab ('Button in Alignment' ticked): the routines
        first (the highest-level actions of the day), then the PIDs and the optimisations, in the order of
        their tabs: [{'id', 'kind', 'key', 'label', 'action'}]."""
        out = []
        for key in ('routines', 'pid', 'optimizer'):
            tab = self.tabs.get(key)
            if tab is not None:
                out += tab.quick_items()
        return out

    def quick_click(self, auto_id: str, action: str = 'toggle') -> Tuple[bool, str]:
        """One click on an Alignment button, no confirmation: OFF -> start, HELD -> resume, engaged -> stop.
        ``action`` forces 'start' / 'stop' / 'resume' (context menu). Refusals go to the journal.
        A PID paused on purpose (routine) resumes on a click."""
        a = self.core.get(auto_id)
        state = a.state if a is not None else AutoState.OFF
        if action == 'toggle':
            resumable = state == AutoState.HELD or (state == AutoState.PAUSED and a.pause_kind == 'manual')
            action = 'resume' if resumable else ('stop' if state in ENGAGED_STATES else 'start')
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
        if auto_id.startswith('RTN:'):
            cfg = self.tabs['routines'].config_of(auto_id)
            if cfg is None:
                return False, 'routine not found'
            ok, msg = self.start_routine(cfg)
            if not ok:
                self.core.events.publish(EventKind.AUTOMATION_START, auto_id, f'Start refused: {msg}', Level.WARNING)
            return ok, msg
        return False, f'unknown automation: {auto_id}'

    def emergency_stop(self, source: str = 'UI'):
        w = self.tabs.get('alignment')
        if w is not None:
            w.release_all_jogs()
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
        for key in ('pid', 'optimizer', 'scan', 'alignment', 'config'):
            w = self.tabs.get(key)
            if w is not None and hasattr(w, 'set_hardware'):
                w.set_hardware()

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
            if a.kind in ('pid', 'optimizer', 'calibration', 'routine'):
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
        w = self.tabs.get('alignment')           # PID setpoints (SP:<id>) exist once the PID tab is loaded
        if w is not None:
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
        w = self.tabs.get('alignment')
        if w is not None:
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
