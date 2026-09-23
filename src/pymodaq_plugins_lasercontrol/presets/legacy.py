"""Import of the historical profiles (cahier des charges T6.3).

Handled files (``~/.pymodaq/laser_controller_profiles`` and ``~/.pymodaq/xy_stage_profiles``):

* ``<preset>.json`` session files of the old LaserController (``pid_profiles``, ``scan_motor``...)
* ``pid_profile_<name>.json`` multi-PID profiles
* ``opt_profile_<name>.json`` optimiser profiles
* XY Stage Controller profiles (``tabs`` + ``alignment``)

Actuator names and ``Detector/Channel`` names are turned into roles. Existing roles pointing to the
same module/channel are reused; otherwise a role is created (name = sanitized module/channel name).
The old per-PID ``Abs Min/Abs Max`` become the ``abs_min/abs_max`` of the role (the most
restrictive when several PIDs disagree). ``max_step``/``max_excursion``/``rate_max`` are NOT
invented: they must be set by the operator before any activation (validation tells which).
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Dict, List, Tuple

from ..core.roles import RoleMap, sanitize_role_name
from ..core.safety import ActuatorLimits, SafetyConfig
from ..engines.pid import PIDSettings
from .schema import default_pid, default_preset, normalize
from .store import read_json


class LegacyImport:
    def __init__(self, roles: RoleMap):
        self.roles = roles
        self.report: List[str] = []
        self.created_roles: List[str] = []

    # ------------------------------------------------------------------ roles
    def actuator_role(self, module: str) -> str:
        if not module or module == 'None':
            return 'None'
        role = self.roles.role_for_module(module)
        if role is None:
            role = sanitize_role_name(module)
            base, i = role, 2
            while role in self.roles.all_roles():
                role = f'{base}_{i}'
                i += 1
            self.roles.set_actuator(role, module)
            self.created_roles.append(role)
            self.report.append(f"rôle moteur créé : {role} → {module}")
        return role

    def signal_role(self, channel: str) -> str:
        if not channel:
            return ''
        role = self.roles.role_for_channel(channel)
        if role is None:
            role = sanitize_role_name(channel.split('/', 1)[-1] if '/' in channel else channel)
            base, i = role, 2
            while role in self.roles.all_roles():
                role = f'{base}_{i}'
                i += 1
            self.roles.set_signal(role, channel)
            self.created_roles.append(role)
            self.report.append(f"rôle signal créé : {role} → {channel} (clé historique : 1er canal)")
        return role

    # ------------------------------------------------------------------ converters
    def pids_from_profile(self, data: dict, safety: SafetyConfig) -> List[dict]:
        pids = []
        for pid_name, cfg in (data or {}).items():
            if not isinstance(cfg, dict):
                continue
            pid = default_pid(sanitize_role_name(cfg.get('pid_id', pid_name)))
            pid['label'] = str(pid_name)
            pid['signal'] = self.signal_role(cfg.get('detector', ''))
            motors = {}
            for act, w in (cfg.get('weights') or {}).items():
                role = self.actuator_role(act)
                motors[role] = float(w)
                lims = (cfg.get('limits') or {}).get(act)
                if lims and len(lims) == 2:
                    lim = safety.actuators.setdefault(role, ActuatorLimits())
                    lo, hi = float(min(lims)), float(max(lims))
                    lim.abs_min = lo if lim.abs_min is None else max(lim.abs_min, lo)
                    lim.abs_max = hi if lim.abs_max is None else min(lim.abs_max, hi)
            pid['motors'] = motors
            old = dict(cfg.get('config') or {})
            pid['settings'] = PIDSettings.from_dict(old).to_dict()
            pids.append(pid)
            self.report.append(f"PID importé : {pid['id']} ({pid['signal']} → {', '.join(motors)})")
        return pids

    def optimizer_from_profile(self, data: dict) -> dict:
        opt = {'config': dict(data.get('opt_config', {}) or {}), 'motors': [], 'signals': []}
        opt['config'].pop('full_optimization', None)
        for m in data.get('active_motors', []) or []:
            opt['motors'].append({'role': self.actuator_role(m.get('name', '')),
                                  'max_step': float(m.get('max_step', 0.1)),
                                  'probe_step': float(m.get('probe_step', 0.02)),
                                  'order': int(m.get('order', 0))})
        for s in data.get('target_signals', []) or []:
            opt['signals'].append({'role': self.signal_role(s.get('channel', '')),
                                   'mode': s.get('mode', 'Maximize'), 'weight': float(s.get('weight', 1.0)),
                                   'p': float(s.get('p', 2.0)), 'target_value': float(s.get('target_value', 0.0)),
                                   'auto_target': bool(s.get('auto_target', True))})
        self.report.append(f"optimiseur importé : {len(opt['motors'])} moteur(s), {len(opt['signals'])} signal(aux)")
        return opt

    def alignment_from_xy(self, data: dict) -> Tuple[dict, List[dict]]:
        align = copy.deepcopy(data.get('alignment') or {})
        for ax in align.get('axes', []) or []:
            for k in ('h', 'v'):
                ch = ax.get(k) if isinstance(ax, dict) else None
                if ch:
                    ch['actuator'] = self.actuator_role(ch.get('actuator', 'None'))
        groups = []
        for tab in data.get('tabs', []) or []:
            g = copy.deepcopy(tab)
            g['actuator_x'] = self.actuator_role(tab.get('actuator_x', 'None'))
            g['actuator_y'] = self.actuator_role(tab.get('actuator_y', 'None'))
            groups.append(g)
        self.report.append(f"alignement importé : {len(align.get('axes', []) or [])} axe(s), "
                           f"{len(groups)} groupe(s) XY")
        return align, groups

    # ------------------------------------------------------------------ entry point
    def import_file(self, path: Path, into: dict = None) -> dict:
        """Merge one historical file into the preset ``into`` (a new preset by default)."""
        preset = normalize(into) if into else default_preset(Path(path).stem)
        data = read_json(Path(path))
        safety = SafetyConfig.from_dict(preset.get('safety', {}))
        name = Path(path).name
        if isinstance(data, dict) and ('tabs' in data or 'alignment' in data):
            align, groups = self.alignment_from_xy(data)
            if align:
                preset['alignment'] = align
            preset['xy_groups'] = groups
        elif isinstance(data, dict) and ('opt_config' in data or 'active_motors' in data):
            preset['optimizer'] = self.optimizer_from_profile(data)
        elif isinstance(data, dict) and 'pid_profiles' in data:
            preset['pids'] = self.pids_from_profile(data.get('pid_profiles') or {}, safety)
        elif isinstance(data, dict) and data and all(isinstance(v, dict) and ('weights' in v or 'detector' in v)
                                                    for v in data.values()):
            preset['pids'] = self.pids_from_profile(data, safety)
        elif isinstance(data, dict) and 'schema_version' in data:
            self.report.append(f"{name} est déjà un preset Laser Control : ouvert tel quel")
            return normalize(data)
        else:
            raise ValueError(f"format de {name} non reconnu")
        preset['safety'] = safety.to_dict()
        preset['description'] = (preset.get('description', '') + f"\nImporté de {name}").strip()
        self.report.append("⚠ définir max_step / max_excursion / rate_max des rôles moteurs avant activation")
        return normalize(preset)
