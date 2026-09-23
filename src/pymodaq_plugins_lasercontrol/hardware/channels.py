"""DataToExport -> ``{channel key: float}`` (pure function, no Qt).

Channel key = ``origin/data_name/label`` (e.g. ``LECO_B/Cam1/x``). The historical extension used
``origin/data_name`` and silently read the *first* channel only; such legacy keys are still
accepted by the SignalSource (they resolve to the first channel of the DataWithAxes).

S12: a value that cannot be read is NaN, never 0.0. Non scalar data (1D/2D/ND) are ignored.
"""
from __future__ import annotations

import math
from typing import Dict

import numpy as np


def _clean(part: str) -> str:
    return str(part).replace('/', '_').strip() or '_'


def channel_key(origin: str, name: str, label: str) -> str:
    return f'{_clean(origin)}/{_clean(name)}/{_clean(label)}'


def extract_scalars(dte, detector: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if dte is None:
        return out
    try:
        items = list(dte)
    except Exception:
        return out
    for dwa in items:
        try:
            arrays = list(dwa.data)
        except Exception:
            continue
        if not arrays:
            continue
        sizes = []
        for a in arrays:
            try:
                sizes.append(int(np.asarray(a).size))
            except Exception:
                sizes.append(1)            # unreadable channel of a 0D dataset -> NaN below
        if any(s != 1 for s in sizes):
            continue                       # not 0D
        name = getattr(dwa, 'name', '') or 'data'
        origin = getattr(dwa, 'origin', '') or detector
        try:
            labels = list(getattr(dwa, 'labels', []) or [])
        except Exception:
            labels = []
        for i, arr in enumerate(arrays):
            label = labels[i] if i < len(labels) and labels[i] else f'CH{i:02d}'
            try:
                v = float(np.asarray(arr).ravel()[0])
            except Exception:
                v = math.nan
            out[channel_key(origin, name, label)] = v
    return out
