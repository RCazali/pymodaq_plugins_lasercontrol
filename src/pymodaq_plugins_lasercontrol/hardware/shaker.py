"""Red Pitaya piezo shaker (fire-and-forget SCPI burst), ported from the XY Stage Controller.

``redpitaya_scpi.py`` (the Red Pitaya standalone script) is looked for, in this order:
an importable ``redpitaya_scpi`` module, the ``hardware`` folder of this package, the Laser
Control data folder (``~/.pymodaq/laser_control``). The SCPI exchange runs in a worker thread so
that a network hiccup never freezes the GUI; the result is reported through a callback (called
from the worker thread: the UI re-emits it as a Qt signal).
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

DEFAULT_SHAKER_IP = 'rp-f01dda.local'


def _import_scpi_module(extra_folders: List[Path]):
    if 'redpitaya_scpi' in sys.modules:
        return sys.modules['redpitaya_scpi']
    try:
        import redpitaya_scpi  # noqa: F401
        return sys.modules['redpitaya_scpi']
    except ImportError:
        pass
    candidates = [Path(__file__).resolve().parent / 'redpitaya_scpi.py'] + \
        [Path(f) / 'redpitaya_scpi.py' for f in extra_folders]
    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location('redpitaya_scpi', path)
            module = importlib.util.module_from_spec(spec)
            sys.modules['redpitaya_scpi'] = module
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError('redpitaya_scpi.py introuvable (copier le script Red Pitaya dans : '
                            + ' ou '.join(str(c.parent) for c in candidates) + ')')


class PiezoShaker:
    def __init__(self, ip: str = DEFAULT_SHAKER_IP, amplitude_v: float = 0.1,
                 extra_folders: Optional[List[Path]] = None):
        self.ip = ip
        self.amplitude_v = min(float(amplitude_v), 0.1)      # 0.1 V maximum, as in the historical code
        self.extra_folders = list(extra_folders or [])
        self._rp = None
        self._lock = threading.Lock()
        self.busy = False

    def _ensure_connected(self):
        if self._rp is None:
            scpi = _import_scpi_module(self.extra_folders)
            self._rp = scpi.scpi(self.ip)
        return self._rp

    @staticmethod
    def waveform() -> str:
        t_real = 8.5 / 2 ** 14 * (2 ** 14 - 2048)
        t = np.linspace(-5, 5, 2 ** 14 - 2048)
        signal = (1 + np.exp(-(0.7 * t) ** 2 / 2) * np.sin(15 * 2 * np.pi * t / 10 * t_real)) / 2
        signal = np.concatenate([np.linspace(0, 0.5, 1024), signal, np.linspace(0.5, 0, 1024)])
        return ', '.join(f'{v:0.5f}' for v in signal)

    def shake_blocking(self):
        with self._lock:
            rp = self._ensure_connected()
            rp.tx_txt('GEN:RST')
            rp.tx_txt('SOUR1:FUNC ARBITRARY')
            rp.tx_txt('SOUR1:TRAC:DATA:DATA ' + self.waveform())
            rp.tx_txt(f'SOUR1:VOLT {self.amplitude_v}')
            rp.tx_txt('SOUR1:FREQ:FIX 0.1')
            rp.tx_txt('SOUR1:BURS:NCYC 1')
            rp.tx_txt('SOUR1:BURS:NOR 1')
            rp.tx_txt('SOUR1:TRIG:SOUR INT')
            rp.tx_txt('OUTPUT1:STATE ON')
            rp.tx_txt('SOUR1:TRIG:IMM')

    def shake_async(self, on_done: Callable[[bool, str], None]):
        """Run the SCPI sequence in a thread; ``on_done(ok, message)`` is called from that thread."""
        if self.busy:
            on_done(False, 'shake déjà en cours')
            return

        def run():
            self.busy = True
            try:
                self.shake_blocking()
                on_done(True, '')
            except Exception as e:
                self._rp = None                     # reconnect next time
                on_done(False, str(e))
            finally:
                self.busy = False

        threading.Thread(target=run, name='PiezoShaker', daemon=True).start()
