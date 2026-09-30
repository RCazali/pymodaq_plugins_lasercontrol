"""Unified, versioned preset schema (specification T6.1, T6.2).

One JSON file replaces the four historical formats (session, PID profiles, optimiser profiles,
XY profiles). A preset only references **roles** (T1.2) so it loads unchanged on the office
(mock) and lab machines.

Loading a preset never starts anything (S9). :func:`validate_preset` tells, automation by
automation, what prevents an activation (missing role, module absent from the dashboard,
safety limits not configured...).
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ..core.roles import RoleMap
from ..core.safety import SafetyConfig
from ..engines.optimizer.automation import DEFAULT_OPT_CONFIG
from ..engines.pid import PIDSettings

SCHEMA_VERSION = 2
META_KEYS = ('name', 'description', 'status', 'validated_on', 'validated_by', 'validation_notes',
             'validated_hash', 'created', 'modified', 'schema_version')
STATUSES = ('draft', 'validated')


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec='seconds')


def default_recording() -> dict:
    return {
        'periodic_enabled': True,      # T7.2
        'period_s': 10.0,
        'burst_enabled': True,         # T7.3 / T7.4
        'burst_events': ['shake', 'ml_off'],
        'pre_buffer_s': 60.0,          # circular buffer saved with each burst (before the event)
        'pre_rate_hz': 2.0,
        'burst_duration_s': 30.0,
        'burst_interval_s': 1.0,
        'record_raw_channels': True,   # also every 0D channel without a role
        'trigger_events': ['shake'],   # O8: events written as trigger files for computer B
    }


#: action of an optimisation button of the Alignment tab
OPT_BUTTON_ACTIONS = ('single', 'periodic', 'full')
OPT_BUTTON_ACTION_LABELS = {'single': 'one pass', 'periodic': 'periodic', 'full': 'full optimization'}


def default_pid(pid_id: str = 'PID_1') -> dict:
    return {'id': pid_id, 'label': '', 'in_stabilization': True, 'show_in_alignment': False, 'signal': '',
            'motors': {}, 'settings': PIDSettings().to_dict(), 'calibration': {}}


def default_optimizer(opt_id: str = 'Optim_1') -> dict:
    return {'id': opt_id, 'label': '', 'show_in_alignment': False, 'button_action': 'single',
            'config': dict(DEFAULT_OPT_CONFIG), 'motors': [], 'signals': []}


def default_preset(name: str = 'New preset') -> dict:
    t = now_iso()
    return {
        'schema_version': SCHEMA_VERSION,
        'name': name,
        'description': '',
        'status': 'draft',
        'validated_on': None,
        'validated_by': '',
        'validation_notes': '',
        'validated_hash': '',
        'created': t,
        'modified': t,
        'conditions': {'regime': 'CW', 'pump_power': '', 'notes': ''},
        'safety': SafetyConfig().to_dict(),
        'pids': [],
        'optimizers': [],
        'scan': {'role': '', 'start': -10.0, 'stop': 10.0, 'step': 1.0, 'dwell': 0.2, 'samples': 1,
                 'channels': [], 'relative': True, 'return_to_start': True, 'check_intensity': False},
        'recording': default_recording(),
        'alignment': {},
        'xy_groups': [],
        'overview': {'plot_roles': []},
    }


def _deep_fill(target: dict, defaults: dict):
    for k, v in defaults.items():
        if k not in target:
            target[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(target[k], dict) and k not in ('motors', 'actuators'):
            _deep_fill(target[k], v)


def normalize(preset: dict) -> dict:
    """Fill missing keys with defaults, coerce the PID settings, migrate old schema versions."""
    p = migrate(copy.deepcopy(preset or {}))
    _deep_fill(p, default_preset(p.get('name', 'preset')))
    p['safety'] = SafetyConfig.from_dict(p.get('safety', {})).to_dict()
    pids = []
    for i, pid in enumerate(p.get('pids', []) or []):
        d = default_pid(f'PID_{i + 1}')
        d.update({k: v for k, v in pid.items() if k != 'settings'})
        d['settings'] = PIDSettings.from_dict(pid.get('settings', {})).to_dict()
        d['motors'] = {str(r): float(w) for r, w in (pid.get('motors', {}) or {}).items()}
        pids.append(d)
    p['pids'] = pids
    opts = []
    for i, opt in enumerate(p.get('optimizers', []) or []):
        d = default_optimizer(f'Optim_{i + 1}')
        d.update({k: v for k, v in opt.items() if k != 'config'})
        cfg = dict(DEFAULT_OPT_CONFIG)
        cfg.update(opt.get('config', {}) or {})
        cfg['full_optimization'] = False
        d['config'] = cfg
        d['motors'] = [dict(m) for m in d.get('motors') or []]
        d['signals'] = [dict(x) for x in d.get('signals') or []]
        if d.get('button_action') not in OPT_BUTTON_ACTIONS:
            d['button_action'] = 'single'
        opts.append(d)
    p['optimizers'] = opts
    if p.get('status') not in STATUSES:
        p['status'] = 'draft'
    return p


def migrate(p: dict) -> dict:
    """Upgrade older schema versions in place. Version 1 is the first one."""
    version = int(p.get('schema_version', SCHEMA_VERSION) or SCHEMA_VERSION)
    if version > SCHEMA_VERSION:
        raise ValueError(f"preset created by a newer version (schema {version})")
    if version < 2:
        # v1 had a single optimiser ('optimizer'); v2 has named optimisations ('optimizers')
        old = p.pop('optimizer', None) or {}
        if 'optimizers' not in p:
            p['optimizers'] = []
            if old.get('motors') or old.get('signals'):
                d = default_optimizer('Optimizer')
                d.update({k: old[k] for k in ('config', 'motors', 'signals') if k in old})
                p['optimizers'].append(d)
    p.pop('optimizer', None)
    p['schema_version'] = SCHEMA_VERSION
    return p


def content_hash(preset: dict) -> str:
    """Hash of the configuration content (metadata excluded), written in every recording."""
    content = {k: v for k, v in preset.items() if k not in META_KEYS}
    txt = json.dumps(content, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(txt.encode('utf-8')).hexdigest()[:12]


def roles_used(preset: dict) -> Dict[str, List[str]]:
    acts, sigs = set(), set()
    for pid in preset.get('pids', []):
        acts.update(r for r, w in (pid.get('motors') or {}).items() if w)
        if pid.get('signal'):
            sigs.add(pid['signal'])
    for opt in preset.get('optimizers', []) or []:
        acts.update(m['role'] for m in opt.get('motors', []) if m.get('role'))
        sigs.update(s['role'] for s in opt.get('signals', []) if s.get('role'))
    sc = preset.get('scan', {})
    if sc.get('role'):
        acts.add(sc['role'])
    sigs.update(sc.get('channels', []) or [])
    inten = preset.get('safety', {}).get('intensity', {})
    if inten.get('role'):
        sigs.add(inten['role'])
    for ax in (preset.get('alignment', {}) or {}).get('axes', []) or []:
        for k in ('h', 'v'):
            ch = ax.get(k) if isinstance(ax, dict) else None
            if ch and ch.get('actuator') not in (None, '', 'None') and not str(ch['actuator']).startswith('SP:'):
                acts.add(ch['actuator'])       # 'SP:<pid>' = setpoint of a PID, not a motor role
    for g in preset.get('xy_groups', []) or []:
        for k in ('actuator_x', 'actuator_y'):
            if g.get(k) not in (None, '', 'None') and not str(g[k]).startswith('SP:'):
                acts.add(g[k])
    return {'actuators': sorted(acts), 'signals': sorted(sigs)}


@dataclass
class PresetReport:
    errors: List[str] = field(default_factory=list)                       # whole preset
    warnings: List[str] = field(default_factory=list)
    automations: Dict[str, List[str]] = field(default_factory=dict)       # id -> blocking problems

    @property
    def ok(self) -> bool:
        return not self.errors and not any(self.automations.values())

    def blocking(self, auto_id: str) -> List[str]:
        return self.errors + self.automations.get(auto_id, [])

    def text(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"✖ {e}")
        for a, errs in self.automations.items():
            for e in errs:
                lines.append(f"✖ {a}: {e}")
        for w in self.warnings:
            lines.append(f"⚠ {w}")
        return '\n'.join(lines) if lines else '✔ preset consistent with this machine'


def validate_preset(preset: dict, roles: RoleMap, modules: Iterable[str] = (),
                    channels: Iterable[str] = ()) -> PresetReport:
    """T6.2: check a preset against this machine. Nothing is started here (S9)."""
    rep = PresetReport()
    modules = set(modules)
    channels = set(channels)
    safety = SafetyConfig.from_dict(preset.get('safety', {}))

    def actuator_problems(role: str) -> List[str]:
        out = []
        if not role:
            return out
        if not roles.is_actuator(role):
            out.append(f"motor role '{role}' missing from the roles file of this machine")
        elif modules and roles.module_for(role) not in modules:
            out.append(f"{role} → module '{roles.module_for(role)}' missing from the dashboard")
        return out

    def limits_problems(role: str, explicit=False, rate=True) -> List[str]:
        lim = safety.limits(role)
        if lim is None:
            return [f"{role}: safety limits not defined"]
        miss = [m for m in lim.missing() if not (explicit and m == 'max_excursion') and not (not rate and m == 'rate_max')]
        return [f"{role}: limit(s) {', '.join(miss)} not defined"] if miss else []

    def signal_problems(role: str) -> List[str]:
        if not role:
            return []
        if not roles.is_signal(role):
            return [f"signal role '{role}' missing from the roles file of this machine"]
        ch = roles.channel_for(role)
        if channels and ch not in channels and not any(c.startswith(ch + '/') for c in channels):
            rep.warnings.append(f"{role} → channel '{ch}' not received yet (acquisition stopped?)")
        return []

    guard_problems = []
    if safety.intensity.enabled:
        if not safety.intensity.role:
            guard_problems.append("intensity guard (S2) enabled without an intensity role")
        else:
            guard_problems += signal_problems(safety.intensity.role)
    else:
        rep.warnings.append("intensity guard (S2) DISABLED in this preset")

    ids = set()
    for pid in preset.get('pids', []):
        aid = f"PID:{pid.get('id')}"
        if aid in ids:
            rep.errors.append(f"duplicate PID identifier: {pid.get('id')}")
        ids.add(aid)
        probs = []
        if not pid.get('signal'):
            probs.append("no signal selected")
        probs += signal_problems(pid.get('signal', ''))
        motors = [r for r, w in (pid.get('motors') or {}).items() if w]
        if not motors:
            probs.append("no motor (non-zero weight)")
        for r in motors:
            probs += actuator_problems(r) + limits_problems(r)
        probs += guard_problems
        rep.automations[aid] = probs

    for opt in preset.get('optimizers', []) or []:
        aid = f"OPT:{opt.get('id')}"
        if aid in ids:
            rep.errors.append(f"duplicate optimization identifier: {opt.get('id')}")
        ids.add(aid)
        probs = []
        for m in opt.get('motors', []):
            probs += actuator_problems(m.get('role', '')) + limits_problems(m.get('role', ''))
        for s in opt.get('signals', []):
            probs += signal_problems(s.get('role', ''))
        if not opt.get('motors'):
            probs.append("no motor")
        if not [s for s in opt.get('signals', []) if float(s.get('weight', 0) or 0) != 0]:
            probs.append("no objective signal")
        probs += guard_problems
        rep.automations[aid] = probs

    dup = roles.duplicate_modules()
    for module, rs in dup.items():
        rep.warnings.append(f"module {module} declared by several roles: {', '.join(rs)}")
    used = roles_used(preset)
    for r in used['actuators']:
        if not roles.is_actuator(r):
            msg = f"motor role '{r}' used by the preset but missing on this machine"
            if msg not in rep.warnings:
                rep.warnings.append(msg)
    return rep
