"""Recording of the laser state: periodic snapshots, bursts around events, journal (L7).

* periodic snapshots (T7.2), optional, adjustable period,
* burst on event (T7.3/T7.4): the circular buffer of the last ``pre_buffer_s`` seconds is saved
  (the *before*), then one snapshot every ``burst_interval_s`` during ``burst_duration_s``,
  all tagged with the id of the triggering event. For a shake, the core publishes the event
  before firing the shaker, so the first burst snapshot is taken before the shot,
* every event of the bus (journal: start/stop, preemption, safety, ML annotations, notes...).
"""
from __future__ import annotations

import datetime as _dt
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Tuple

from ..core.events import Event, EventKind, Level
from .h5writer import H5SessionWriter, KIND_BURST, KIND_MANUAL, KIND_PERIODIC, KIND_PRE
from .snapshot import build_snapshot

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore

BURST_TRIGGERS = {
    'shake': 'Shake piézo',
    'ml_off': 'Perte de mode-lock (ML OFF / Instable)',
    'safety': 'Gel sécurité / arrêt d’urgence',
    'intensity': 'Intensité sous le seuil',
}


def event_matches(trigger: str, ev: Event) -> bool:
    if trigger == 'shake':
        return ev.kind == EventKind.SHAKE
    if trigger == 'ml_off':
        return ev.kind == EventKind.ML and ev.data.get('new') in ('ML_OFF', 'UNSTABLE')
    if trigger == 'safety':
        return ev.kind in (EventKind.SAFETY_HOLD, EventKind.ESTOP)
    if trigger == 'intensity':
        return ev.kind == EventKind.SAFETY_INFO and ev.level >= Level.WARNING
    return False


class Recorder:
    def __init__(self, core: 'LaserCore', folder: Path, settings: Optional[dict] = None,
                 metadata_provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 writer_factory=H5SessionWriter, max_mb: float = 500.0, max_hours: float = 12.0):
        self.core = core
        self.folder = Path(folder)
        self.settings: dict = {}
        self.metadata_provider = metadata_provider or (lambda: {})
        self.writer_factory = writer_factory
        self.max_mb, self.max_hours = max_mb, max_hours
        self.writer: Optional[H5SessionWriter] = None
        self._token: Optional[int] = None
        self._pre: Deque[Tuple[float, Dict[str, float], Dict[str, Any]]] = deque()
        self._next_pre = 0.0
        self._next_periodic = 0.0
        self._bursts: List[dict] = []      # {'event_id', 'end', 'next'}
        self.configure(settings or {})

    # ------------------------------------------------------------------ config
    def configure(self, settings: dict):
        from ..presets.schema import default_recording
        s = default_recording()
        s.update(settings or {})
        self.settings = s
        maxlen = max(int(float(s['pre_buffer_s']) * float(s['pre_rate_hz'])), 1)
        self._pre = deque(self._pre, maxlen=maxlen)

    @property
    def active(self) -> bool:
        return self.writer is not None

    @property
    def current_path(self) -> Optional[Path]:
        return self.writer.current_path if self.writer else None

    # ------------------------------------------------------------------ session
    def start(self) -> Path:
        if self.writer is not None:
            return self.folder
        day = _dt.datetime.now().strftime('%Y-%m-%d')
        base = 'session_' + _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        meta = dict(self.metadata_provider() or {})
        meta['session_start'] = _dt.datetime.now().isoformat(timespec='seconds')
        self.writer = self.writer_factory(self.folder / day, base, meta, max_mb=self.max_mb,
                                          max_hours=self.max_hours)
        self.writer.start()
        self._token = self.core.events.subscribe(self._on_event)
        now = self.core.clock.now()
        self._next_pre = now
        self._next_periodic = now
        self._bursts = []
        self.core.events.publish(EventKind.RECORDING, 'RECORDER', f"Enregistrement démarré ({base})")
        self.snapshot_now(KIND_MANUAL)
        return self.folder / day / f'{base}.h5'

    def stop(self):
        if self.writer is None:
            return
        self.core.events.publish(EventKind.RECORDING, 'RECORDER', "Enregistrement arrêté")
        if self._token is not None:
            self.core.events.unsubscribe(self._token)
            self._token = None
        writer, self.writer = self.writer, None
        writer.close()
        self._bursts = []

    def update_metadata(self):
        if self.writer is not None:
            self.writer.update_metadata(dict(self.metadata_provider() or {}))

    # ------------------------------------------------------------------ snapshots
    def _snap(self):
        return build_snapshot(self.core, bool(self.settings.get('record_raw_channels', True)))

    def snapshot_now(self, kind: int = KIND_MANUAL, event_id: int = -1):
        if self.writer is None:
            return
        values, meta = self._snap()
        self.writer.write_snapshot(self.core.clock.wall(), kind, event_id, values, meta)

    def _on_event(self, ev: Event):
        if self.writer is None:
            return
        self.writer.write_event(ev.as_dict())
        if ev.kind == EventKind.PRESET:
            self.update_metadata()
        if not self.settings.get('burst_enabled', True):
            return
        if any(event_matches(t, ev) for t in self.settings.get('burst_events', [])):
            self._start_burst(ev)

    def _start_burst(self, ev: Event):
        for (t_wall, values, meta) in list(self._pre):          # the "before" of the event
            self.writer.write_snapshot(t_wall, KIND_PRE, ev.id, values, meta)
        values, meta = self._snap()                              # right now (before a shake is fired)
        self.writer.write_snapshot(self.core.clock.wall(), KIND_BURST, ev.id, values, meta)
        now = self.core.clock.now()
        self._bursts.append({'event_id': ev.id, 'end': now + float(self.settings['burst_duration_s']),
                             'next': now + float(self.settings['burst_interval_s'])})

    def tick(self, now: Optional[float] = None):
        if self.writer is None:
            return
        now = self.core.clock.now() if now is None else now
        s = self.settings
        snap = None
        if now >= self._next_pre:
            snap = self._snap()
            self._pre.append((self.core.clock.wall(), snap[0], snap[1]))
            self._next_pre = now + 1.0 / max(float(s['pre_rate_hz']), 0.01)
        if s.get('periodic_enabled', True) and now >= self._next_periodic:
            snap = snap or self._snap()
            self.writer.write_snapshot(self.core.clock.wall(), KIND_PERIODIC, -1, snap[0], snap[1])
            self._next_periodic = now + max(float(s['period_s']), 0.1)
        for b in list(self._bursts):
            if now >= b['next']:
                snap = snap or self._snap()
                self.writer.write_snapshot(self.core.clock.wall(), KIND_BURST, b['event_id'], snap[0], snap[1])
                b['next'] = now + max(float(s['burst_interval_s']), 0.05)
            if now >= b['end']:
                self._bursts.remove(b)

    @property
    def bursts_running(self) -> int:
        return len(self._bursts)
