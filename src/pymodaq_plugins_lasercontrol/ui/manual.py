"""Manual control facade used by the Alignment / XY / overview widgets.

Every manual order goes through the broker, which first stops the automations using the motor
(D5/S7) and applies the roles' absolute limits. Widgets reference **roles** (the Alignment and
XY configurations are stored in presets, so they must be machine independent, T1.2).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore


class ManualControl:
    def __init__(self, core: 'LaserCore', feedback: Optional[Callable[[str], None]] = None):
        self.core = core
        self.feedback = feedback or (lambda msg: None)
        self._listeners: List[Callable[[str, float, bool], None]] = []
        core.broker.position_listeners.append(self._on_position)

    # ------------------------------------------------------------------ roles / modules
    def actuator_roles(self) -> List[str]:
        return sorted(self.core.roles.actuators)

    def module(self, role: str) -> Optional[str]:
        return self.core.broker.resolve(role)

    def roles_of_module(self, module: str) -> List[str]:
        out = [r.name for r in self.core.roles.actuators.values() if r.module == module]
        if not out and module in self.core.broker.actuators:
            out = [module]
        return out

    def exists(self, role: str) -> bool:
        return self.module(role) is not None

    def position(self, role: str) -> Optional[float]:
        return self.core.broker.position(self.module(role))

    def units(self, role: str) -> str:
        return self.core.broker.units(self.module(role))

    def is_busy(self, role: str) -> bool:
        return self.core.broker.is_busy(self.module(role))

    def owner_label(self, role: str) -> str:
        m = self.module(role)
        return self.core.owner_label(m) if m else '—'

    def supports_native_jog(self, role: str) -> bool:
        return self.core.broker.supports_native_jog(role)

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
        return self._report(self.core.broker.manual_move_abs(role, value, source), f'{role} → {value:.6g}')

    def move_rel(self, role: str, delta: float, source: str = 'manual') -> bool:
        return self._report(self.core.broker.manual_move_rel(role, delta, source), f'{role} Δ {delta:+.6g}')

    def jog_native(self, role: str, direction: int, speed: float, source: str = 'jog') -> bool:
        return self._report(self.core.broker.manual_jog_native(role, direction, speed, source), f'jog {role}')

    def jog_limit(self, role: str, target: float, speed_path: List[str], speed: float, source: str = 'jog') -> bool:
        return self._report(self.core.broker.manual_jog_limit(role, target, speed_path, speed, source),
                            f'jog {role}')

    def stop(self, role: str, source: str = 'stop') -> bool:
        return self._report(self.core.broker.manual_stop(role, source), f'stop {role}')

    def read_positions(self, roles: List[str]):
        mods = [m for m in (self.module(r) for r in roles) if m]
        self.core.broker.request_positions(mods)
