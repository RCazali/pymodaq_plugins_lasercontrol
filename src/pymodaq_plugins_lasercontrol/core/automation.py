"""Base class of every automation (PID, optimiser, scans...).

Engines are driven by :meth:`LaserCore.tick` in the GUI thread (option O2-A). They never block
and never talk to the hardware: they ask the core to move (``core.request_moves``) and read the
latest data (``core.signals``). Their math lives in pure functions/classes (engines package).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .safety import SafetyPolicy, Violation
from .states import AutoState

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import LaserCore


class Automation:
    kind = 'generic'
    #: acquire the motors at start and keep them until stop (False for the periodic optimiser,
    #: which takes them only during a pass)
    acquire_at_start = True

    def __init__(self, auto_id: str, label: str = ''):
        self.id = auto_id
        self.label = label or auto_id
        self.state: AutoState = AutoState.OFF
        self.reason: str = ''
        self.period_s: float = 0.5
        self.next_due: float = 0.0
        self.pause_kind: Optional[str] = None      # 'preempted' | 'confirm'
        self.paused_by: Optional[str] = None
        self.last_error: str = ''
        self.started_at: Optional[float] = None

    # ------------------------------------------------------------------ description
    def motor_roles(self) -> List[str]:
        """Roles this automation may move (ownership + safety). Keep stable while engaged."""
        return []

    def wanted_motor_roles(self) -> List[str]:
        """Roles it needs to own *now* (default: all). The optimiser returns [] between passes."""
        return self.motor_roles()

    def signal_roles(self) -> List[str]:
        """Roles whose freshness is required before every tick (S1)."""
        return []

    def policy(self) -> SafetyPolicy:
        return SafetyPolicy()

    def explicit_ranges(self, core: 'LaserCore') -> Optional[Dict[str, Tuple[float, float]]]:
        """For policy.envelope == 'explicit': {module: (lo, hi)}."""
        return None

    def summary(self) -> str:
        return self.label

    # ------------------------------------------------------------------ lifecycle hooks
    def validate(self, core: 'LaserCore') -> List[str]:
        return []

    def on_start(self, core: 'LaserCore') -> Optional[str]:
        """Return an error message to abort the start."""
        return None

    def on_stop(self, core: 'LaserCore') -> None:
        pass

    def on_pause(self, core: 'LaserCore') -> None:
        pass

    def on_resume(self, core: 'LaserCore') -> None:
        pass

    def on_hold(self, core: 'LaserCore', violation: Violation) -> None:
        pass

    def tick(self, core: 'LaserCore', now: float) -> None:
        pass

    def is_done(self) -> bool:
        return False

    def variables(self) -> Dict[str, float]:
        """Internal variables recorded in every snapshot (T7.1)."""
        return {}
