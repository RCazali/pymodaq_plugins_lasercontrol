"""Manual control facade used by the Alignment / XY / overview widgets.

Every manual order goes through the broker, which first stops the automations using the motor
(D5/S7) and applies the roles' absolute limits. Widgets reference **roles** (the Alignment and
XY configurations are stored in presets, so they must be machine independent, T1.2).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore


VIRTUAL_PREFIX = 'SP:'
VIRTUAL_STEP_MS = 100       # a virtual 'move' completes after this delay: 10 steps / s while a key is held


class VirtualActuators:
    """Values that the manual widgets drive like motors, e.g. the setpoint of a PID (``SP:<pid id>``).
    Implemented by the application; the default has none."""

    def names(self) -> List[str]:
        return []

    def value(self, name: str) -> Optional[float]:
        return None

    def set_value(self, name: str, value: float) -> Tuple[bool, str]:
        return False, f'unknown target {name}'

    def units(self, name: str) -> str:
        return ''

    def label(self, name: str) -> str:
        return 'setpoint'


def is_virtual(role: Optional[str]) -> bool:
    return bool(role) and str(role).startswith(VIRTUAL_PREFIX)


class ManualControl:
    def __init__(self, core: 'LaserCore', feedback: Optional[Callable[[str], None]] = None,
                 virtual: Optional[VirtualActuators] = None,
                 defer: Optional[Callable[[int, Callable[[], None]], None]] = None):
        self.core = core
        self.feedback = feedback or (lambda msg: None)
        self.virtual = virtual or VirtualActuators()
        self.defer = defer or (lambda ms, fn: fn())        # the application gives a QTimer.singleShot
        self._virtual_busy: set = set()
        self._listeners: List[Callable[[str, float, bool], None]] = []
        core.broker.position_listeners.append(self._on_position)

    # ------------------------------------------------------------------ roles / modules
    def actuator_roles(self) -> List[str]:
        return sorted(self.core.roles.actuators) + list(self.virtual.names())

    def module(self, role: str) -> Optional[str]:
        return None if is_virtual(role) else self.core.broker.resolve(role)

    def roles_of_module(self, module: str) -> List[str]:
        out = [r.name for r in self.core.roles.actuators.values() if r.module == module]
        if not out and module in self.core.broker.actuators:
            out = [module]
        return out

    def exists(self, role: str) -> bool:
        if is_virtual(role):
            return role in self.virtual.names()
        return self.module(role) is not None

    def position(self, role: str) -> Optional[float]:
        if is_virtual(role):
            return self.virtual.value(role)
        return self.core.broker.position(self.module(role))

    def units(self, role: str) -> str:
        if is_virtual(role):
            return self.virtual.units(role)
        return self.core.broker.units(self.module(role))

    def is_busy(self, role: str) -> bool:
        if is_virtual(role):
            return role in self._virtual_busy
        return self.core.broker.is_busy(self.module(role))

    def owner_label(self, role: str) -> str:
        if is_virtual(role):
            return self.virtual.label(role)
        m = self.module(role)
        return self.core.owner_label(m) if m else '—'

    def supports_native_jog(self, role: str) -> bool:
        if is_virtual(role):
            return False                                   # 'auto' mode -> step
        return self.core.broker.supports_native_jog(role)

    # ------------------------------------------------------------------ virtual actuators
    def notify_virtual(self, role: str):
        """A virtual value changed elsewhere (e.g. setpoint typed in the PID tab): refresh the widgets."""
        v = self.virtual.value(role)
        if v is not None:
            for cb in list(self._listeners):
                cb(role, v, False)

    def _virtual_set(self, role: str, value: float) -> bool:
        """Set a virtual value; it behaves like a short move: busy, then 'done' after VIRTUAL_STEP_MS
        (the step jog of the Alignment tab chains the next step on 'done', like with a motor).
        Never goes through the broker: moving a setpoint does not stop the PID."""
        if role in self._virtual_busy:
            return False
        ok, msg = self.virtual.set_value(role, float(value))
        if not ok:
            self.feedback(f'{role} refused: {msg}')
            return False
        self._virtual_busy.add(role)

        def done(r=role):
            self._virtual_busy.discard(r)
            v = self.virtual.value(r)
            for cb in list(self._listeners):
                cb(r, v, True)
        self.defer(VIRTUAL_STEP_MS, done)
        return True

    # ------------------------------------------------------------------ position listeners
    def add_listener(self, cb: Callable[[str, float, bool], None]):
        """cb(role, value, done) for every role mapped on the module that moved."""
        self._listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _on_position(self, module: str, value: float, done: bool):
        for role in self.roles_of_module(module):
            for cb in list(self._listeners):
                cb(role, value, done)

    def detach(self):
        if self._on_position in self.core.broker.position_listeners:
            self.core.broker.position_listeners.remove(self._on_position)
        self._listeners = []

    # ------------------------------------------------------------------ orders
    def _report(self, res: Tuple[bool, str], what: str) -> bool:
        ok, msg = res
        if not ok and msg:
            self.feedback(f'{what} refused: {msg}')
        return ok

    def move_abs(self, role: str, value: float, source: str = 'manual') -> bool:
        if is_virtual(role):
            return self._virtual_set(role, value)
        return self._report(self.core.broker.manual_move_abs(role, value, source), f'{role} → {value:.6g}')

    def move_rel(self, role: str, delta: float, source: str = 'manual') -> bool:
        if is_virtual(role):
            v = self.virtual.value(role)
            return v is not None and self._virtual_set(role, v + float(delta))
        return self._report(self.core.broker.manual_move_rel(role, delta, source), f'{role} Δ {delta:+.6g}')

    def jog_native(self, role: str, direction: int, speed: float, source: str = 'jog') -> bool:
        if is_virtual(role):
            return self._report((False, 'a setpoint has no native jog: use the step mode'), f'jog {role}')
        return self._report(self.core.broker.manual_jog_native(role, direction, speed, source), f'jog {role}')

    def jog_limit(self, role: str, target: float, speed_path: List[str], speed: float, source: str = 'jog') -> bool:
        if is_virtual(role):
            return self._report((False, 'a setpoint has no limit jog: use the step mode'), f'jog {role}')
        return self._report(self.core.broker.manual_jog_limit(role, target, speed_path, speed, source),
                            f'jog {role}')

    def stop(self, role: str, source: str = 'stop') -> bool:
        if is_virtual(role):
            return True                         # nothing moves; the step chain stops with the key release
        return self._report(self.core.broker.manual_stop(role, source), f'stop {role}')

    def read_positions(self, roles: List[str]):
        for r in roles:
            if is_virtual(r):
                self.notify_virtual(r)
        mods = [m for m in (self.module(r) for r in roles) if m]
        self.core.broker.request_positions(mods)
