"""Unified, versioned preset schema (cahier des charges T6.1, T6.2).

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

SCHEMA_VERSION = 1
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


def default_pid(pid_id: str = 'PID_1') -> dict:
    return {'id': pid_id, 'label': '', 'in_stabilization': True, 'signal': '', 'motors': {},
            'settings': PIDSettings().to_dict()}


def default_preset(name: str = 'Nouveau preset') -> dict:
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
        'optimizer': {'config': dict(DEFAULT_OPT_CONFIG), 'motors': [], 'signals': []},
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
    opt = p['optimizer']
    cfg = dict(DEFAULT_OPT_CONFIG)
    cfg.update(opt.get('config', {}) or {})
    cfg['full_optimization'] = False
    opt['config'] = cfg
    if p.get('status') not in STATUSES:
        p['status'] = 'draft'
    return p


def migrate(p: dict) -> dict:
    """Upgrade older schema versions in place. Version 1 is the first one."""
    version = int(p.get('schema_version', SCHEMA_VERSION) or SCHEMA_VERSION)
    if version > SCHEMA_VERSION:
        raise ValueError(f"preset créé par une version plus récente (schéma {version})")
    # future: if version < 2: ... ; p['schema_version'] = 2
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
    opt = preset.get('optimizer', {})
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
            if ch and ch.get('actuator') not in (None, '', 'None'):
                acts.add(ch['actuator'])
    for g in preset.get('xy_groups', []) or []:
        for k in ('actuator_x', 'actuator_y'):
            if g.get(k) not in (None, '', 'None'):
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
                lines.append(f"✖ {a} : {e}")
        for w in self.warnings:
            lines.append(f"⚠ {w}")
        return '\n'.join(lines) if lines else '✔ preset cohérent avec cette machine'


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
            out.append(f"rôle moteur '{role}' absent du fichier de rôles de cette machine")
        elif modules and roles.module_for(role) not in modules:
            out.append(f"{role} → module '{roles.module_for(role)}' absent du dashboard")
        return out

    def limits_problems(role: str, explicit=False, rate=True) -> List[str]:
        lim = safety.limits(role)
        if lim is None:
            return [f"{role} : limites de sécurité non définies"]
        miss = [m for m in lim.missing() if not (explicit and m == 'max_excursion') and not (not rate and m == 'rate_max')]
        return [f"{role} : limite(s) {', '.join(miss)} non définie(s)"] if miss else []

    def signal_problems(role: str) -> List[str]:
        if not role:
            return []
        if not roles.is_signal(role):
            return [f"rôle signal '{role}' absent du fichier de rôles de cette machine"]
        ch = roles.channel_for(role)
        if channels and ch not in channels and not any(c.startswith(ch + '/') for c in channels):
            rep.warnings.append(f"{role} → canal '{ch}' pas encore reçu (acquisition arrêtée ?)")
        return []

    guard_problems = []
    if safety.intensity.enabled:
        if not safety.intensity.role:
            guard_problems.append("garde intensité (S2) activée sans rôle d'intensité")
        else:
            guard_problems += signal_problems(safety.intensity.role)
    else:
        rep.warnings.append("garde intensité (S2) DÉSACTIVÉE dans ce preset")

    ids = set()
    for pid in preset.get('pids', []):
        aid = f"PID:{pid.get('id')}"
        if aid in ids:
            rep.errors.append(f"identifiant PID en double : {pid.get('id')}")
        ids.add(aid)
        probs = []
        if not pid.get('signal'):
            probs.append("aucun signal choisi")
        probs += signal_problems(pid.get('signal', ''))
        motors = [r for r, w in (pid.get('motors') or {}).items() if w]
        if not motors:
            probs.append("aucun moteur (poids non nul)")
        for r in motors:
            probs += actuator_problems(r) + limits_problems(r)
        probs += guard_problems
        rep.automations[aid] = probs

    opt = preset.get('optimizer', {})
    if opt.get('motors') or opt.get('signals'):
        probs = []
        for m in opt.get('motors', []):
            probs += actuator_problems(m.get('role', '')) + limits_problems(m.get('role', ''))
        for s in opt.get('signals', []):
            probs += signal_problems(s.get('role', ''))
        if not opt.get('motors'):
            probs.append("aucun moteur")
        if not [s for s in opt.get('signals', []) if float(s.get('weight', 0) or 0) != 0]:
            probs.append("aucun signal objectif")
        probs += guard_problems
        rep.automations['OPT'] = probs

    dup = roles.duplicate_modules()
    for module, rs in dup.items():
        rep.warnings.append(f"module {module} déclaré par plusieurs rôles : {', '.join(rs)}")
    used = roles_used(preset)
    for r in used['actuators']:
        if not roles.is_actuator(r):
            msg = f"rôle moteur '{r}' utilisé par le preset mais absent de cette machine"
            if msg not in rep.warnings:
                rep.warnings.append(msg)
    return rep
