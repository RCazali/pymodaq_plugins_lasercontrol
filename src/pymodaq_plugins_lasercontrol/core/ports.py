"""Hardware port interfaces used by the broker.

The broker never sees PyMoDAQ objects: it talks to *ports*. ``hardware/pymodaq_ports.py``
implements them on top of DAQ_Move / DAQ_Viewer, ``core/fakes.py`` implements them for the tests.
This is what makes every safety rule testable without Qt or hardware (T1.4).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional


class ActuatorPort:
    """One physical motor (one DAQ_Move). Callbacks are set by the broker."""

    name: str = ''
    on_value: Optional[Callable[[float], None]] = None   # live value while moving / when read
    on_done: Optional[Callable[[float], None]] = None    # end of a move (any origin)

    def units(self) -> str:
        return ''

    def position(self) -> Optional[float]:
        raise NotImplementedError

    def move_abs(self, value: float) -> None:
        raise NotImplementedError

    def move_rel(self, delta: float) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def request_position(self) -> None:
        pass

    def supports_native_jog(self) -> bool:
        return False

    def jog(self, direction: int, speed: float) -> None:
        raise NotImplementedError

    def set_setting(self, path: List[str], value) -> bool:
        return False

    def is_initialized(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass


class DetectorPort:
    """One detector (one DAQ_Viewer) delivering 0D channels ``{key: value}`` at each frame."""

    name: str = ''
    on_frame: Optional[Callable[[Dict[str, float]], None]] = None

    def start_grab(self) -> None:
        pass

    def stop_grab(self) -> None:
        pass

    def is_grabbing(self) -> bool:
        return False

    def is_initialized(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass
