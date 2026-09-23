"""Logging helpers: PyMoDAQ logger + de-duplication (cahier des charges T1.6, invariant S8).

A loop that fails every tick must not flood the log: identical messages emitted within
``window_s`` are counted instead of written, and the count is reported with the next occurrence.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

try:  # PyMoDAQ logger (writes into the PyMoDAQ log file as well)
    from pymodaq_utils.logger import set_logger as _pmd_set_logger
except Exception:  # pragma: no cover - pymodaq_utils is always there in practice
    _pmd_set_logger = None


def get_logger(name: str) -> logging.Logger:
    """Return the PyMoDAQ logger for ``name`` (plain ``logging`` logger as a fallback)."""
    if _pmd_set_logger is not None:
        try:
            return _pmd_set_logger(name)
        except Exception:  # pragma: no cover
            pass
    return logging.getLogger(f'pymodaq.{name}')


def module_logger(file: str) -> logging.Logger:
    """Logger named after the module file, e.g. ``lasercontrol.core.broker``."""
    p = Path(file)
    name = p.stem
    parts = [name]
    for parent in p.parents:
        if parent.name in ('', 'src') or parent.name.startswith('pymodaq_plugins_'):
            break
        parts.insert(0, parent.name)
    return get_logger('lasercontrol.' + '.'.join(parts))


class DedupLogger:
    """Wrapper suppressing identical log records repeated within ``window_s`` seconds."""

    def __init__(self, logger: logging.Logger, window_s: float = 30.0):
        self._logger = logger
        self.window_s = window_s
        self._seen: Dict[Tuple[int, str], Tuple[float, int]] = {}
        self._lock = threading.Lock()

    def _log(self, level: int, msg: str, exc_info=False):
        now = time.monotonic()
        key = (level, msg)
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last[0] < self.window_s:
                self._seen[key] = (last[0], last[1] + 1)
                return
            repeated = last[1] if last is not None else 0
            self._seen[key] = (now, 0)
            if len(self._seen) > 2000:  # keep the memory bounded (8 h endurance, T12.4)
                for k in list(self._seen)[:1000]:
                    self._seen.pop(k, None)
        if repeated:
            msg = f'{msg}  (répété {repeated}x)'
        self._logger.log(level, msg, exc_info=exc_info)

    def debug(self, msg: str):
        self._log(logging.DEBUG, msg)

    def info(self, msg: str):
        self._log(logging.INFO, msg)

    def warning(self, msg: str):
        self._log(logging.WARNING, msg)

    def error(self, msg: str, exc_info=False):
        self._log(logging.ERROR, msg, exc_info=exc_info)

    def exception(self, msg: str):
        self._log(logging.ERROR, msg, exc_info=True)


def add_rotating_file_handler(folder: Path, name: str = 'laser_control.log',
                              max_bytes: int = 5_000_000, backups: int = 5) -> Optional[logging.Handler]:
    """Add a rotating log file for the extension loggers (T1.6). Returns the handler (or None)."""
    from logging.handlers import RotatingFileHandler
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(str(folder / name), maxBytes=max_bytes, backupCount=backups,
                                      encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        target = logging.getLogger('pymodaq.lasercontrol')
        for h in target.handlers:
            if isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', '') == handler.baseFilename:
                handler.close()
                return h
        target.addHandler(handler)
        if target.level == logging.NOTSET or target.level > logging.INFO:
            target.setLevel(logging.INFO)
        return handler
    except Exception:  # pragma: no cover - never block the extension for a log file
        return None
