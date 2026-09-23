"""Logical roles <-> real module/channel names, one TOML file per machine (cahier des charges T1.2).

Presets only reference roles (``OC_H``, ``Cam1_x``, ``Intensity_main`` ...). Each machine maps the
roles to its own DAQ_Move titles and detector channels, so that the same preset loads on the office
(mock) computer and on the lab computer without modification.

File example (``roles_lab.toml``)::

    [meta]
    machine = "lab"
    description = "Ordi A, labo"

    [actuators.OC_H]
    module = "Pico_OC_H"        # title of the DAQ_Move in the dashboard
    units = "step"
    note = "Output coupler, horizontal. d/+ = beam to the right on Cam1"

    [signals.Cam1_x]
    channel = "LECO_B/Cam1/x"    # detector title / data name / channel label

    [heartbeats]
    LECO_B = "LECO_B/Stats/frame_counter"   # O9: frame counter of computer B

Adding a new sensor later (O7) = add a module to the dashboard + declare a role here; the
recording and the UI pick it up automatically (T7.1).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .log import DedupLogger, module_logger

logger = DedupLogger(module_logger(__file__))

try:  # pragma: no cover - depends on the python version
    import tomllib as _toml_reader  # py >= 3.11
except Exception:  # pragma: no cover
    _toml_reader = None
import toml as _toml  # dependency of pymodaq_utils


def sanitize_role_name(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_\-]+', '_', str(name).strip())
    name = re.sub(r'_+', '_', name).strip('_')
    return name or 'role'


@dataclass
class ActuatorRole:
    name: str
    module: str
    units: str = ''
    note: str = ''


@dataclass
class SignalRole:
    name: str
    channel: str
    units: str = ''
    note: str = ''


@dataclass
class RoleMap:
    machine: str = ''
    description: str = ''
    actuators: Dict[str, ActuatorRole] = field(default_factory=dict)
    signals: Dict[str, SignalRole] = field(default_factory=dict)
    heartbeats: Dict[str, str] = field(default_factory=dict)
    path: Optional[Path] = None

    # ----------------------------------------------------------------- queries
    def module_for(self, role: str) -> Optional[str]:
        r = self.actuators.get(role)
        return r.module if r else None

    def channel_for(self, role: str) -> Optional[str]:
        r = self.signals.get(role)
        return r.channel if r else None

    def role_for_module(self, module: str) -> Optional[str]:
        for r in self.actuators.values():
            if r.module == module:
                return r.name
        return None

    def role_for_channel(self, channel: str) -> Optional[str]:
        for r in self.signals.values():
            if r.channel == channel:
                return r.name
        return None

    def label_for_module(self, module: str) -> str:
        role = self.role_for_module(module)
        return role if role else module

    def is_actuator(self, role: str) -> bool:
        return role in self.actuators

    def is_signal(self, role: str) -> bool:
        return role in self.signals

    def all_roles(self) -> List[str]:
        return list(self.actuators) + list(self.signals)

    def duplicate_modules(self) -> Dict[str, List[str]]:
        """Modules mapped by more than one role (allowed, but ownership is per physical module)."""
        seen: Dict[str, List[str]] = {}
        for r in self.actuators.values():
            seen.setdefault(r.module, []).append(r.name)
        return {m: rs for m, rs in seen.items() if len(rs) > 1}

    # ----------------------------------------------------------------- edition
    def set_actuator(self, name: str, module: str, units: str = '', note: str = '') -> ActuatorRole:
        name = sanitize_role_name(name)
        self.signals.pop(name, None)
        role = ActuatorRole(name, module, units, note)
        self.actuators[name] = role
        return role

    def set_signal(self, name: str, channel: str, units: str = '', note: str = '') -> SignalRole:
        name = sanitize_role_name(name)
        self.actuators.pop(name, None)
        role = SignalRole(name, channel, units, note)
        self.signals[name] = role
        return role

    def remove(self, name: str):
        self.actuators.pop(name, None)
        self.signals.pop(name, None)

    def auto_complete(self, modules: Iterable[str] = (), channels: Iterable[str] = (),
                      units_of: Optional[Callable[[str], str]] = None) -> List[str]:
        """Create a role for every actuator module / 0D channel that has none (role name = a
        sanitized version of the module / channel name). ``units_of(module)`` fills the units of the
        created actuator roles. Returns the created role names."""
        created = []
        taken = set(self.all_roles())
        for module in modules:
            if self.role_for_module(module) is None:
                name = self._unique(sanitize_role_name(module), taken)
                units = ''
                if units_of is not None:
                    try:
                        units = str(units_of(module) or '')
                    except Exception:  # pragma: no cover
                        units = ''
                self.set_actuator(name, module, units)
                taken.add(name)
                created.append(name)
        channels = list(channels)
        for ch in channels:
            if self.role_for_channel(ch) is None and ch not in self.heartbeats.values():
                parts = ch.split('/')
                short = '_'.join(parts[1:]) if len(parts) > 1 else parts[0]
                # 'Det/Mock0D/Mock_00' -> 'Mock0D_Mock_00'; if the short name is ambiguous use the full one
                ambiguous = sum(1 for c in channels if '_'.join(c.split('/')[1:]) == short) > 1
                base = sanitize_role_name(ch.replace('/', '_') if ambiguous else short)
                name = self._unique(base, taken)
                self.set_signal(name, ch)
                taken.add(name)
                created.append(name)
        return created

    @staticmethod
    def _unique(base: str, taken: set) -> str:
        name = base
        i = 2
        while name in taken:
            name = f'{base}_{i}'
            i += 1
        return name

    # ---------------------------------------------------------------- (de)serialization
    def to_dict(self) -> dict:
        return {
            'meta': {'machine': self.machine, 'description': self.description},
            'actuators': {k: {'module': v.module, 'units': v.units, 'note': v.note}
                          for k, v in self.actuators.items()},
            'signals': {k: {'channel': v.channel, 'units': v.units, 'note': v.note}
                        for k, v in self.signals.items()},
            'heartbeats': dict(self.heartbeats),
        }

    @classmethod
    def from_dict(cls, data: dict, path: Optional[Path] = None) -> 'RoleMap':
        data = data or {}
        meta = data.get('meta', {}) or {}
        rm = cls(machine=str(meta.get('machine', '')), description=str(meta.get('description', '')),
                 path=Path(path) if path else None)
        for name, d in (data.get('actuators', {}) or {}).items():
            if isinstance(d, str):
                d = {'module': d}
            if not isinstance(d, dict) or not d.get('module'):
                logger.warning(f'Role actuator "{name}" ignored: no module')
                continue
            rm.actuators[name] = ActuatorRole(name, str(d['module']), str(d.get('units', '')),
                                              str(d.get('note', '')))
        for name, d in (data.get('signals', {}) or {}).items():
            if isinstance(d, str):
                d = {'channel': d}
            if not isinstance(d, dict) or not d.get('channel'):
                logger.warning(f'Role signal "{name}" ignored: no channel')
                continue
            rm.signals[name] = SignalRole(name, str(d['channel']), str(d.get('units', '')),
                                          str(d.get('note', '')))
        for det, ch in (data.get('heartbeats', {}) or {}).items():
            if ch:
                rm.heartbeats[str(det)] = str(ch)
        return rm

    @classmethod
    def load(cls, path: Path) -> 'RoleMap':
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        text = path.read_text(encoding='utf-8')
        if _toml_reader is not None:
            data = _toml_reader.loads(text)
        else:  # pragma: no cover
            data = _toml.loads(text)
        return cls.from_dict(data, path)

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path or self.path)
        if path is None:
            raise ValueError('No path for the roles file')
        path.parent.mkdir(parents=True, exist_ok=True)
        header = ('# Laser Control - logical roles of this machine (cahier des charges T1.2)\n'
                  '# Presets reference the role names; only this file differs between machines.\n\n')
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(header + _toml.dumps(self.to_dict()), encoding='utf-8')
        tmp.replace(path)
        self.path = path
        return path

    def copy(self) -> 'RoleMap':
        return RoleMap.from_dict(self.to_dict(), self.path)
