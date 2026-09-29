"""Preset files on disk (one JSON per preset), validation mark, session autosave (T6.4, T6.6)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .schema import content_hash, default_preset, normalize, now_iso


def safe_filename(name: str) -> str:
    name = re.sub(r'[^\w\-. ]+', '_', str(name), flags=re.UNICODE).strip().strip('.')
    return name or 'preset'


def atomic_write_json(path: Path, data) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


@dataclass
class PresetInfo:
    name: str
    path: Path
    status: str
    modified: str
    validated_on: Optional[str]
    description: str
    regime: str


class PresetStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        return self.folder / f"{safe_filename(name)}.json"

    def list(self) -> List[PresetInfo]:
        """Validated presets first (T6.4), then alphabetical."""
        infos = []
        for p in sorted(self.folder.glob('*.json')):
            try:
                d = read_json(p)
            except Exception:
                continue
            if not isinstance(d, dict) or 'schema_version' not in d:
                continue
            infos.append(PresetInfo(d.get('name', p.stem), p, d.get('status', 'draft'), d.get('modified', ''),
                                    d.get('validated_on'), d.get('description', ''),
                                    (d.get('conditions') or {}).get('regime', '')))
        infos.sort(key=lambda i: (i.status != 'validated', i.name.lower()))
        return infos

    def names(self) -> List[str]:
        return [i.name for i in self.list()]

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def load(self, name: str) -> dict:
        path = self.path_for(name)
        if not path.exists():
            # the displayed name may differ from the file name
            for info in self.list():
                if info.name == name:
                    path = info.path
                    break
        return normalize(read_json(path))

    def save(self, preset: dict, name: Optional[str] = None) -> Path:
        p = normalize(preset)
        if name:
            p['name'] = name
        p['modified'] = now_iso()
        if p.get('status') == 'validated' and p.get('validated_hash') and \
                p['validated_hash'] != content_hash(p):
            p['status'] = 'draft'     # content changed since its validation
            p['validation_notes'] = (p.get('validation_notes', '') +
                                     f"\n[{now_iso()}] modified after validation: back to draft").strip()
        preset.clear()
        preset.update(p)
        return atomic_write_json(self.path_for(p['name']), p)

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def mark_validated(self, preset: dict, by: str = '', notes: str = '') -> dict:
        preset['status'] = 'validated'
        preset['validated_on'] = now_iso()
        preset['validated_by'] = by
        if notes:
            preset['validation_notes'] = notes
        preset['validated_hash'] = content_hash(preset)
        return preset

    def new(self, name: str) -> dict:
        return default_preset(name)


class SessionStore:
    """UI state restored at restart, never with an automation running (T6.6, S9)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return read_json(self.path) if self.path.exists() else {}
        except Exception:
            return {}

    def save(self, data: dict):
        try:
            atomic_write_json(self.path, data)
        except Exception:  # pragma: no cover - never block the UI for the session file
            pass
