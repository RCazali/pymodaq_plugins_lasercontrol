"""State snapshot of the laser (specification T7.1).

Generic by construction (O7): every declared role (actuator or 0D signal), every actuator of the
dashboard, every 0D channel received and every automation's internal variables are included
automatically. Adding a sensor later = add a module in the dashboard (+ a role): no code change.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Dict, Tuple

from ..core.coordinator import ML_CODES
from ..core.states import AutoState, GlobalState

if TYPE_CHECKING:  # pragma: no cover
    from ..core.coordinator import LaserCore

STATE_CODES = {s: float(i) for i, s in enumerate(AutoState)}
GLOBAL_CODES = {s: float(i) for i, s in enumerate(GlobalState)}
DET_STATE_CODES = {'unknown': math.nan, 'ok': 0.0, 'warn': 1.0, 'stale': 2.0}


def _num(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return math.nan
    return v if math.isfinite(v) else math.nan


def build_snapshot(core: 'LaserCore', record_raw: bool = True) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Returns (numeric values, metadata). Numeric names use '/' as separator."""
    values: Dict[str, float] = {}
    broker, signals, roles = core.broker, core.signals, core.roles
    role_modules = set()
    for role, ar in roles.actuators.items():
        m = ar.module
        role_modules.add(m)
        st = broker.state(m)
        values[f'act/{role}/pos'] = _num(broker.position(m))
        values[f'act/{role}/cmd'] = _num(broker.commanded(m))
        values[f'act/{role}/busy'] = 1.0 if st is not None and st.busy else 0.0
    for m in broker.modules():
        if m not in role_modules:
            values[f'mod/{m}/pos'] = _num(broker.position(m))
            values[f'mod/{m}/cmd'] = _num(broker.commanded(m))
    for role, sr in roles.signals.items():
        values[f'sig/{role}'] = _num(signals.value(sr.channel))
        values[f'sig/{role}/age'] = _num(signals.age(sr.channel))
    if record_raw:
        for ch in signals.channels():
            values[f'raw/{ch}'] = _num(signals.value(ch))
    for det in signals.detectors():
        st = signals.detector_status(det)
        values[f'det/{det}/age'] = _num(st.age)
        values[f'det/{det}/rate_hz'] = _num(st.rate_hz)
        values[f'det/{det}/state'] = DET_STATE_CODES.get(st.state, math.nan)
        if st.heartbeat_channel:
            values[f'det/{det}/heartbeat'] = _num(st.heartbeat_value)
    for a in core.automations.values():
        values[f'auto/{a.id}/state'] = STATE_CODES.get(a.state, math.nan)
        try:
            for k, v in (a.variables() or {}).items():
                values[f'auto/{a.id}/{k}'] = _num(v)
        except Exception:  # pragma: no cover - a broken engine must not break the recording
            pass
        refs = core.safety.references(a.id)
        for m, ref in refs.items():
            role = roles.label_for_module(m)
            pos = broker.position(m)
            if pos is not None:
                values[f'auto/{a.id}/excursion/{role}'] = _num(pos - ref)
    guard = core.safety.guard
    values['safety/global_state'] = GLOBAL_CODES.get(core.global_state, math.nan)
    values['safety/estop'] = 1.0 if core.estop else 0.0
    values['safety/intensity'] = _num(guard.value())
    values['safety/intensity_ratio'] = _num(guard.ratio())
    values['safety/intensity_reference'] = _num(guard.reference)
    values['ml_state'] = ML_CODES.get(core.ml_state, math.nan)
    meta = {
        'global': core.global_state.value,
        'estop': core.estop,
        'ml': core.ml_state,
        'preset': core.preset_name,
        'preset_hash': core.preset_hash,
        'owners': {roles.label_for_module(m): o for m, o in core.ownership.table().items()},
        'automations': {a.id: {'state': a.state.value, 'reason': a.reason} for a in core.automations.values()
                        if a.state != AutoState.OFF},
        'intensity_guard': core.safety.guard.evaluate(),
    }
    return values, meta
