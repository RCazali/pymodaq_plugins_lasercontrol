"""Cost function of the drift optimiser (unchanged from the historical OptimizationEngine).

Maximize/Minimize terms are normalised by the anchor value measured at the start of each pass,
Target Point terms by the noise sigma, so the total cost is ~O(1) per signal and a deadband
proportional to the number of signals is a reasonable "don't chase noise" threshold.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np

MIN_SCALE = 1e-9


class CostModel:
    def __init__(self, signals: List[dict]):
        # {'role', 'mode' ('Maximize'|'Minimize'|'Target Point'), 'weight', 'p', 'target_value', 'auto_target'}
        self.signals = [s for s in signals if float(s.get('weight', 0) or 0) != 0]
        self.anchor: Dict[str, float] = {}
        self.noise: Dict[str, float] = {}
        self.deadband = 0.1

    @property
    def roles(self) -> List[str]:
        return [s['role'] for s in self.signals]

    def estimate(self, samples: Dict[str, List[float]], deadband_scale: float) -> float:
        for s in self.signals:
            arr = np.asarray(samples.get(s['role'], []), dtype=float)
            arr = arr[np.isfinite(arr)]
            if arr.size == 0:
                raise ValueError(f"no valid measurement for {s['role']}")
            self.anchor[s['role']] = float(np.mean(arr))
            self.noise[s['role']] = max(float(np.std(arr)), MIN_SCALE)
        self.deadband = 0.25 * max(len(self.signals), 1) * max(float(deadband_scale), 1e-6)
        return self.deadband

    def target_for(self, s: dict) -> float:
        if s.get('auto_target', True):
            return self.anchor.get(s['role'], 0.0)
        return float(s.get('target_value', 0.0))

    def cost(self, readings: Dict[str, float]) -> float:
        total = 0.0
        for s in self.signals:
            role = s['role']
            val = readings.get(role)
            if val is None or not math.isfinite(val):
                continue
            mode = s.get('mode', 'Maximize')
            w = float(s.get('weight', 1.0))
            p = float(s.get('p', 2.0))
            scale = max(abs(self.anchor.get(role, 0.0)), MIN_SCALE)
            if mode == 'Maximize':
                total += -w * (val / scale)
            elif mode == 'Minimize':
                total += w * abs(val / scale) ** p
            else:  # 'Target Point'
                target = self.target_for(s)
                noise = self.noise.get(role, MIN_SCALE)
                total += w * (abs(val - target) / noise) ** p
        return total
