"""Event trigger files for computer B (option O8-A, T7.6).

On selected events (shake, ML lost...) a small JSON file is written in a folder shared with
computer B. B's extension (or a tiny watcher script) saves a few timestamped camera images when a
new file appears. Nothing is sent over the network by this extension; no file = no image.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from ..core.events import Event, EventKind, Level
from .recorder import event_matches

if TYPE_CHECKING:  # pragma: no cover
    from ..core.events import EventBus


class EventTriggerWriter:
    def __init__(self, events: 'EventBus', folder: Optional[Path], triggers: Iterable[str] = ('shake',)):
        self.events = events
        self.folder = Path(folder) if folder else None
        self.triggers = list(triggers)
        self._token = None
        self.last_file: Optional[Path] = None
        self.error = ''
        if self.folder is not None:
            self._token = events.subscribe(self._on_event)

    def set_triggers(self, triggers: Iterable[str]):
        self.triggers = list(triggers)

    def close(self):
        if self._token is not None:
            self.events.unsubscribe(self._token)
            self._token = None

    def _on_event(self, ev: Event):
        if not any(event_matches(t, ev) for t in self.triggers):
            return
        stamp = _dt.datetime.fromtimestamp(ev.t).strftime('%Y%m%d_%H%M%S_%f')[:-3]
        name = f"event_{stamp}_{ev.id}_{ev.kind.replace('.', '_')}.json"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / name
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(ev.as_dict(), ensure_ascii=False, default=str), encoding='utf-8')
            tmp.replace(path)
            self.last_file = path
            self.error = ''
        except Exception as e:
            self.error = str(e)
            if ev.kind != EventKind.ERROR:
                self.events.publish(EventKind.ERROR, 'TRIGGER', f"Fichier déclencheur non écrit : {e}",
                                    Level.WARNING)
