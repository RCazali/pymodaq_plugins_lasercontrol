"""Timestamped event bus (specification T3.6).

Every noteworthy thing (start/stop, preemption, safety trigger, shake, ML annotation, error...)
is published here once. Consumers (UI journal, HDF5 recorder, B trigger files, future
notifications) subscribe without knowing about each other.
"""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from .clock import Clock
from .log import DedupLogger, module_logger

logger = DedupLogger(module_logger(__file__))


class Level(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ALARM = 40      # safety related (hold, estop, timeout...)
    ERROR = 50


class EventKind:
    """Event kinds used throughout the extension (plain strings, easy to store in HDF5)."""
    AUTOMATION_STATE = 'automation.state'
    AUTOMATION_START = 'automation.start'
    AUTOMATION_STOP = 'automation.stop'
    PREEMPTION = 'preemption'
    SAFETY_HOLD = 'safety.hold'
    SAFETY_RESUME = 'safety.resume'
    SAFETY_INFO = 'safety.info'
    ESTOP = 'estop'
    ESTOP_RESET = 'estop.reset'
    MOVE_MANUAL = 'move.manual'
    MOVE_EXTERNAL = 'move.external'
    MOVE_TIMEOUT = 'move.timeout'
    DATA = 'data'                 # data freshness transitions
    SHAKE = 'shake'
    ML = 'ml'                     # O6-A manual mode-lock annotation
    NOTE = 'note'                 # free text annotation (T7.5)
    PRESET = 'preset'
    RECORDING = 'recording'
    OPTIMIZER = 'optimizer'
    SCAN = 'scan'
    ERROR = 'error'
    INFO = 'info'


@dataclass
class Event:
    id: int
    t: float                 # wall clock (s since epoch)
    kind: str
    source: str
    message: str
    level: Level = Level.INFO
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {'id': self.id, 't': self.t, 'kind': self.kind, 'source': self.source,
                'message': self.message, 'level': int(self.level), 'data': dict(self.data)}


Callback = Callable[[Event], None]


class EventBus:
    """Synchronous publish/subscribe. Callbacks run in the publisher thread (the GUI thread in
    this extension, option O2-A). A failing subscriber never breaks the publisher."""

    def __init__(self, clock: Optional[Clock] = None, history: int = 5000, log: bool = True):
        self.clock = clock or Clock()
        self._ids = itertools.count(1)
        self._subs: Dict[int, tuple] = {}
        self._tokens = itertools.count(1)
        self.history: Deque[Event] = deque(maxlen=history)
        self._log = log

    def subscribe(self, callback: Callback, kinds: Optional[Iterable[str]] = None,
                  min_level: Level = Level.DEBUG) -> int:
        token = next(self._tokens)
        self._subs[token] = (callback, set(kinds) if kinds else None, min_level)
        return token

    def unsubscribe(self, token: int):
        self._subs.pop(token, None)

    def publish(self, kind: str, source: str, message: str, level: Level = Level.INFO,
                **data) -> Event:
        ev = Event(next(self._ids), self.clock.wall(), kind, source, message, Level(level), data)
        self.history.append(ev)
        if self._log:
            text = f'[{source}] {message}'
            if ev.level >= Level.ERROR:
                logger.error(text)
            elif ev.level >= Level.WARNING:
                logger.warning(text)
            elif ev.level >= Level.INFO:
                logger.info(text)
        for token, (cb, kinds, min_level) in list(self._subs.items()):
            if kinds is not None and kind not in kinds:
                continue
            if ev.level < min_level:
                continue
            try:
                cb(ev)
            except Exception as e:  # pragma: no cover - defensive
                logger.error(f'Event subscriber failed on {kind}: {e}')
        return ev

    def recent(self, n: int = 100, min_level: Level = Level.DEBUG) -> List[Event]:
        out = [e for e in self.history if e.level >= min_level]
        return out[-n:]
