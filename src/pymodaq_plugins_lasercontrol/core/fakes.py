"""In-memory ports for the tests and offline simulations (no Qt, no hardware)."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .ports import ActuatorPort, DetectorPort


class FakeActuator(ActuatorPort):
    """Motor whose moves complete when :meth:`finish` is called (or immediately if ``instant``).

    Every call is appended to ``log`` so that the tests can check the order of operations
    (e.g. S7: the automation is stopped *before* the manual order is sent).
    """

    def __init__(self, name: str, position: float = 0.0, units: str = 'um', instant: bool = False,
                 native_jog: bool = False, log: Optional[List[Tuple]] = None):
        self.name = name
        self._pos = float(position)
        self._target: Optional[float] = None
        self._units = units
        self.instant = instant
        self.native_jog = native_jog
        self.log: List[Tuple] = log if log is not None else []
        self.on_value = None
        self.on_done = None
        self.stopped = 0
        self.fail_next = False

    def units(self) -> str:
        return self._units

    def position(self) -> Optional[float]:
        return self._pos

    def _start(self, target: float):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError('simulated failure')
        self._target = float(target)
        if self.instant:
            self.finish()

    def move_abs(self, value: float):
        self.log.append(('move_abs', self.name, float(value)))
        self._start(value)

    def move_rel(self, delta: float):
        self.log.append(('move_rel', self.name, float(delta)))
        self._start(self._pos + float(delta))

    def stop(self):
        self.log.append(('stop', self.name))
        self.stopped += 1
        self._target = None

    def request_position(self):
        if self.on_value:
            self.on_value(self._pos)

    def supports_native_jog(self) -> bool:
        return self.native_jog

    def jog(self, direction: int, speed: float):
        self.log.append(('jog', self.name, direction, speed))

    def set_setting(self, path, value) -> bool:
        self.log.append(('set_setting', self.name, tuple(path), value))
        return True

    # test helpers
    @property
    def moving(self) -> bool:
        return self._target is not None

    def finish(self, position: Optional[float] = None):
        """Complete the running move (optionally at another position) and emit move_done."""
        if position is not None:
            self._pos = float(position)
        elif self._target is not None:
            self._pos = self._target
        self._target = None
        if self.on_done:
            self.on_done(self._pos)

    def external_move(self, position: float):
        """Somebody moved the motor from the DAQ_Move UI (not through the broker)."""
        self._pos = float(position)
        if self.on_done:
            self.on_done(self._pos)


class FakeDetector(DetectorPort):
    def __init__(self, name: str):
        self.name = name
        self.on_frame = None
        self.grabbing = False

    def start_grab(self):
        self.grabbing = True

    def stop_grab(self):
        self.grabbing = False

    def is_grabbing(self) -> bool:
        return self.grabbing

    def emit(self, values: Dict[str, float]):
        if self.on_frame:
            self.on_frame(dict(values))
