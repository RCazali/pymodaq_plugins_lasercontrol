"""Session loader for the analysis (specification T9.2).

>>> from pymodaq_plugins_lasercontrol.recording.loader import load_session
>>> s = load_session('session_20260923_101500.h5')
>>> s.values['sig/Intensity_main']      # numpy array, one value per snapshot
>>> df = s.snapshots_dataframe()         # pandas (optional dependency)
>>> ev = s.events_dataframe()
>>> s.burst(event_id=42)                 # the snapshots of one burst (before + after)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from .h5writer import FORMAT_NAME, unescape


@dataclass
class SessionData:
    path: Path
    attrs: Dict[str, Any] = field(default_factory=dict)
    t: np.ndarray = field(default_factory=lambda: np.zeros(0))
    kind: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    event_id: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    meta: List[dict] = field(default_factory=list)
    values: Dict[str, np.ndarray] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)

    def burst(self, event_id: int) -> Dict[str, np.ndarray]:
        idx = np.where(self.event_id == event_id)[0]
        out = {'t': self.t[idx], 'kind': self.kind[idx]}
        out.update({k: v[idx] for k, v in self.values.items()})
        return out

    def snapshots_dataframe(self):
        import pandas as pd
        df = pd.DataFrame(self.values)
        df.insert(0, 'event_id', self.event_id)
        df.insert(0, 'kind', self.kind)
        df.insert(0, 'time', pd.to_datetime(self.t, unit='s'))
        return df

    def events_dataframe(self):
        import pandas as pd
        df = pd.DataFrame(self.events)
        if len(df):
            df['time'] = pd.to_datetime(df['t'], unit='s')
        return df


def _s(v) -> str:
    return v.decode('utf-8') if isinstance(v, bytes) else str(v)


def load_session(path) -> SessionData:
    import h5py
    path = Path(path)
    out = SessionData(path)
    with h5py.File(path, 'r') as f:
        for k, v in f.attrs.items():
            v = _s(v) if isinstance(v, (bytes, str)) else v
            if isinstance(v, str) and v[:1] in '{[':
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            out.attrs[k] = v
        if out.attrs.get('format') != FORMAT_NAME:
            raise ValueError(f'{path.name} is not a Laser Control session file')
        g = f['snapshots']
        out.t = g['t'][()]
        out.kind = g['kind'][()].astype(int)
        out.event_id = g['event_id'][()].astype(int)
        out.meta = [json.loads(_s(m)) for m in g['meta'][()]]
        for esc, ds in g['values'].items():
            out.values[_s(ds.attrs.get('name', unescape(esc)))] = ds[()]
        e = f['events']
        n = e['t'].shape[0]
        for i in range(n):
            out.events.append({'t': float(e['t'][i]), 'id': int(e['id'][i]), 'level': int(e['level'][i]),
                               'kind': _s(e['kind'][i]), 'source': _s(e['source'][i]),
                               'message': _s(e['message'][i]), 'data': json.loads(_s(e['data'][i]) or '{}')})
    return out
