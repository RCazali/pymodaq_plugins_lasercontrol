"""Hardware broker: the only door to the hardware (specification T3.1, option O2-A).

Event driven, in the GUI thread: orders are sent without blocking (``move_rel``/``move_abs`` of
the DAQ_Move), the end of a move is followed through ``move_done_signal``, data arrive through
the viewers' ``grab_done_signal``. There is **no command queue**: nothing can survive a crash and
an automation never stacks an order on a busy motor (S10, T4.1).

* automatic orders: :meth:`send_relative` - only called by the core *after* the ownership and
  safety checks (S3-S6),
* manual orders: ``manual_*`` - they first preempt (stop) the automations using the motor
  (D5/S7), then send the order,
* :meth:`stop_all` - emergency stop of *every* actuator of the dashboard (S11).

Motors are identified by their DAQ_Move title (``module``); roles are resolved with the RoleMap.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from .clock import Clock
from .events import EventBus, EventKind, Level
from .log import DedupLogger, module_logger
from .ownership import HUMAN, Ownership
from .ports import ActuatorPort, DetectorPort
from .roles import RoleMap
from .safety import SafetySupervisor
from .signals import SignalSource, is_finite

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import LaserCore

logger = DedupLogger(module_logger(__file__))

STOP_GRACE_S = 5.0     # a move_done arriving shortly after a stop is not an "external move"


@dataclass
class MotorState:
    module: str
    units: str = ''
    position: Optional[float] = None
    commanded: Optional[float] = None
    busy: bool = False
    busy_since: Optional[float] = None
    mover: Optional[str] = None          # 'HUMAN:<source>' or automation id of the running move
    expect_done: bool = False
    jogging: bool = False                # continuous manual jog (native / limit modes)
    last_done_t: Optional[float] = None
    last_value_t: Optional[float] = None
    stop_grace_until: float = -math.inf
    timed_out: bool = False
    initialized: bool = True


def _is_human(mover: Optional[str]) -> bool:
    return bool(mover) and str(mover).startswith(HUMAN)


class HardwareBroker:
    def __init__(self, clock: Clock, events: EventBus, ownership: Ownership,
                 safety: SafetySupervisor, roles: RoleMap, signals: SignalSource):
        self.clock = clock
        self.events = events
        self.ownership = ownership
        self.safety = safety
        self.roles = roles
        self.signals = signals
        self.core: Optional['LaserCore'] = None
        self.actuators: Dict[str, ActuatorPort] = {}
        self.detectors: Dict[str, DetectorPort] = {}
        self.motors: Dict[str, MotorState] = {}
        self.position_listeners: List[Callable[[str, float, bool], None]] = []  # (module, value, done)

    # ------------------------------------------------------------------ ports
    def attach(self, actuators: Dict[str, ActuatorPort], detectors: Dict[str, DetectorPort]):
        """(Re)bind the ports. Existing motor states are kept when the module is still present."""
        self.detach_all()
        self.actuators = dict(actuators)
        self.detectors = dict(detectors)
        old = self.motors
        self.motors = {}
        for name, port in self.actuators.items():
            st = old.get(name) or MotorState(name)
            st.busy, st.expect_done, st.jogging, st.mover = False, False, False, None
            try:
                st.units = port.units() or ''
                st.initialized = port.is_initialized()
                pos = port.position()
                if pos is not None and is_finite(pos):
                    st.position = float(pos)
                    st.commanded = float(pos)
            except Exception as e:  # pragma: no cover
                logger.warning(f'Port {name}: initial read failed: {e}')
            self.motors[name] = st
            port.on_value = (lambda v, n=name: self._on_value(n, v))
            port.on_done = (lambda v, n=name: self._on_done(n, v))
        for name, port in self.detectors.items():
            port.on_frame = (lambda values, n=name: self._on_frame(n, values))

    def detach_all(self):
        for port in list(self.actuators.values()):
            try:
                port.disconnect()
            except Exception:  # pragma: no cover
                pass
            port.on_value = None
            port.on_done = None
        for port in list(self.detectors.values()):
            try:
                port.disconnect()
            except Exception:  # pragma: no cover
                pass
            port.on_frame = None
        self.actuators = {}
        self.detectors = {}

    def modules(self) -> List[str]:
        return list(self.actuators)

    def has_module(self, module: Optional[str]) -> bool:
        return module in self.actuators

    def resolve(self, ref: Optional[str]) -> Optional[str]:
        """Role name or module title -> module title (None if unknown)."""
        if not ref or ref == 'None':
            return None
        module = self.roles.module_for(ref)
        if module is not None:
            return module
        return ref if ref in self.actuators else None

    def role_of(self, module: str) -> str:
        return self.roles.label_for_module(module)

    # ------------------------------------------------------------------ state
    def state(self, module: str) -> Optional[MotorState]:
        return self.motors.get(module)

    def position(self, module: Optional[str]) -> Optional[float]:
        st = self.motors.get(module) if module else None
        return st.position if st else None

    def commanded(self, module: Optional[str]) -> Optional[float]:
        st = self.motors.get(module) if module else None
        return st.commanded if st else None

    def is_busy(self, module: Optional[str]) -> bool:
        st = self.motors.get(module) if module else None
        return bool(st and st.busy)

    def units(self, module: Optional[str]) -> str:
        st = self.motors.get(module) if module else None
        port = self.actuators.get(module) if module else None
        if st is not None and not st.units and port is not None:
            try:
                st.units = port.units() or ''
            except Exception:  # pragma: no cover
                pass
        return st.units if st else ''

    def is_initialized(self, module: Optional[str]) -> bool:
        port = self.actuators.get(module) if module else None
        try:
            return bool(port is not None and port.is_initialized())
        except Exception:  # pragma: no cover
            return False

    def request_positions(self, modules: Optional[Iterable[str]] = None):
        for m in (modules or list(self.actuators)):
            port = self.actuators.get(m)
            if port is not None:
                try:
                    port.request_position()
                except Exception as e:  # pragma: no cover
                    logger.warning(f'{m}: get value failed: {e}')

    # ------------------------------------------------------------------ callbacks
    def _notify_position(self, module: str, value: float, done: bool):
        for cb in list(self.position_listeners):
            try:
                cb(module, value, done)
            except Exception as e:  # pragma: no cover
                logger.warning(f'position listener failed: {e}')

    def _on_value(self, module: str, value):
        st = self.motors.get(module)
        if st is None:
            return
        if value is not None and is_finite(value):
            st.position = float(value)
            st.last_value_t = self.clock.now()
            self._notify_position(module, st.position, False)

    def _on_done(self, module: str, value):
        st = self.motors.get(module)
        if st is None:
            return
        now = self.clock.now()
        if value is not None and is_finite(value):
            st.position = float(value)
        st.last_done_t = now
        was_expected = st.expect_done
        mover = st.mover
        st.busy = False
        st.expect_done = False
        st.jogging = False
        st.busy_since = None
        st.timed_out = False
        if not was_expected and now > st.stop_grace_until:
            # a move we did not order: somebody used the DAQ_Move directly (or another program)
            if self.core is not None:
                self.core.on_external_move(module, st.position)
        if not was_expected or st.commanded is None:
            st.commanded = st.position      # unknown target (external move): follow the reading
        self._notify_position(module, st.position if st.position is not None else math.nan, True)

    def _on_frame(self, detector: str, values: Dict[str, float]):
        self.signals.ingest(detector, values)

    # ------------------------------------------------------------------ automatic (checked by the core)
    def send_relative(self, owner: str, module: str, delta: float) -> bool:
        """Low level relative order for an automation. The core has already checked ownership
        (S6), busy state (no stacking), safety (S3-S5). Returns True if sent."""
        port = self.actuators.get(module)
        st = self.motors.get(module)
        if port is None or st is None:
            return False
        if self.core is not None and self.core.estop:
            return False
        if self.ownership.owner_of(module) != owner:   # defensive S6
            logger.error(f'Refused order on {module}: {owner} is not the owner')
            return False
        if st.busy:                                     # defensive: never stack (T4.1)
            return False
        now = self.clock.now()
        base = st.position if st.position is not None else st.commanded
        st.busy, st.busy_since, st.mover, st.expect_done = True, now, owner, True
        st.commanded = (base if base is not None else 0.0) + float(delta)
        try:
            port.move_rel(float(delta))
        except Exception as e:
            st.busy, st.expect_done, st.mover = False, False, None
            self.events.publish(EventKind.ERROR, 'BROKER', f'Order refused by {module}: {e}', Level.ERROR)
            return False
        return True

    # ------------------------------------------------------------------ manual orders
    def _manual_prologue(self, ref: str, source: str) -> Tuple[Optional[str], Optional[ActuatorPort], str]:
        module = self.resolve(ref)
        if module is None:
            return None, None, f"unknown actuator: {ref}"
        port = self.actuators.get(module)
        if port is None:
            return None, None, f"module missing from the dashboard: {module}"
        if self.core is not None and self.core.estop:
            return None, None, "EMERGENCY STOP active: click 'Re-arm' before any move"
        return module, port, ''

    def _preempt(self, module: str, source: str):
        if self.core is not None:
            self.core.human_preempt(module, source)

    def manual_move_abs(self, ref: str, value: float, source: str = 'manual') -> Tuple[bool, str]:
        module, port, msg = self._manual_prologue(ref, source)
        if module is None:
            return False, msg
        role = self.roles.role_for_module(module)
        refusal = self.safety.check_manual(role, float(value))
        if refusal:
            return False, refusal
        st = self.motors[module]
        if st.position is not None and abs(float(value) - st.position) <= 1e-12 * max(1.0, abs(st.position)):
            return True, 'already in position'   # PyMoDAQ 5.1 sends no move_done for a null move
        self._preempt(module, source)                  # S7: stop the automation BEFORE the order
        st.busy, st.busy_since, st.mover, st.expect_done = True, self.clock.now(), f'{HUMAN}:{source}', True
        st.commanded = float(value)
        try:
            port.move_abs(float(value))
        except Exception as e:
            st.busy, st.expect_done = False, False
            return False, f'{module}: {e}'
        return True, ''

    def manual_move_rel(self, ref: str, delta: float, source: str = 'manual') -> Tuple[bool, str]:
        module, port, msg = self._manual_prologue(ref, source)
        if module is None:
            return False, msg
        if abs(float(delta)) <= 0:
            return True, ''
        role = self.roles.role_for_module(module)
        st = self.motors[module]
        lim = self.safety.limits(role) if role else None
        if lim is not None and self.safety.config.manual_abs_limits and \
                (lim.abs_min is not None or lim.abs_max is not None):
            if st.position is None:
                return False, f"{role}: unknown position, cannot check the limits"
            refusal = self.safety.check_manual(role, st.position + float(delta))
            if refusal:
                return False, refusal
        self._preempt(module, source)
        base = st.position if st.position is not None else (st.commanded or 0.0)
        st.busy, st.busy_since, st.mover, st.expect_done = True, self.clock.now(), f'{HUMAN}:{source}', True
        st.commanded = base + float(delta)
        try:
            port.move_rel(float(delta))
        except Exception as e:
            st.busy, st.expect_done = False, False
            return False, f'{module}: {e}'
        return True, ''

    def manual_jog_native(self, ref: str, direction: int, speed: float,
                          source: str = 'jog') -> Tuple[bool, str]:
        module, port, msg = self._manual_prologue(ref, source)
        if module is None:
            return False, msg
        if not port.supports_native_jog():
            return False, f"{module}: the plugin does not implement jog()"
        self._preempt(module, source)
        st = self.motors[module]
        st.busy, st.busy_since, st.mover, st.expect_done, st.jogging = \
            True, self.clock.now(), f'{HUMAN}:{source}', True, True
        try:
            port.jog(int(direction), float(speed))
        except Exception as e:
            st.busy, st.expect_done, st.jogging = False, False, False
            return False, f'{module}: {e}'
        return True, ''

    def manual_jog_limit(self, ref: str, target: float, speed_path: List[str], speed: float,
                         source: str = 'jog') -> Tuple[bool, str]:
        """'limit' jog: move_abs towards Min/Max, stopped on key release.
        Warning S10: this long move may continue on the controller if PyMoDAQ crashes."""
        module, port, msg = self._manual_prologue(ref, source)
        if module is None:
            return False, msg
        role = self.roles.role_for_module(module)
        refusal = self.safety.check_manual(role, float(target))
        if refusal:
            return False, refusal
        self._preempt(module, source)
        if speed_path:
            port.set_setting(list(speed_path), speed)
        st = self.motors[module]
        st.busy, st.busy_since, st.mover, st.expect_done, st.jogging = \
            True, self.clock.now(), f'{HUMAN}:{source}', True, True
        st.commanded = float(target)
        try:
            port.move_abs(float(target))
        except Exception as e:
            st.busy, st.expect_done, st.jogging = False, False, False
            return False, f'{module}: {e}'
        return True, ''

    def manual_stop(self, ref: str, source: str = 'stop') -> Tuple[bool, str]:
        module = self.resolve(ref)
        if module is None or module not in self.actuators:
            return False, f"unknown actuator: {ref}"
        self._preempt(module, source)
        self._stop_module(module)
        return True, ''

    def _stop_module(self, module: str):
        port = self.actuators.get(module)
        st = self.motors.get(module)
        if port is None or st is None:
            return
        try:
            port.stop()
        except Exception as e:
            self.events.publish(EventKind.ERROR, 'BROKER', f'Stop failed on {module}: {e}', Level.ERROR)
        st.busy, st.expect_done, st.jogging, st.mover = False, False, False, None
        st.busy_since = None
        st.stop_grace_until = self.clock.now() + STOP_GRACE_S
        try:
            port.request_position()
        except Exception:  # pragma: no cover
            pass

    def stop_all(self, reason: str = 'stop'):
        """Stop EVERY actuator of the dashboard (not only the roles), S11."""
        for module in list(self.actuators):
            self._stop_module(module)

    def set_setting(self, ref: str, path: List[str], value) -> bool:
        module = self.resolve(ref)
        port = self.actuators.get(module) if module else None
        return bool(port and port.set_setting(list(path), value))

    def supports_native_jog(self, ref: str) -> bool:
        module = self.resolve(ref)
        port = self.actuators.get(module) if module else None
        try:
            return bool(port and port.supports_native_jog())
        except Exception:  # pragma: no cover
            return False

    # ------------------------------------------------------------------ periodic checks
    def tick(self, now: float, timeout_s: float):
        for module, st in self.motors.items():
            if st.busy and not st.jogging and not st.timed_out and st.busy_since is not None \
                    and not _is_human(st.mover) and now - st.busy_since > timeout_s:
                st.timed_out = True
                if self.core is not None:
                    self.core.on_move_timeout(module, st.mover)

    # ------------------------------------------------------------------ detectors
    def start_grab(self, names: Optional[Iterable[str]] = None):
        for n in (names or list(self.detectors)):
            port = self.detectors.get(n)
            if port is not None and not port.is_grabbing():
                try:
                    port.start_grab()
                except Exception as e:
                    self.events.publish(EventKind.ERROR, 'BROKER', f'Acquisition failed on {n}: {e}',
                                        Level.ERROR)

    def stop_grab(self, names: Optional[Iterable[str]] = None):
        for n in (names or list(self.detectors)):
            port = self.detectors.get(n)
            if port is not None:
                try:
                    port.stop_grab()
                except Exception as e:  # pragma: no cover
                    logger.warning(f'stop grab {n}: {e}')

    def is_grabbing(self, name: str) -> bool:
        port = self.detectors.get(name)
        try:
            return bool(port and port.is_grabbing())
        except Exception:  # pragma: no cover
            return False
