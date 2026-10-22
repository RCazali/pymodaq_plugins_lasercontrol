"""Check that each motor of a dashboard answers: +step then -step (back to the start), one motor at a time.

Rules (lab test protocol, computer A, 2026-10-22):

* one command to the motors per second at most (moves, stops, position reads sent by this tool; the polling of
  PyMoDAQ during a move belongs to the move);
* a motor that does not answer (no move done before its timeout, a position not reached, not back to its start,
  an exception) stops everything: its motion is stopped, a **lock file** is written and no motor is moved any
  more, by this tool or by any other run of it, until a person checks the hardware and deletes the file;
* a motor is skipped when its position cannot be read or when start + step is outside its bounds;
* ``--dry-run`` reads and reports only (no move).

It works on DAQ_Move modules already initialised (PyMoDAQ 5.1 and 5.3)::

    from pymodaq_plugins_lasercontrol.tools.motor_check import check_motors
    report = check_motors(dashboard.actuators_modules, step=1.0, dry_run=True)

or from the command line with a PyMoDAQ 5.1 preset (the modules are initialised by the preset)::

    python -m pymodaq_plugins_lasercontrol.tools.motor_check --preset MotorControl --dry-run
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

LOCK_FILE = Path.home() / '.pymodaq' / 'laser_control' / 'MOTOR_LOCK.txt'
MIN_CALL_INTERVAL_S = 1.0


def _app():
    from qtpy import QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        try:
            from pymodaq_gui.qt_utils import mkQApp
        except ImportError:
            from pymodaq_gui.utils.utils import mkQApp
        app = mkQApp('motor_check')
    return app


def _wait(pred=lambda: False, timeout=1.0) -> bool:
    app = _app()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    app.processEvents()
    return pred()


def _as_float(value) -> float:
    """DataActuator / number -> float (NaN when unreadable)."""
    try:
        if hasattr(value, 'value'):
            value = value.value()
        import numpy as np
        arr = np.asarray(value, dtype=float).ravel()
        return float(arr[0]) if arr.size else math.nan
    except Exception:
        return math.nan


def _setting(module, name):
    """First child called ``name`` in the settings of the module (None if absent)."""
    def find(p):
        for c in p.children():
            if c.name() == name:
                return c
            r = find(c)
            if r is not None:
                return r
        return None
    try:
        return find(module.settings)
    except Exception:
        return None


def lock_reason() -> Optional[str]:
    try:
        return LOCK_FILE.read_text(encoding='utf-8') if LOCK_FILE.exists() else None
    except OSError as e:
        return f'lock file unreadable: {e}'


def write_lock(reason: str):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(f'{time.strftime("%Y-%m-%d %H:%M:%S")}  MOTORS LOCKED by motor_check\n{reason}\n'
                         'Check the hardware, then delete this file to allow motor moves again.\n',
                         encoding='utf-8')


@dataclass
class MotorResult:
    name: str
    status: str = 'not tested'        # ok | skipped | FAILED | dry-run
    units: str = ''
    start: float = math.nan
    after_plus: float = math.nan
    after_minus: float = math.nan
    reason: str = ''
    times: List[float] = field(default_factory=list)

    def line(self) -> str:
        pos = f'start {self.start:.6g} -> {self.after_plus:.6g} -> {self.after_minus:.6g} {self.units}'
        return f'{self.name}: {self.status}  {pos}' + (f'  ({self.reason})' if self.reason else '')


class _Caller:
    """At most one command to the motors per MIN_CALL_INTERVAL_S."""

    def __init__(self, interval: Optional[float] = None):
        self.interval = MIN_CALL_INTERVAL_S if interval is None else interval
        self.last = -math.inf
        self.calls: List[float] = []

    def __call__(self, fn: Callable, *args):
        dt = time.monotonic() - self.last
        if dt < self.interval:
            _wait(timeout=self.interval - dt)
        self.last = time.monotonic()
        self.calls.append(self.last)
        return fn(*args)


def _move(caller: _Caller, module, delta: float, timeout_s: float) -> float:
    """Relative move; returns the position from move_done, NaN when no move_done before the timeout."""
    done = []
    slot = (lambda v: done.append(_as_float(v)))
    module.move_done_signal.connect(slot)
    try:
        caller(module.move_rel, delta)
        _wait(lambda: bool(done), timeout_s)
    finally:
        try:
            module.move_done_signal.disconnect(slot)
        except Exception:
            pass
    return done[-1] if done else math.nan


def check_motors(modules: Iterable, step: float = 1.0, only: Optional[Iterable[str]] = None,
                 dry_run: bool = False, timeout_s: Optional[float] = None,
                 log: Callable[[str], None] = print, caller: Optional['_Caller'] = None) -> List[MotorResult]:
    """+step then -step on each initialised module, one at a time, one command per second at most.
    Stops everything and writes the lock file at the first motor that does not answer."""
    results: List[MotorResult] = []
    reason = lock_reason()
    if reason:
        log(f'REFUSED: motors are locked ({LOCK_FILE}):\n{reason}')
        return results
    wanted = set(only) if only else None
    caller = caller or _Caller()
    for module in list(modules):
        name = getattr(module, 'title', '?')
        if wanted is not None and name not in wanted:
            continue
        r = MotorResult(name)
        results.append(r)
        try:
            r.units = str(getattr(module, 'units', '') or '')
        except Exception:
            pass
        if not getattr(module, 'initialized_state', False):
            r.status, r.reason = 'skipped', 'not initialised'
            log(r.line())
            continue
        r.start = _as_float(getattr(module, 'current_value', math.nan))
        if not math.isfinite(r.start):
            got = []
            slot = (lambda v: got.append(_as_float(v)))
            module.current_value_signal.connect(slot)
            try:
                caller(module.get_actuator_value)
                _wait(lambda: bool(got), 5.0)
            finally:
                try:
                    module.current_value_signal.disconnect(slot)
                except Exception:
                    pass
            r.start = got[-1] if got else math.nan
        if not math.isfinite(r.start):
            r.status, r.reason = 'skipped', 'position unreadable'
            log(r.line())
            continue
        is_bounds = _setting(module, 'is_bounds')
        if is_bounds is not None and is_bounds.value():
            lo, hi = _setting(module, 'min_bound').value(), _setting(module, 'max_bound').value()
            if not (lo <= r.start + step <= hi and lo <= r.start <= hi):
                r.status, r.reason = 'skipped', f'start + {step:g} outside the bounds [{lo:g}, {hi:g}]'
                log(r.line())
                continue
        eps = _setting(module, 'epsilon')
        tol = max(2.0 * abs(float(eps.value())) if eps is not None else 0.0, 0.05 * abs(step), 1e-9)
        plugin_timeout = _setting(module, 'timeout')
        t_out = timeout_s or min(max(float(plugin_timeout.value()) if plugin_timeout is not None else 0.0, 10.0),
                                 60.0) + 5.0
        if dry_run:
            r.status, r.reason = 'dry-run', f'would move +{step:g} then -{step:g} {r.units} (tolerance {tol:.3g})'
            log(r.line())
            continue
        failure = ''
        try:
            if lock_reason():
                failure = 'lock file appeared during the test'
            else:
                r.times.append(time.time())
                r.after_plus = _move(caller, module, step, t_out)
                if not math.isfinite(r.after_plus):
                    failure = f'no "move done" within {t_out:.0f} s after +{step:g}'
                elif abs(r.after_plus - (r.start + step)) > tol:
                    failure = f'+{step:g} reached {r.after_plus:.6g} instead of {r.start + step:.6g}'
                elif lock_reason():
                    failure = 'lock file appeared during the test'
                else:
                    r.times.append(time.time())
                    r.after_minus = _move(caller, module, -step, t_out)
                    if not math.isfinite(r.after_minus):
                        failure = f'no "move done" within {t_out:.0f} s after -{step:g}'
                    elif abs(r.after_minus - r.start) > tol:
                        failure = f'not back to its start: {r.after_minus:.6g} instead of {r.start:.6g}'
        except Exception as e:
            failure = f'exception: {e!r}'
        if failure:
            r.status, r.reason = 'FAILED', failure
            log(r.line())
            try:
                caller(module.stop_motion)
            except Exception as e:
                log(f'{name}: stop_motion raised {e!r}')
            write_lock(f'{name}: {failure}')
            log(f'MOTORS LOCKED: {LOCK_FILE} (no motor will be moved until a person deletes it)')
            break
        r.status = 'ok'
        log(r.line())
    return results


def _dashboard_actuators(dashboard) -> list:
    mods = getattr(dashboard, 'actuators_modules', None)
    if mods is None and getattr(dashboard, 'modules_manager', None) is not None:
        mods = dashboard.modules_manager.actuators
    return list(mods or [])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--preset', required=True, help='PyMoDAQ 5.1 dashboard preset (initialises its modules)')
    parser.add_argument('--step', type=float, default=1.0)
    parser.add_argument('--only', nargs='*', help='titles of the modules to test (default: all)')
    parser.add_argument('--dry-run', action='store_true', help='read and report only, no move')
    parser.add_argument('--init-wait', type=float, default=60.0, help='max seconds for the modules to initialise')
    a = parser.parse_args(argv)
    _app()
    from pymodaq.utils.gui_utils.loader_utils import load_dashboard_with_preset
    dashboard, _ext, _win = load_dashboard_with_preset(a.preset, None)
    mods = _dashboard_actuators(dashboard)
    _wait(lambda: all(getattr(m, 'initialized_state', False) for m in mods), a.init_wait)
    results = check_motors(mods, a.step, a.only, a.dry_run)
    print('SUMMARY:', ', '.join(f'{r.name}={r.status}' for r in results) or 'nothing tested')
    dashboard.quit_fun()
    _wait(timeout=2.0)
    return 1 if any(r.status == 'FAILED' for r in results) or lock_reason() else 0


if __name__ == '__main__':
    sys.exit(main())
