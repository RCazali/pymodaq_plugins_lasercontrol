"""Non-blocking HDF5 session writer (option O4-A, T7.2).

One file per session, extensible tables for snapshots and events, metadata as attributes.
The GUI thread only puts items in a queue; a background thread writes and flushes every few
seconds (writing must never block the GUI nor the PIDs). Files rotate on size or duration.

Layout::

    /                         attrs: format, code_version, git_hash, machine, operator,
                                     preset_name, preset_hash, preset_json, roles_json, ...
    /snapshots/t              float64  wall clock (s since epoch)
    /snapshots/kind           int8     0 periodic, 1 burst, 2 before-event buffer, 3 manual
    /snapshots/event_id       int64    id of the triggering event (-1 if none)
    /snapshots/meta           str      JSON (owners, states, ML, preset...)
    /snapshots/values/<name>  float64  one dataset per variable ('/' written as '|'), NaN padded
    /events/{t,id,level,kind,source,message,data}
"""
from __future__ import annotations

import json
import math
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from ..core.log import DedupLogger, module_logger

logger = DedupLogger(module_logger(__file__))

KIND_PERIODIC, KIND_BURST, KIND_PRE, KIND_MANUAL = 0, 1, 2, 3
FORMAT_NAME = 'laser_control_session'
FORMAT_VERSION = 1


def escape(name: str) -> str:
    return str(name).replace('/', '|')


def unescape(name: str) -> str:
    return str(name).replace('|', '/')


class H5SessionWriter:
    def __init__(self, folder: Path, base_name: str, metadata: Optional[Dict[str, Any]] = None,
                 max_mb: float = 500.0, max_hours: float = 12.0, flush_s: float = 2.0):
        self.folder = Path(folder)
        self.base_name = base_name
        self.metadata = dict(metadata or {})
        self.max_bytes = max(float(max_mb) * 1e6, 1.0)
        self.max_s = max(float(max_hours), 0.01) * 3600.0
        self.flush_s = flush_s
        self._q: 'queue.Queue' = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._file = None
        self._file_index = 0
        self._opened_at = 0.0
        self.current_path: Optional[Path] = self.folder / f'{base_name}.h5'   # file created at first flush
        self.paths: List[Path] = []
        self.n_snapshots = 0
        self.n_events = 0
        self.error: str = ''
        self._stop = threading.Event()

    # ------------------------------------------------------------------ API (GUI thread)
    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='LaserControlH5Writer', daemon=True)
        self._thread.start()

    def write_snapshot(self, t: float, kind: int, event_id: int, values: Dict[str, float], meta: Dict[str, Any]):
        self._q.put(('snap', (float(t), int(kind), int(event_id), dict(values), meta)))

    def write_event(self, ev: Dict[str, Any]):
        self._q.put(('event', dict(ev)))

    def update_metadata(self, metadata: Dict[str, Any]):
        self._q.put(('meta', dict(metadata)))

    def close(self, timeout: float = 10.0):
        if self._thread is None:
            return
        self._q.put(('close', None))
        self._thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ writer thread
    def _run(self):
        snaps: List[tuple] = []
        events: List[dict] = []
        last_flush = time.monotonic()
        closing = False
        while not closing:
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is not None:
                kind, payload = item
                if kind == 'snap':
                    snaps.append(payload)
                elif kind == 'event':
                    events.append(payload)
                elif kind == 'meta':
                    self.metadata.update(payload)
                    self._write_attrs()
                elif kind == 'close':
                    closing = True
            if closing or len(snaps) + len(events) > 200 or time.monotonic() - last_flush > self.flush_s:
                try:
                    if snaps or events:
                        self._ensure_file()
                        self._write_snapshots(snaps)
                        self._write_events(events)
                        self._file.flush()
                        self._rotate_if_needed()
                    self.error = ''
                except Exception as e:  # keep running: a disk problem must not kill the extension
                    self.error = str(e)
                    logger.error(f'HDF5 write failed: {e}')
                snaps, events = [], []
                last_flush = time.monotonic()
        self._close_file()

    def _new_path(self) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        suffix = '' if self._file_index == 0 else f'_{self._file_index + 1:03d}'
        return self.folder / f'{self.base_name}{suffix}.h5'

    def _ensure_file(self):
        if self._file is not None:
            return
        import h5py
        path = self._new_path()
        self._file = h5py.File(path, 'a')
        self._opened_at = time.monotonic()
        self.current_path = path
        self.paths.append(path)
        f = self._file
        str_dt = h5py.string_dtype('utf-8')
        g = f.require_group('snapshots')
        for name, dt in (('t', 'f8'), ('kind', 'i1'), ('event_id', 'i8'), ('meta', str_dt)):
            if name not in g:
                g.create_dataset(name, shape=(0,), maxshape=(None,), dtype=dt, chunks=(512,))
        g.require_group('values')
        e = f.require_group('events')
        for name, dt in (('t', 'f8'), ('id', 'i8'), ('level', 'i2'), ('kind', str_dt), ('source', str_dt),
                         ('message', str_dt), ('data', str_dt)):
            if name not in e:
                e.create_dataset(name, shape=(0,), maxshape=(None,), dtype=dt, chunks=(256,))
        self._write_attrs()

    def _write_attrs(self):
        if self._file is None:
            return
        attrs = self._file.attrs
        attrs['format'] = FORMAT_NAME
        attrs['format_version'] = FORMAT_VERSION
        attrs['file_index'] = self._file_index + 1
        for k, v in self.metadata.items():
            try:
                if isinstance(v, (dict, list)):
                    attrs[k] = json.dumps(v, ensure_ascii=False, default=str)
                elif v is None:
                    attrs[k] = ''
                else:
                    attrs[k] = v
            except Exception:
                attrs[k] = str(v)

    def _write_snapshots(self, snaps: List[tuple]):
        if not snaps:
            return
        g = self._file['snapshots']
        n0 = g['t'].shape[0]
        n = len(snaps)
        for name, col in (('t', [s[0] for s in snaps]), ('kind', [s[1] for s in snaps]),
                          ('event_id', [s[2] for s in snaps]),
                          ('meta', [json.dumps(s[4], ensure_ascii=False, default=str) for s in snaps])):
            ds = g[name]
            ds.resize((n0 + n,))
            ds[n0:n0 + n] = col
        vg = g['values']
        names = set()
        for s in snaps:
            names.update(s[3].keys())
        existing = set(vg.keys())
        for esc in existing:
            names.discard(unescape(esc))
        for name in names:                      # a variable appearing mid-session: NaN before
            ds = vg.create_dataset(escape(name), shape=(n0,), maxshape=(None,), dtype='f8', chunks=(512,),
                                   fillvalue=np.nan)
            if n0:
                ds[:] = np.nan
            ds.attrs['name'] = name
        for esc in vg.keys():
            name = unescape(esc)
            ds = vg[esc]
            col = np.array([s[3].get(name, math.nan) for s in snaps], dtype=float)
            ds.resize((n0 + n,))
            ds[n0:n0 + n] = col
        self.n_snapshots += n

    def _write_events(self, events: List[dict]):
        if not events:
            return
        e = self._file['events']
        n0 = e['t'].shape[0]
        n = len(events)
        cols = {
            't': [float(ev.get('t', 0.0)) for ev in events],
            'id': [int(ev.get('id', -1)) for ev in events],
            'level': [int(ev.get('level', 20)) for ev in events],
            'kind': [str(ev.get('kind', '')) for ev in events],
            'source': [str(ev.get('source', '')) for ev in events],
            'message': [str(ev.get('message', '')) for ev in events],
            'data': [json.dumps(ev.get('data', {}), ensure_ascii=False, default=str) for ev in events],
        }
        for name, col in cols.items():
            ds = e[name]
            ds.resize((n0 + n,))
            ds[n0:n0 + n] = col
        self.n_events += n

    def _rotate_if_needed(self):
        if self._file is None or self.current_path is None:
            return
        size = self.current_path.stat().st_size if self.current_path.exists() else 0
        if size > self.max_bytes or time.monotonic() - self._opened_at > self.max_s:
            self._close_file()
            self._file_index += 1

    def _close_file(self):
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception as e:  # pragma: no cover
                logger.error(f'HDF5 close failed: {e}')
            self._file = None
