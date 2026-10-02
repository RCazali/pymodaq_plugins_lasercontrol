"""Realistic-enough simulator of the laser for office tests (specification T1.3).

Shared by the ``LaserSim`` DAQ_Move plugin (one axis per module) and the ``LaserSim`` 0D
detector plugin: both run in the same dashboard process, so a thread-safe singleton is enough.

Model (all positions in µm):

* the optimum position of the cavity mirrors drifts slowly (sinusoid + random walk, ~1 min),
* camera centroids (px) are linear combinations of the misalignments (axis coupling),
* the laser power is a Gaussian of the cavity misalignment, with random intensity drops,
* the XUV flux ~ power^3 x Gaussian of the gas-jet position,
* the frame counter mimics the heartbeat channel of computer B (O9-A),
* the detector can be *muted* (computer B silent) or *frozen* (frames keep coming, frozen values).

Two independent test benches, to try the PIDs and the optimiser on something obvious:

* **beam steering**: a mirror (STEER_H / STEER_V) moves a beam on a position detector,
  ``Beam/x`` = STEER_H − drift (µm), ``Beam/y`` idem. A PID with setpoint 0, positive sign, keeps
  the beam centred (Kp ≈ 0.5, Ki ≈ 0.05);
* **photodiode**: a lens (PD_X / PD_Y) focuses on a photodiode behind a pinhole,
  ``PD/current`` (mA) = 2 · exp(−r² / 2·12²) with r the distance to a drifting optimum (µm).
  The optimiser maximises it (probe step ≈ 1 µm, max step ≈ 3 µm).

Two coupled test benches, to try coupled (MIMO) control (see ``COUPLED_SIMPLE`` / ``BEAM_WALK``):

* **coupled mount** (simple, moderate coupling): a mount whose two screws CPL_A / CPL_B are not aligned
  with the detector axes, ``Coupled/x`` = 1.0·A + 0.35·B, ``Coupled/y`` = −0.25·A + 1.0·B. Two independent
  PIDs still work, slower; a coupled PID converges in one step;
* **beam walk** (complex, strong coupling): two steering mirrors BW1 and BW2 (H and V each) seen by a
  near-field and a far-field camera. Both mirrors move both cameras (condition number ≈ 6 per plane)
  and a slightly rotated mount adds an H ↔ V cross-coupling. Independent PIDs pairing one mirror with
  one camera fight each other (slow, may oscillate): this is the case the coupled PID is made for.

A hard aperture on the beam of the beam walk, to try the routines (pause the PIDs, optimise, resume):

* **hard aperture (HA)**: ``HA/flux`` (mW) = 10 · exp(−(HA_X² + HA_Y²) / 2·6²) · exp(−(far x² + far y²) / 2·4²),
  with HA_X / HA_Y its position (drifting optimum) and far x / y the angle of the beam. Optimising it raises
  the flux but shifts the **position** of the beam a little (near field, 0.2 µm per µm: ``Near/x``,
  ``Near/y``), not its angle (far field). So the near / far PID re-locks after an HA optimisation, and a PID
  correcting during the optimisation would change the flux the optimiser measures: it runs with the PID
  paused.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, Optional

import numpy as np

AXES = ['OC_H', 'OC_V', 'EM_H', 'EM_V', 'AP_X', 'AP_Y', 'JET_X', 'JET_Y', 'JET_Z', 'KERR',
        'STEER_H', 'STEER_V', 'PD_X', 'PD_Y', 'CPL_A', 'CPL_B', 'BW1_H', 'BW1_V', 'BW2_H', 'BW2_V',
        'HA_X', 'HA_Y']
#: drift amplitude of each axis, relative to ``drift_amplitude`` (0.3 for the axes not listed)
DRIFT_FACTOR = {'OC_H': 1.0, 'OC_V': 1.0, 'EM_H': 1.0, 'EM_V': 1.0,
                'STEER_H': 1.3, 'STEER_V': 1.0, 'PD_X': 0.8, 'PD_Y': 0.8,
                'CPL_A': 1.0, 'CPL_B': 0.8, 'BW1_H': 0.8, 'BW1_V': 0.6, 'BW2_H': 0.6, 'BW2_V': 0.8,
                'HA_X': 1.2, 'HA_Y': 1.0}
#: sensitivity matrices of the coupled benches: {signal: {axis: gain}} (µm of spot per µm of screw)
COUPLED_SIMPLE = {'Coupled/x': {'CPL_A': 1.0, 'CPL_B': 0.35},
                  'Coupled/y': {'CPL_A': -0.25, 'CPL_B': 1.0}}
BEAM_WALK = {'Near/x': {'BW1_H': 1.0, 'BW2_H': 0.8, 'BW1_V': 0.15, 'HA_X': 0.2},
             'Far/x': {'BW1_H': 1.0, 'BW2_H': 1.5, 'BW2_V': 0.15},
             'Near/y': {'BW1_V': 1.0, 'BW2_V': 0.8, 'BW1_H': -0.15, 'HA_Y': 0.2},
             'Far/y': {'BW1_V': 1.0, 'BW2_V': 1.5, 'BW2_H': -0.15}}
PD_WIDTH = 12.0             # µm
PD_MAX = 2.0                # mA
HA_MAX = 10.0               # mW, flux through the hard aperture when it is centred on the mode
HA_WIDTH = 6.0              # µm, tolerance of the hard aperture position (HA_X, HA_Y)
HA_POINTING_WIDTH = 4.0     # µm, tolerance on the beam angle (far field)
UNITS = 'µm'


class LaserSimModel:
    _instance: Optional['LaserSimModel'] = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> 'LaserSimModel':
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self, seed: Optional[int] = None, clock=time.monotonic):
        self._lock = threading.RLock()
        self._clock = clock
        self.rng = np.random.default_rng(seed)
        now = clock()
        self.t0 = now
        self._start = {a: 0.0 for a in AXES}
        self._target = {a: 0.0 for a in AXES}
        self._t_start = {a: now for a in AXES}
        self.speed = {a: 50.0 for a in AXES}               # µm/s
        # environment
        self.drift_amplitude = 6.0      # µm
        self.drift_period_s = 120.0
        self.random_walk = 0.15         # µm / sqrt(s)
        self.noise = 1.0                # multiplies every noise level
        self.dropout_rate_per_min = 0.3
        self.dropout_depth = 0.9
        self.dropout_duration_s = 2.0
        self.base_power = 50.0          # W
        self.width = 15.0               # µm, cavity misalignment tolerance
        self.muted = False
        self.frozen = False
        self._walk = {a: 0.0 for a in AXES}
        self._walk_t = now
        self._phase = {a: float(self.rng.uniform(0, 2 * math.pi)) for a in AXES}
        self._drop_until = -1.0
        self._frame = 0
        self._last_frame: Optional[Dict[str, Dict[str, float]]] = None

    # ------------------------------------------------------------------ motion
    def _pos(self, axis: str, now: float) -> float:
        start, target = self._start[axis], self._target[axis]
        dist = target - start
        travelled = self.speed[axis] * max(now - self._t_start[axis], 0.0)
        if travelled >= abs(dist):
            return target
        return start + math.copysign(travelled, dist)

    def position(self, axis: str) -> float:
        with self._lock:
            return self._pos(axis, self._clock())

    def is_moving(self, axis: str) -> bool:
        with self._lock:
            return self._pos(axis, self._clock()) != self._target[axis]

    def move_to(self, axis: str, target: float, speed: Optional[float] = None):
        with self._lock:
            now = self._clock()
            self._start[axis] = self._pos(axis, now)
            self._target[axis] = float(target)
            self._t_start[axis] = now
            if speed is not None:
                self.speed[axis] = max(float(speed), 1e-6)

    def stop(self, axis: str):
        with self._lock:
            now = self._clock()
            p = self._pos(axis, now)
            self._start[axis] = self._target[axis] = p
            self._t_start[axis] = now

    def jog(self, axis: str, direction: int, velocity: float):
        self.move_to(axis, self.position(axis) + math.copysign(1e6, direction), speed=abs(velocity))

    def set_speed(self, axis: str, speed: float):
        with self._lock:
            now = self._clock()
            self._start[axis] = self._pos(axis, now)
            self._t_start[axis] = now
            self.speed[axis] = max(float(speed), 1e-6)

    # ------------------------------------------------------------------ optics
    def optimum(self, now: float) -> Dict[str, float]:
        dt = max(now - self._walk_t, 0.0)
        if dt > 0:
            for a in self._walk:
                self._walk[a] += float(self.rng.normal(0.0, self.random_walk * math.sqrt(dt)))
                self._walk[a] *= math.exp(-dt / 600.0)       # keep the walk bounded (10 min memory)
            self._walk_t = now
        t = now - self.t0
        opt = {}
        for i, a in enumerate(AXES):
            amp = self.drift_amplitude * DRIFT_FACTOR.get(a, 0.3)
            period = self.drift_period_s * (1.0 + 0.37 * i)
            opt[a] = amp * math.sin(2 * math.pi * t / period + self._phase[a]) + self._walk[a]
        return opt

    def frame(self) -> Optional[Dict[str, Dict[str, float]]]:
        """One detector frame: {data name: {label: value}}. None when muted (B silent)."""
        with self._lock:
            if self.muted:
                return None
            if self.frozen and self._last_frame is not None:
                return {k: dict(v) for k, v in self._last_frame.items()}
            now = self._clock()
            pos = {a: self._pos(a, now) for a in AXES}
            opt = self.optimum(now)
            m = {a: pos[a] - opt[a] for a in AXES}
            n = self.noise
            u = m['OC_H'] + 0.6 * m['EM_H']
            v = m['OC_V'] + 0.6 * m['EM_V']
            aperture = math.exp(-((m['AP_X']) ** 2 + (m['AP_Y']) ** 2) / (2 * 40.0 ** 2))
            if self.dropout_rate_per_min > 0 and now > self._drop_until:
                if self.rng.random() < self.dropout_rate_per_min / 60.0 * 0.5:   # ~2 Hz frames
                    self._drop_until = now + self.dropout_duration_s * float(self.rng.uniform(0.5, 1.5))
            drop = (1.0 - self.dropout_depth) if now < self._drop_until else 1.0
            power = self.base_power * math.exp(-(u ** 2 + v ** 2) / (2 * self.width ** 2)) * aperture * drop
            power *= 1.0 + float(self.rng.normal(0, 0.01 * n))
            jet = math.exp(-(m['JET_X'] ** 2 + m['JET_Y'] ** 2) / (2 * 8.0 ** 2) - m['JET_Z'] ** 2 / (2 * 20.0 ** 2))
            xuv = 1e-3 * max(power, 0.0) ** 3 / self.base_power ** 2 * jet * (1 + float(self.rng.normal(0, 0.03 * n)))
            sigma_px = 0.2 * n
            cam1 = {'x': 1.0 * m['OC_H'] + 0.3 * m['EM_H'] + float(self.rng.normal(0, sigma_px)),
                    'y': 1.0 * m['OC_V'] + 0.3 * m['EM_V'] + float(self.rng.normal(0, sigma_px)),
                    'sum': max(power, 0.0) * 1e3}
            cam2 = {'x': 0.4 * m['OC_H'] + 1.2 * m['EM_H'] + float(self.rng.normal(0, sigma_px)),
                    'y': 0.4 * m['OC_V'] + 1.2 * m['EM_V'] + float(self.rng.normal(0, sigma_px)),
                    'sum': max(power, 0.0) * 8e2}
            beam = {'x': m['STEER_H'] + float(self.rng.normal(0, 0.15 * n)),
                    'y': m['STEER_V'] + float(self.rng.normal(0, 0.15 * n))}
            r2 = m['PD_X'] ** 2 + m['PD_Y'] ** 2
            pd = PD_MAX * math.exp(-r2 / (2 * PD_WIDTH ** 2)) * (1 + float(self.rng.normal(0, 0.01 * n)))
            self._frame += 1
            out = {'Cam1': cam1, 'Cam2': cam2, 'Laser': {'power': max(power, 0.0)},
                   'XUV': {'flux': max(xuv, 0.0)}, 'Beam': beam, 'PD': {'current': max(pd, 0.0)},
                   'Stats': {'frame_counter': float(self._frame)}}
            for matrix in (COUPLED_SIMPLE, BEAM_WALK):
                for key, row in matrix.items():
                    data, label = key.split('/')
                    value = sum(g * m[a] for a, g in row.items()) + float(self.rng.normal(0, 0.15 * n))
                    out.setdefault(data, {})[label] = value
            far = [sum(g * m[a] for a, g in BEAM_WALK[k].items()) for k in ('Far/x', 'Far/y')]
            ha = HA_MAX * math.exp(-(m['HA_X'] ** 2 + m['HA_Y'] ** 2) / (2 * HA_WIDTH ** 2)
                                   - (far[0] ** 2 + far[1] ** 2) / (2 * HA_POINTING_WIDTH ** 2))
            out['HA'] = {'flux': max(ha * (1 + float(self.rng.normal(0, 0.01 * n))), 0.0)}
            self._last_frame = out
            return {k: dict(v) for k, v in out.items()}
