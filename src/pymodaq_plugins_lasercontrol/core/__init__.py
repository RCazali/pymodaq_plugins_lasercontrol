"""Core layer of Laser Control: pure Python (no Qt, no PyMoDAQ object), testable offline.

Golden rule (specification, section 5): engines and UI never touch ``modules_manager``,
``selected_*_name`` or ``connect_*``. Every hardware access goes through the broker.
"""
from .clock import Clock, FakeClock
from .events import EventBus, Event, EventKind, Level
from .states import AutoState, GlobalState
from .roles import RoleMap, ActuatorRole, SignalRole
from .signals import SignalSource, Sample
from .ownership import Ownership, HUMAN
from .safety import (SafetyConfig, SafetyPolicy, SafetySupervisor, ActuatorLimits,
                     IntensityGuardConfig, Violation)
from .broker import HardwareBroker
from .automation import Automation
from .coordinator import LaserCore, ML_STATES, ML_LABELS, ML_CODES
