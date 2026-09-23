"""PyMoDAQ implementation of the broker ports.

**This is the only module of the extension that touches DAQ_Move / DAQ_Viewer objects or the
modules manager** (cahier des charges T3.1, acceptance: "plus aucun appel direct à
modules_manager hors du broker"). It only reads ``actuators_all`` / ``detectors_all``; it never
changes ``selected_*_name`` nor calls ``connect_*``.

Compatible with PyMoDAQ 5.1.x and 5.2.x (checked: move_abs to the current position emits no
move_done in 5.1 but does in 5.2 - the broker never sends such a null move).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..core.log import DedupLogger, module_logger
from ..core.ports import ActuatorPort, DetectorPort
from .channels import extract_scalars

logger = DedupLogger(module_logger(__file__))

try:  # PyMoDAQ >= 5
    from pymodaq_utils.utils import ThreadCommand
except ImportError:  # pragma: no cover
    from pymodaq.utils.daq_utils import ThreadCommand


def data_to_float(data) -> Optional[float]:
    """DataActuator (or number) -> float, None if not convertible (S12: never a fake 0.0)."""
    try:
        if hasattr(data, 'value'):
            return float(data.value())
        return float(data)
    except Exception:
        return None


def _plugin_of(mod):
    """Best-effort read-only access to the plugin instance living in the hardware thread
    (capability detection only: does it implement ``jog``?). 5.1: hardware.hardware, 5.2: .plugin"""
    hw = getattr(getattr(mod, '_hardware_thread', None), 'hardware', None)
    return getattr(hw, 'plugin', None) or getattr(hw, 'hardware', None)


class DAQMovePort(ActuatorPort):
    def __init__(self, mod):
        self.mod = mod
        self.name = str(mod.title)
        self.on_value = None
        self.on_done = None
        self._connections: List[Tuple[object, object]] = []
        for sig_name, slot in (('current_value_signal', self._value_slot), ('move_done_signal', self._done_slot)):
            sig = getattr(mod, sig_name, None)
            if sig is not None:
                try:
                    sig.connect(slot)
                    self._connections.append((sig, slot))
                except Exception as e:  # pragma: no cover
                    logger.warning(f'{self.name}: cannot connect {sig_name}: {e}')

    # signals (GUI thread)
    def _value_slot(self, data):
        if self.on_value is not None:
            self.on_value(data_to_float(data))

    def _done_slot(self, data):
        if self.on_done is not None:
            self.on_done(data_to_float(data))

    def disconnect(self):
        for sig, slot in self._connections:
            try:
                sig.disconnect(slot)
            except Exception:
                pass
        self._connections = []

    # queries
    def units(self) -> str:
        try:
            return str(self.mod.units or '')
        except Exception:
            return ''

    def is_initialized(self) -> bool:
        try:
            return bool(self.mod.initialized_state)
        except Exception:
            return True

    def position(self) -> Optional[float]:
        if not self.is_initialized():
            return None
        return data_to_float(getattr(self.mod, '_current_value', None))

    # orders
    def move_abs(self, value: float):
        self.mod.move_abs(float(value))

    def move_rel(self, delta: float):
        self.mod.move_rel(float(delta))

    def stop(self):
        for name in ('stop_motion', 'stop'):      # 5.x: stop_motion (the historical bug of the E-STOP)
            fn = getattr(self.mod, name, None)
            if callable(fn):
                fn()
                return
        raise RuntimeError(f'{self.name}: no stop method')

    def request_position(self):
        self.mod.get_actuator_value()

    def supports_native_jog(self) -> bool:
        try:
            return callable(getattr(_plugin_of(self.mod), 'jog', None))
        except Exception:
            return False

    def jog(self, direction: int, speed: float):
        self.mod.command_hardware.emit(ThreadCommand('jog', [int(direction), float(speed)]))

    def set_setting(self, path, value) -> bool:
        group = getattr(self.mod, '_hw_settings_name', 'move_settings')
        try:
            self.mod.settings.child(group, *path).setValue(value)
            return True
        except Exception as e:
            logger.warning(f"{self.name}: parameter {'/'.join(path)} not found: {e}")
            return False


class DAQViewerPort(DetectorPort):
    def __init__(self, mod):
        self.mod = mod
        self.name = str(mod.title)
        self.on_frame = None
        self._connected = False
        sig = getattr(mod, 'grab_done_signal', None)
        if sig is not None:
            sig.connect(self._frame_slot)
            self._connected = True

    def _frame_slot(self, dte):
        values = extract_scalars(dte, self.name)
        if values and self.on_frame is not None:
            self.on_frame(values)

    def disconnect(self):
        if self._connected:
            try:
                self.mod.grab_done_signal.disconnect(self._frame_slot)
            except Exception:
                pass
            self._connected = False

    def is_initialized(self) -> bool:
        try:
            return bool(self.mod.initialized_state)
        except Exception:
            return True

    def is_grabbing(self) -> bool:
        try:
            return bool(self.mod.grab_state)
        except Exception:
            return False

    def start_grab(self):
        if self.is_grabbing() or not self.is_initialized():
            return
        if getattr(self.mod, 'ui', None) is not None and callable(getattr(self.mod, 'grab', None)):
            self.mod.grab()                       # keeps the viewer's Grab button in sync
        else:
            self.mod.grab_data(True)

    def stop_grab(self):
        self.mod.stop_grab()


def discover_ports(modules_manager) -> Tuple[Dict[str, DAQMovePort], Dict[str, DAQViewerPort]]:
    """Ports for every actuator/detector of the dashboard (read-only use of the modules manager)."""
    acts: Dict[str, DAQMovePort] = {}
    dets: Dict[str, DAQViewerPort] = {}
    if modules_manager is None:
        return acts, dets
    for mod in list(getattr(modules_manager, 'actuators_all', []) or []):
        try:
            acts[str(mod.title)] = DAQMovePort(mod)
        except Exception as e:  # pragma: no cover
            logger.warning(f'actuator port failed: {e}')
    for mod in list(getattr(modules_manager, 'detectors_all', []) or []):
        try:
            dets[str(mod.title)] = DAQViewerPort(mod)
        except Exception as e:  # pragma: no cover
            logger.warning(f'detector port failed: {e}')
    return acts, dets
