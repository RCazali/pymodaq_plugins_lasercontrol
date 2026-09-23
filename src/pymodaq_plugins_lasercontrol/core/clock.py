"""Time sources. The core never calls ``time`` directly so that every rule can be tested with a
fake clock (no Qt, no hardware, cahier des charges T1.4)."""
from __future__ import annotations

import time


class Clock:
    """Monotonic time for durations/ages, wall time for timestamps written to disk."""

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Manually advanced clock used by the tests and the offline simulations."""

    def __init__(self, t0: float = 1000.0, wall0: float = 1.7e9):
        self._t = float(t0)
        self._wall_offset = float(wall0) - float(t0)

    def now(self) -> float:
        return self._t

    def wall(self) -> float:
        return self._t + self._wall_offset

    def advance(self, dt: float) -> float:
        self._t += float(dt)
        return self._t
