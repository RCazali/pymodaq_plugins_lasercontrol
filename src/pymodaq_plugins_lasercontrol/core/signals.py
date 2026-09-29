"""Latest 0D values with freshness and validity (specification T3.2, options O9-A, S1, S12).

Detectors keep grabbing continuously; every frame they deliver is pushed here. Automations
never trigger grabs: they read the latest value and its age. A value is *fresh* when:

* it is finite (a failed read is NaN, never 0.0 - S12),
* it was received less than ``max_age_s`` ago,
* and, when a heartbeat channel (frame counter of computer B, O9-A) is declared for its
  detector, that counter advanced during the last ``max_age_s`` seconds (frozen data are stale
  even if frames keep arriving).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .clock import Clock


def is_finite(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


@dataclass
class Sample:
    value: float
    t: float          # monotonic reception time
    wall: float       # wall clock reception time
    seq: int          # per channel frame counter (increments at every received frame)
    detector: str


class History:
    """Bounded (time, value) buffer used by the live curves and the passive monitor (T4.3)."""

    def __init__(self, maxlen: int = 6000):
        self.t: Deque[float] = deque(maxlen=maxlen)
        self.v: Deque[float] = deque(maxlen=maxlen)

    def append(self, t: float, v: float):
        self.t.append(t)
        self.v.append(v)

    def arrays(self, since: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
        t = np.fromiter(self.t, dtype=float, count=len(self.t))
        v = np.fromiter(self.v, dtype=float, count=len(self.v))
        if since is not None and len(t):
            i = int(np.searchsorted(t, since))
            t, v = t[i:], v[i:]
        return t, v

    def __len__(self):
        return len(self.t)


@dataclass
class DetectorStatus:
    name: str
    frames: int = 0
    last_frame_t: Optional[float] = None
    rate_hz: float = 0.0
    heartbeat_channel: Optional[str] = None
    heartbeat_value: Optional[float] = None
    heartbeat_change_t: Optional[float] = None
    age: float = math.inf
    heartbeat_age: Optional[float] = None
    state: str = 'unknown'     # 'ok' | 'warn' | 'stale' | 'unknown'
    reason: str = ''


class SignalSource:
    WARN_FRACTION = 0.6

    def __init__(self, clock: Optional[Clock] = None, max_age_s: float = 1.0,
                 history_len: int = 6000):
        self.clock = clock or Clock()
        self.max_age_s = float(max_age_s)
        self._samples: Dict[str, Sample] = {}
        self._aliases: Dict[str, str] = {}          # 'Det/Name' -> 'Det/Name/label0' (legacy keys)
        self._detectors: Dict[str, DetectorStatus] = {}
        self._channel_det: Dict[str, str] = {}
        self._heartbeats: Dict[str, str] = {}
        self._history: Dict[str, History] = {}
        self._history_len = history_len
        self.frames_total = 0

    # ------------------------------------------------------------------ config
    def set_max_age(self, max_age_s: float):
        self.max_age_s = max(float(max_age_s), 0.05)

    def set_heartbeats(self, mapping: Dict[str, str]):
        """``{detector title: heartbeat channel key}`` (O9-A)."""
        self._heartbeats = {str(k): str(v) for k, v in (mapping or {}).items() if v}
        for det, st in self._detectors.items():
            ch = self._heartbeats.get(det)
            if ch != st.heartbeat_channel:
                # a new counter must be SEEN advancing: start from its last known value
                last = self.latest(ch) if ch else None
                st.heartbeat_channel = ch
                st.heartbeat_value = last.value if last is not None and is_finite(last.value) else None
                st.heartbeat_change_t = None

    # ------------------------------------------------------------------ input
    def ingest(self, detector: str, values: Dict[str, float], t: Optional[float] = None,
               wall: Optional[float] = None):
        """One frame of one detector: ``{channel key: value}``."""
        t = self.clock.now() if t is None else t
        wall = self.clock.wall() if wall is None else wall
        st = self._detectors.get(detector)
        if st is None:
            st = DetectorStatus(detector, heartbeat_channel=self._heartbeats.get(detector))
            self._detectors[detector] = st
        if st.last_frame_t is not None:
            dt = t - st.last_frame_t
            if dt > 1e-6:
                inst = 1.0 / dt
                st.rate_hz = inst if st.rate_hz <= 0 else 0.8 * st.rate_hz + 0.2 * inst
        st.frames += 1
        st.last_frame_t = t
        self.frames_total += 1

        for key, raw in values.items():
            try:
                v = float(raw)
            except (TypeError, ValueError):
                v = math.nan
            prev = self._samples.get(key)
            seq = prev.seq + 1 if prev is not None else 1
            self._samples[key] = Sample(v, t, wall, seq, detector)
            self._channel_det[key] = detector
            hist = self._history.get(key)
            if hist is None:
                hist = self._history[key] = History(self._history_len)
            hist.append(wall, v)
            parts = key.split('/')
            if len(parts) >= 3:
                alias = '/'.join(parts[:2])
                self._aliases.setdefault(alias, key)

        hb = st.heartbeat_channel
        if hb:
            hb_key = self.resolve(hb)
            if hb_key in values:
                v = self._samples[hb_key].value
                if is_finite(v) and (st.heartbeat_value is None or v != st.heartbeat_value):
                    st.heartbeat_value = v
                    st.heartbeat_change_t = t

    # ------------------------------------------------------------------ queries
    def resolve(self, key: Optional[str]) -> Optional[str]:
        if key is None:
            return None
        if key in self._samples:
            return key
        return self._aliases.get(key, key)

    def latest(self, key: Optional[str]) -> Optional[Sample]:
        k = self.resolve(key)
        return self._samples.get(k) if k is not None else None

    def value(self, key: Optional[str]) -> float:
        s = self.latest(key)
        return s.value if s is not None else math.nan

    def age(self, key: Optional[str]) -> float:
        s = self.latest(key)
        return self.clock.now() - s.t if s is not None else math.inf

    def detector_of(self, key: Optional[str]) -> Optional[str]:
        k = self.resolve(key)
        return self._channel_det.get(k) if k else None

    def channels(self) -> List[str]:
        return sorted(self._samples)

    def detectors(self) -> List[str]:
        return sorted(self._detectors)

    def known(self, key: Optional[str]) -> bool:
        return self.latest(key) is not None

    def history(self, key: Optional[str]) -> Optional[History]:
        k = self.resolve(key)
        return self._history.get(k) if k else None

    def detector_status(self, name: str) -> DetectorStatus:
        st = self._detectors.get(name)
        if st is None:
            st = DetectorStatus(name, heartbeat_channel=self._heartbeats.get(name))
            st.state, st.reason = 'unknown', 'no frame received'
            return st
        now = self.clock.now()
        st.age = now - st.last_frame_t if st.last_frame_t is not None else math.inf
        st.heartbeat_age = (now - st.heartbeat_change_t) if st.heartbeat_change_t is not None else None
        if st.last_frame_t is None:
            st.state, st.reason = 'unknown', 'no frame received'
        elif st.age > self.max_age_s:
            st.state, st.reason = 'stale', f'no frame for {st.age:.1f} s'
        elif st.heartbeat_channel and st.heartbeat_age is None:
            st.state, st.reason = 'stale', 'frame counter not yet seen advancing'
        elif st.heartbeat_channel and st.heartbeat_age > self.max_age_s:
            st.state, st.reason = 'stale', f'frame counter frozen for {st.heartbeat_age:.1f} s'
        elif st.age > self.WARN_FRACTION * self.max_age_s:
            st.state, st.reason = 'warn', f'frame is {st.age:.1f} s old'
        else:
            st.state, st.reason = 'ok', ''
        return st

    def freshness(self, key: Optional[str]) -> Tuple[bool, str]:
        """(fresh, reason). Reason is a short text for the UI / events."""
        s = self.latest(key)
        if s is None:
            return False, f'{key}: never received'
        if not is_finite(s.value):
            return False, f'{key}: invalid value (NaN)'
        age = self.clock.now() - s.t
        if age > self.max_age_s:
            return False, f'{key}: stale ({age:.1f} s)'
        st = self.detector_status(s.detector)
        if st.state == 'stale':
            return False, f'{key}: {st.reason}'
        return True, ''

    def is_fresh(self, key: Optional[str]) -> bool:
        return self.freshness(key)[0]

    def all_fresh(self, keys: Iterable[str]) -> Tuple[bool, List[str]]:
        reasons = []
        for k in keys:
            ok, why = self.freshness(k)
            if not ok:
                reasons.append(why)
        return not reasons, reasons

    def snapshot_values(self) -> Dict[str, float]:
        return {k: s.value for k, s in self._samples.items()}
