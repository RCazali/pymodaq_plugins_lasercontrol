"""What a routine does through the application (engines/routine.py › RoutineHost).

A routine only executes: PIDs are started from their PID tab configuration, optimisations from the
Optimization tab settings (the "one pass / periodic / full" mode of the step, or the one of its button).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from ..engines.routine import RoutineHost

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp


class AppRoutineHost(RoutineHost):
    def __init__(self, app: 'LaserControlApp'):
        super().__init__(app.core)
        self.app = app

    def _pid_cfg(self, target: str) -> Optional[dict]:
        tab = self.app.tabs.get('pid')
        return tab.config_of(target) if tab is not None else None

    def _opt_cfg(self, target: str) -> Optional[dict]:
        tab = self.app.tabs.get('optimizer')
        return tab.config_of(target) if tab is not None else None

    def check_target(self, target: str) -> Optional[str]:
        if target.startswith('PID:'):
            return None if self._pid_cfg(target) is not None else f'{target} is not in the PID tab'
        if target.startswith('OPT:'):
            return None if self._opt_cfg(target) is not None else f'{target} is not in the Optimization tab'
        return super().check_target(target)

    def automation(self, target: str):
        if target.startswith('PID:') and self.core.get(target) is None:
            cfg = self._pid_cfg(target)
            if cfg is not None:
                self.app.ensure_pid(cfg)
        return self.core.get(target)

    def set_setpoint(self, target: str, k: int, value: float) -> Tuple[bool, str]:
        """Through the PID tab (applied live, shown, kept in the preset like the SP: axes)."""
        tab = self.app.tabs.get('pid')
        w = tab.widgets.get(target.split(':', 1)[-1]) if tab is not None else None
        if w is None or not 0 <= k < len(w.loops()):
            return False, f'{target}: no signal {k + 1}'
        w.set_setpoint(k, float(value))
        return True, ''

    def start(self, target: str, option: str = '') -> Tuple[bool, str]:
        if target.startswith('PID:'):
            cfg = self._pid_cfg(target)
            return self.app.start_pid(cfg) if cfg is not None else (False, 'PID not found')
        if target.startswith('OPT:'):
            cfg = self._opt_cfg(target)
            if cfg is None:
                return False, 'optimization not found'
            if not cfg['motors'] or not cfg['signals']:
                return False, 'tick at least one motor and one objective signal (Optimization tab)'
            how = option or cfg.get('button_action', 'single')
            cfg['config']['full_optimization'] = how == 'full'
            return self.app.start_optimizer(cfg, single_shot=how != 'periodic')
        return super().start(target, option)
